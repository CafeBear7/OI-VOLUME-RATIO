#!/usr/bin/env python3
"""個股期貨 近月 OI ÷ 現貨近 5 日均量 —— 抓資料、計算、累積歷史。

每天盤後由 GitHub Actions 執行一次：
  python3 fetch.py

會做的事：
  1. 期交所 OpenAPI：股票期貨標的清單、每日行情（近月 OI）、調整型契約股數。
  2. 證交所 / 櫃買 OpenAPI：當日各股成交股數。
  3. 把當日成交量追加進 docs/data/history.csv（每檔一列，保留最近 N 天）。
  4. 用歷史算近 5 日均量 → 算 OI／量比 → 寫出 docs/data/latest.json（前端讀這個）。

離線測試（不連網，用 sample/ 的假資料）：
  python3 fetch.py --fixture sample/taifex.json sample/twse.json sample/tpex.json --today 2026-09-18

期交所 OpenAPI 在部分雲端環境（含本專案的開發沙箱）會擋 IP，但 GitHub Actions
的機器連得到，正式排程不受影響。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "docs" / "data"
HISTORY = DATA / "history.csv"
LATEST = DATA / "latest.json"

HISTORY_DAYS = 40          # history.csv 保留的天數（>5 即可算均量，多留一點給趨勢圖）
AVG_WINDOW = 5             # 近幾日均量

# ---- 資料來源（皆為免金鑰 OpenAPI）--------------------------------------
TAIFEX = "https://openapi.taifex.com.tw/v1"
SRC = {
    "SSF_LIST": f"{TAIFEX}/SSFLists",                # 股票期貨交易標的（商品代碼 ↔ 證券代號）
    "SSF_DAILY": f"{TAIFEX}/DailyMarketReportFut",   # 期貨每日交易行情（含未沖銷契約數 OI）
    "SSF_ADJ": f"{TAIFEX}/SSFAdjustedInfo",          # 調整型契約（每口股數與 2000 不同）
    "TWSE_VOL": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",       # 上市每日收盤
    "TPEX_VOL": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",  # 上櫃每日收盤
}

# 每口股數：一般股票期貨 2000、國內成分股 ETF 期貨 10000。小型／微型與調整型另外處理。
LOT_STOCK = 2000
LOT_ETF = 10000


# ---- 小工具 ------------------------------------------------------------
def fetch_json(url: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "ssf-oi-ratio", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = r.read().decode("utf-8-sig", "replace").strip()
            if txt.startswith("<"):
                raise RuntimeError("回傳 HTML 而非 JSON（可能被防火牆擋）")
            return json.loads(txt) if txt else []
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"抓取失敗 {url}：{last}")


def pick(rec: dict, *names, default=""):
    """欄位名稱容錯：去空白比對，支援多個別名。"""
    norm = {re.sub(r"\s+", "", str(k)): v for k, v in rec.items()}
    for n in names:
        v = norm.get(re.sub(r"\s+", "", n))
        if v not in (None, ""):
            return str(v).strip()
    return default


def to_num(s) -> float:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def roc_to_iso(s: str) -> str:
    """1150918 / 115/09/18 / 2026-09-18 → 2026-09-18。"""
    s = str(s or "").strip()
    m = re.fullmatch(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", s)
    if m and int(m.group(1)) > 1911:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.fullmatch(r"(\d{3})[-/]?(\d{2})[-/]?(\d{2})", s) or re.fullmatch(r"(\d{3})(\d{2})(\d{2})", s)
    if m:
        return f"{int(m.group(1)) + 1911:04d}-{m.group(2)}-{m.group(3)}"
    return ""


def is_etf(code: str, name: str = "") -> bool:
    return code.startswith("00") or "ETF" in name.upper()


# ---- 期交所：標的、調整型、近月 OI -------------------------------------
def load_ssf_meta(rows) -> dict:
    """商品代碼 → {stock, name, market, etf}。SSFLists 欄位以 pick 容錯。"""
    meta = {}
    for r in rows:
        contract = pick(r, "Contract", "商品代碼", "股票期貨商品代碼")
        stock = pick(r, "StockCode", "證券代號", "標的證券代號")
        name = pick(r, "StockName", "UnderlyingStock", "標的證券", "標的證券簡稱")
        if not contract or not stock:
            continue
        meta[contract] = {"stock": stock, "name": name, "etf": is_etf(stock, name)}
    return meta


def load_adjusted(rows) -> dict:
    """商品代碼 → 每口股數（調整型契約）。"""
    adj = {}
    for r in rows:
        contract = pick(r, "Contract", "商品代碼", "股票期貨英文代碼")
        shares = to_num(pick(r, "UnderlyingSecurityShares", "約定標的物證券股數", "標的證券股數"))
        if contract and shares > 0:
            adj[contract] = shares
    return adj


NEAR_RE = re.compile(r"^\d{6}$")  # 到期月份 202610；價差單為 202610/202611，用此排除


def load_near_oi(rows, meta: dict, adj: dict) -> dict:
    """每檔標的證券 → 近月 OI 累加（口 與 股）。

    規則：
      - 只取一般交易時段（避免與盤後時段重複計 OI）。
      - 排除價差單（到期月份含 '/'）。
      - 同一標的可能有一般＋小型＋調整型多個商品代碼，各自乘以自己的每口股數後相加。
    """
    # 先挑出每個商品代碼的「近月」那一列
    per_contract = {}  # contract -> (month, oi)
    for r in rows:
        contract = pick(r, "Contract", "商品代碼")
        month = pick(r, "ContractMonth(Week)", "ContractMonth", "到期月份(週別)", "到期月份")
        session = pick(r, "TradingSession", "交易時段")
        if not contract or not NEAR_RE.match(month):
            continue  # 排除價差單與異常列
        if session and ("盤後" in session or "After" in session):
            continue
        oi = to_num(pick(r, "OpenInterest", "未沖銷契約數", "未沖銷契約量"))
        cur = per_contract.get(contract)
        if cur is None or month < cur[0]:
            per_contract[contract] = (month, oi)

    out = {}
    near_month = None
    for contract, (month, oi) in per_contract.items():
        m = meta.get(contract)
        if not m:
            continue  # 不在股票期貨標的清單（可能是指數期貨等）
        if near_month is None or month < near_month:
            near_month = month
        # 小型契約：期交所命名規則，契約代碼第 2 碼固定為 W（例：一般旺矽 UVF、小型旺矽 UWF）。
        # SSFLists／每日行情都沒有中文契約名或每口股數欄位，只能靠代碼區分。
        # 每口股數：小型股票 100 股、小型 ETF 1,000 單位；一般股票 2,000、一般 ETF 10,000。
        # 調整型契約（除權／現增後換代碼）以期交所公告的每口股數為準，優先採用。
        is_small = len(contract) >= 2 and contract[1] == "W"
        if contract in adj:
            lot = adj[contract]
            small, adjusted = is_small, True
        elif is_small:
            lot = 1000 if m["etf"] else 100
            small, adjusted = True, False
        elif m["etf"]:
            lot = LOT_ETF
            small, adjusted = False, False
        else:
            lot = LOT_STOCK
            small, adjusted = False, False

        e = out.setdefault(m["stock"], {
            "stock": m["stock"], "name": m["name"], "oi_lots": 0.0,
            "oi_shares": 0.0, "small": False, "adjusted": False, "month": month})
        e["oi_lots"] += oi
        e["oi_shares"] += oi * lot
        e["small"] = e["small"] or small
        e["adjusted"] = e["adjusted"] or adjusted
        e["name"] = e["name"] or m["name"]
    return out, near_month or ""


# ---- 現貨成交量 --------------------------------------------------------
def load_twse_vol(rows) -> dict:
    out = {}
    for r in rows:
        code = pick(r, "Code", "證券代號")
        if not re.fullmatch(r"\d{4}", code):  # 只留 4 位數普通股／ETF
            continue
        out[code] = {"vol": to_num(pick(r, "TradeVolume", "成交股數")), "market": "上市",
                     "name": pick(r, "Name", "證券名稱")}
    return out


def load_tpex_vol(rows) -> dict:
    out = {}
    for r in rows:
        code = pick(r, "SecuritiesCompanyCode", "證券代號", "股票代號")
        if not re.fullmatch(r"\d{4}", code):
            continue
        out[code] = {"vol": to_num(pick(r, "TradingShares", "成交股數")), "market": "上櫃",
                     "name": pick(r, "CompanyName", "公司名稱")}
    return out


# ---- 歷史 CSV ----------------------------------------------------------
def read_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    with HISTORY.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_history(rows: list[dict]):
    DATA.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "code", "market", "volume_shares"])
        w.writeheader()
        w.writerows(rows)


# ---- 主流程 ------------------------------------------------------------
def load_all(args):
    if args.fixture:
        blobs = {}
        for f in args.fixture:
            blobs.update(json.loads(Path(f).read_text("utf-8")))
        ssf_list = blobs.get("SSF_LIST", [])
        ssf_daily = blobs.get("SSF_DAILY", [])
        ssf_adj = blobs.get("SSF_ADJ", [])
        twse = blobs.get("TWSE_VOL", [])
        tpex = blobs.get("TPEX_VOL", [])
        errors = []
    else:
        errors = []
        def safe(key):
            try:
                return fetch_json(SRC[key])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{key}: {e}")
                return []
        ssf_list = safe("SSF_LIST")
        ssf_daily = safe("SSF_DAILY")
        ssf_adj = safe("SSF_ADJ")
        twse = safe("TWSE_VOL")
        tpex = safe("TPEX_VOL")
    return ssf_list, ssf_daily, ssf_adj, twse, tpex, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", nargs="*", help="改用本機 JSON（測試用）")
    ap.add_argument("--today", help="指定資料日期 YYYY-MM-DD（測試用）")
    args = ap.parse_args()

    today = args.today or dt.datetime.now(TZ).date().isoformat()
    ssf_list, ssf_daily, ssf_adj, twse, tpex, errors = load_all(args)

    meta = load_ssf_meta(ssf_list)
    adj = load_adjusted(ssf_adj)
    oi_by_stock, near_month = load_near_oi(ssf_daily, meta, adj)
    vol = {**load_twse_vol(twse), **load_tpex_vol(tpex)}

    # 追加當日成交量到歷史（同日重跑會覆蓋當日）
    hist = [r for r in read_history() if r["date"] != today]
    for code, v in vol.items():
        if code in oi_by_stock and v["vol"] > 0:  # 只存有股期的標的，控制檔案大小
            hist.append({"date": today, "code": code, "market": v["market"],
                         "volume_shares": str(int(v["vol"]))})
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    hist = [r for r in hist if r["date"] >= cutoff]
    hist.sort(key=lambda r: (r["code"], r["date"]))
    write_history(hist)

    # 近 5 日均量（每檔取最近 AVG_WINDOW 個交易日）
    by_code = {}
    for r in hist:
        by_code.setdefault(r["code"], []).append((r["date"], int(r["volume_shares"])))
    avg5 = {}
    for code, series in by_code.items():
        series.sort()
        window = series[-AVG_WINDOW:]
        if window:
            avg5[code] = (sum(v for _, v in window) / len(window), len(window))

    # 組出每檔的比例
    rows = []
    for code, e in oi_by_stock.items():
        a = avg5.get(code)
        if not a or a[0] <= 0:
            continue  # 沒有成交量資料，跳過
        avg_shares, ndays = a
        ratio = e["oi_shares"] / avg_shares * 100
        v = vol.get(code, {})
        rows.append({
            "code": code,
            "name": e["name"] or v.get("name", ""),
            "market": v.get("market", "ETF" if is_etf(code) else ""),
            "oi_lots": round(e["oi_lots"]),
            "oi_shares": round(e["oi_shares"]),
            "oi_lots_equiv": round(e["oi_shares"] / 1000),   # 換算張數（1 張 = 1000 股）
            "avg5_shares": round(avg_shares),
            "avg5_lots": round(avg_shares / 1000),
            "ratio_pct": round(ratio, 1),
            "avg_days": ndays,             # 均量實際採用天數（<5 表示歷史還沒累積滿）
            "small": e["small"],
            "adjusted": e["adjusted"],
            "month": e["month"],
        })
    rows.sort(key=lambda r: -r["ratio_pct"])

    payload = {
        "report_date": today,
        "near_month": near_month,
        "generated_at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
        "avg_window": AVG_WINDOW,
        "count": len(rows),
        "rows": rows,
        "errors": errors,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")

    print(json.dumps({"date": today, "near_month": near_month, "stocks_with_oi": len(oi_by_stock),
                      "volume_codes": len(vol), "output_rows": len(rows),
                      "history_rows": len(hist), "errors": errors}, ensure_ascii=False, indent=1))
    if errors and not rows:
        sys.exit(1)  # 全部來源失敗才視為失敗，讓 Actions 標紅


if __name__ == "__main__":
    main()
