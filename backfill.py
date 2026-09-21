#!/usr/bin/env python3
"""一次性補資料：把股票期貨標的前幾個交易日的成交量補進 docs/data/history.csv，
讓「近 5 日均量」第一天就準（不必等 5 個交易日）。

用法（先跑過一次 fetch.py，產生 docs/data/latest.json 之後再跑這支）：
  python3 backfill.py               # 補到湊滿近 5 個交易日
  python3 backfill.py --days 20     # 想多補一點給趨勢圖，改天數

補完後再跑一次 fetch.py，latest.json 的均量就會用滿整個視窗：
  python3 fetch.py

資料來源（某日全部個股，一天各叫一次）：
  證交所 MI_INDEX（date=YYYYMMDD）、櫃買 otc（date=YYYY/MM/DD）。成交量單位皆為「股」。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "docs" / "data"
HISTORY = DATA / "history.csv"
LATEST = DATA / "latest.json"

AVG_WINDOW = 5      # 要湊滿的交易日數（與 fetch.py 一致）
HISTORY_DAYS = 40
LOOKBACK_LIMIT = 40  # 最多往前找幾個「日曆日」來湊交易日，避免連假時無限往前

TWSE = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json"
TPEX = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?type=EW&date={y}/{m}/{d}&response=json"


def get(url: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ssf-oi-ratio-backfill",
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = r.read().decode("utf-8-sig", "replace").strip()
            if txt.startswith("<"):
                raise RuntimeError("回傳 HTML（可能被擋）")
            return json.loads(txt) if txt else {}
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(str(last))


def _idx(fields, kw):
    for i, f in enumerate(fields):
        if kw in str(f).replace(" ", ""):
            return i
    return -1


def _num(s):
    return int(re.sub(r"[^\d]", "", str(s)) or 0)


def _stock_table(d):
    """從 tables 陣列挑出含『代號 + 成交股數』的個股表，回傳 {code: shares}。"""
    for t in d.get("tables", []) if isinstance(d, dict) else []:
        f = t.get("fields", [])
        ci = _idx(f, "證券代號")
        if ci < 0:
            ci = _idx(f, "代號")
        vi = _idx(f, "成交股數")
        if ci >= 0 and vi >= 0 and t.get("data"):
            out = {}
            for row in t["data"]:
                code = str(row[ci]).strip()
                if code:
                    out[code] = _num(row[vi])
            return out
    return {}


def fetch_twse(day: dt.date) -> dict:
    d = get(TWSE.format(ymd=day.strftime("%Y%m%d")))
    if isinstance(d, dict) and d.get("stat") not in ("OK", None):
        return {}   # 非交易日
    return _stock_table(d)


def fetch_tpex(day: dt.date) -> dict:
    d = get(TPEX.format(y=day.year, m=f"{day.month:02d}", d=f"{day.day:02d}"))
    return _stock_table(d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=AVG_WINDOW, help="要湊滿的交易日數")
    ap.add_argument("--today", help="以此日期為基準往前補（測試用）")
    args = ap.parse_args()

    if not LATEST.exists():
        raise SystemExit("找不到 docs/data/latest.json，請先執行一次 python3 fetch.py")
    latest = json.loads(LATEST.read_text("utf-8"))
    # 只補「有股期、且 fetch.py 已列入」的標的，與 history.csv 一致，控制檔案大小
    want = {r["code"]: r.get("market", "") for r in latest.get("rows", [])}
    if not want:
        raise SystemExit("latest.json 沒有任何標的，請確認 fetch.py 已成功抓到期交所資料")

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()

    # 讀既有歷史，記下每檔已有哪些日期
    existing = []
    have = set()
    if HISTORY.exists():
        with HISTORY.open("r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                existing.append(r)
                have.add((r["date"], r["code"]))

    added, got_days, day = 0, 0, today - dt.timedelta(days=1)
    limit = today - dt.timedelta(days=LOOKBACK_LIMIT)
    while got_days < args.days and day >= limit:
        if day.weekday() >= 5:   # 週六日直接跳過
            day -= dt.timedelta(days=1)
            continue
        try:
            tw = fetch_twse(day)
            time.sleep(0.6)
            tp = fetch_tpex(day)
            time.sleep(0.6)
        except Exception as e:  # noqa: BLE001
            print(f"  {day} 抓取失敗：{e}")
            day -= dt.timedelta(days=1)
            continue
        vols = {**tw, **tp}
        if not vols:            # 非交易日（假日）
            day -= dt.timedelta(days=1)
            continue
        n = 0
        for code, market in want.items():
            v = vols.get(code)
            if v and (day.isoformat(), code) not in have:
                existing.append({"date": day.isoformat(), "code": code,
                                 "market": market, "volume_shares": str(v)})
                have.add((day.isoformat(), code))
                n += 1
        print(f"  {day}（{day.strftime('%a')}）：補進 {n} 檔")
        added += n
        got_days += 1
        day -= dt.timedelta(days=1)

    # 去重、修剪、排序後寫回
    cutoff = (today - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    existing = [r for r in existing if r["date"] >= cutoff]
    existing.sort(key=lambda r: (r["code"], r["date"]))
    DATA.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "code", "market", "volume_shares"])
        w.writeheader()
        w.writerows(existing)

    print(f"\n補進 {got_days} 個交易日、共 {added} 筆。history.csv 現有 {len(existing)} 列。")
    print("接著再跑一次 fetch.py，latest.json 的均量就會用滿整個視窗：python3 fetch.py")


if __name__ == "__main__":
    main()
