# 個股期貨 OI／量比 觀測站

每天盤後計算：**台灣有個股期貨的標的，其近月未沖銷契約量（OI）換算成股數後，佔該股近 5 個交易日平均成交量的比例。**
比例越高，代表期貨市場堆積的部位相對於現貨成交越大。

- 資料由 GitHub Actions 每天盤後（台灣時間 20:00）自動抓取並累積。
- 網頁是純靜態頁面，放在 GitHub Pages，朋友點連結就能看，不必安裝任何東西。
- 全部免金鑰、免伺服器、免費。

## 檔案結構

```
ssf-oi-ratio/
├── fetch.py                    # 抓資料 + 計算 + 累積歷史（每天）
├── backfill.py                 # 一次性補足前幾天成交量（只第一天用）
├── docs/                       # ← GitHub Pages 的根目錄
│   ├── index.html              #   前端頁面（讀 data/latest.json）
│   └── data/
│       ├── latest.json         #   最新一天的結果（前端讀這個）
│       └── history.csv         #   每日成交量歷史（算 5 日均量用，保留 40 天）
├── .github/workflows/daily.yml # 每日排程
└── sample/                     # 離線測試用假資料
```

## 資料來源（都是官方免金鑰 OpenAPI）

| 資料 | 來源 |
|------|------|
| 股票期貨標的清單（商品代碼 ↔ 證券代號） | 期交所 `SSFLists` |
| 期貨每日行情（近月 OI） | 期交所 `DailyMarketReportFut` |
| 調整型契約每口股數 | 期交所 `SSFAdjustedInfo` |
| 上市每日成交量 | 證交所 `STOCK_DAY_ALL` |
| 上櫃每日成交量 | 櫃買 `tpex_mainboard_daily_close_quotes` |

## 一次性設定（約 5 分鐘）

1. **建 repo**：把這個資料夾推上 GitHub（公開 repo 的 Actions 與 Pages 都免費）。
   ```bash
   git init && git add . && git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/你的帳號/ssf-oi-ratio.git
   git push -u origin main
   ```
2. **開 Pages**：Repo → Settings → Pages → Source 選 `Deploy from a branch`，
   Branch 選 `main`、資料夾選 `/docs`，存檔。等一兩分鐘就會有網址，把它分享給朋友。
3. **給 Actions 寫入權限**：Repo → Settings → Actions → General →
   最下面 Workflow permissions 選 `Read and write permissions`，存檔。
   （這樣排程才能把更新後的資料 commit 回 repo。）
4. **先手動跑一次**：Repo → Actions → 「每日盤後更新」→ Run workflow。
   跑完後 `docs/data/latest.json` 就會出現，網頁也就有資料了。

之後每天盤後會自動更新，你不用再管它。

> 註：GitHub 的排程可能會延遲幾分鐘到十幾分鐘觸發，屬正常現象。

## 本機測試

不連網、用假資料跑一遍（會產生 `docs/data/latest.json`）：

```bash
python3 fetch.py --fixture sample/taifex.json sample/twse.json sample/tpex.json --today 2026-09-18
```

實際連線跑（需要能連到期交所，本機一般可以）：

```bash
python3 fetch.py
```

在本機預覽網頁：

```bash
cd docs && python3 -m http.server 8000
# 瀏覽器開 http://localhost:8000
```

## 計算與注意事項

- **單位換算**：OI 單位是「口」，成交量是「股」，先換成同單位再相除。
  一般股票期貨每口 2,000 股、小型契約 100 股、國內成分股 ETF 期貨 10,000 單位。
- **調整型契約**：標的除權／現增後每口股數會變，本專案用期交所 `SSFAdjustedInfo`
  的股數換算，並在表上標「調整型」。
- **換月**：股票期貨每月第 3 個週三結算，結算前後近月 OI 會有斷層。目前取「最近到期月份」
  為近月；若想看得更平順，可改成近月＋次月合計（見 `fetch.py` 的 `load_near_oi`）。
- **盤後時段與價差單**：已排除盤後交易時段的重複列，以及到期月份為「202610/202611」的價差單。

## 首次上線時把歷史補滿（選用）

近 5 日均量需要 5 個交易日的成交量。剛建好時 `history.csv` 是空的，
第一天只會有 1 天資料（表上會標示「(1日)」），跑滿 5 個交易日後就正常。

不想等的話，用內建的 `backfill.py` 一次補齊。它會自動往前找最近幾個交易日
（遇到週末、假日會跳過），把成交量補進 `history.csv`。**先跑過一次 `fetch.py`**
（要有 `latest.json` 才知道要補哪些標的），再照順序：

```bash
python3 fetch.py        # 先產生 latest.json（第一天）
python3 backfill.py     # 補到湊滿近 5 個交易日
python3 fetch.py        # 再算一次，均量就用滿整個視窗
```

補資料來源是證交所 MI_INDEX 與櫃買 otc 的「某日全部個股」端點，一天各叫一次，
成交量單位為股。想多補一點給趨勢圖，可 `python3 backfill.py --days 20`。
`backfill.py` 只在第一天跑一次，之後每天的 `fetch.py` 會自己累積。

（Windows 上把上面的 `python3` 都改成 `python`。）

## 免責

本頁數據由程式自動彙整，僅供參考，不構成投資建議，請以官方公告為準。
