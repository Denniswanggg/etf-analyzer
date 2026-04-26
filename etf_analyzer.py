#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台灣前10大主動式ETF低持股交集分析工具
功能：每日抓取持股 → 篩選<2% → 找交集 → 比較成長 → 輸出報告
"""

import os, json, time, logging, re
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import pandas as pd
from bs4 import BeautifulSoup

# ─── 設定區（可自行修改）─────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
DATA_DIR        = BASE_DIR / "data"
HISTORY_DIR     = DATA_DIR / "history"
REPORT_DIR      = BASE_DIR / "reports"
LOG_DIR         = BASE_DIR / "logs"

WEIGHT_THRESHOLD = 2.0   # 持股上限(%)
MIN_ETF_OVERLAP  = 2     # 至少幾檔ETF同時持有才算交集

# 台灣前10大績效主動式ETF（依近年績效/規模排列）
TARGET_ETFS = {
    "00878": "國泰永續高股息",
    "00919": "群益台灣精選高息",
    "00929": "復華台灣科技優息",
    "00900": "富邦特選高股息30",
    "0056":  "元大高股息",
    "00944": "群益半導體收益",
    "00933": "國泰台灣領袖50",
    "00930": "永豐ESG低碳高息",
    "00713": "元大台灣高息低波",
    "00907": "永豐台灣優息存股",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
}
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging():
    for d in [DATA_DIR, HISTORY_DIR, REPORT_DIR, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"etf_{datetime.now():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ]
    )

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════
#  資料抓取層
# ══════════════════════════════════════════════

def fetch_twse(etf_id: str) -> Optional[pd.DataFrame]:
    """主來源：TWSE OpenAPI 抓取ETF成分股"""
    url = f"https://openapi.twse.com.tw/v1/ETF/GetETFComponentStocks?stockNo={etf_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        rows = []
        for item in data:
            ticker = str(item.get("StockCode", "")).strip()
            name   = str(item.get("StockName", "")).strip()
            raw_w  = str(item.get("Weight", "0")).replace("%","").replace(",","")
            try:
                weight = float(raw_w)
            except ValueError:
                weight = 0.0
            if ticker and ticker.isdigit():
                rows.append({"ticker": ticker, "name": name, "weight": weight})
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["etf_id"]   = etf_id
        df["etf_name"] = TARGET_ETFS[etf_id]
        df["source"]   = "TWSE"
        log.info(f"  ✓ TWSE  {etf_id} {TARGET_ETFS[etf_id]}: {len(df)} 檔")
        return df
    except Exception as e:
        log.warning(f"  ✗ TWSE  {etf_id}: {e}")
        return None


def fetch_moneydj(etf_id: str) -> Optional[pd.DataFrame]:
    """備援來源：MoneyDJ 爬取ETF持股"""
    url = f"https://www.moneydj.com/ETF/X/Basic/Basic0007.xdjhtm?etfid={etf_id}.TW"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"id": "oMainTable"}) or soup.find("table", class_="datalist")
        if not table:
            return None
        rows = []
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            ticker = re.sub(r"\D", "", tds[1].get_text(strip=True))
            name   = tds[2].get_text(strip=True)
            raw_w  = tds[3].get_text(strip=True).replace("%","")
            try:
                weight = float(raw_w)
            except ValueError:
                continue
            if ticker:
                rows.append({"ticker": ticker, "name": name, "weight": weight})
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["etf_id"]   = etf_id
        df["etf_name"] = TARGET_ETFS[etf_id]
        df["source"]   = "MoneyDJ"
        log.info(f"  ✓ MDJ   {etf_id} {TARGET_ETFS[etf_id]}: {len(df)} 檔")
        return df
    except Exception as e:
        log.warning(f"  ✗ MDJ   {etf_id}: {e}")
        return None


def fetch_all() -> pd.DataFrame:
    log.info("━"*52)
    log.info("【步驟1】抓取 ETF 持股資料")
    frames = []
    for etf_id in TARGET_ETFS:
        df = fetch_twse(etf_id)
        if df is None:
            df = fetch_moneydj(etf_id)
        if df is not None:
            frames.append(df)
        time.sleep(1.0)   # 避免被封鎖

    if not frames:
        raise RuntimeError("❌ 所有ETF資料抓取失敗，請檢查網路連線")

    all_df = pd.concat(frames, ignore_index=True)
    all_df["date"] = datetime.now().strftime("%Y-%m-%d")

    # 儲存每日原始資料
    raw_path = HISTORY_DIR / f"raw_{datetime.now():%Y%m%d}.csv"
    all_df.to_csv(raw_path, index=False, encoding="utf-8-sig")
    log.info(f"原始資料已存：{raw_path}（{len(all_df)} 筆）")
    return all_df


# ══════════════════════════════════════════════
#  分析層
# ══════════════════════════════════════════════

def load_prev() -> Optional[pd.DataFrame]:
    """載入上一次的歷史資料，供計算持股變化"""
    today = datetime.now().strftime("%Y%m%d")
    files = sorted(HISTORY_DIR.glob("raw_*.csv"))
    prev  = [f for f in files if today not in f.name]
    if not prev:
        log.info("無歷史資料，跳過持股變化計算")
        return None
    path = prev[-1]
    log.info(f"載入上期資料：{path.name}")
    return pd.read_csv(path, dtype={"ticker": str})


def analyze(current: pd.DataFrame, previous: Optional[pd.DataFrame]) -> pd.DataFrame:
    log.info("━"*52)
    log.info(f"【步驟2】篩選持股 ≤ {WEIGHT_THRESHOLD}%")

    # 1. 篩選低持股
    low = current[current["weight"] <= WEIGHT_THRESHOLD].copy()
    log.info(f"  低持股個股：{low['ticker'].nunique()} 檔")

    # 2. 計算交集
    log.info(f"【步驟3】找出被 ≥ {MIN_ETF_OVERLAP} 檔ETF同時持有的個股")
    grp = (
        low.groupby("ticker")
        .agg(
            name       = ("name",    "first"),
            etf_count  = ("etf_id",  "nunique"),
            etf_ids    = ("etf_id",  lambda x: ",".join(sorted(x.unique()))),
            etf_names  = ("etf_name",lambda x: "、".join(sorted(x.unique()))),
            avg_weight = ("weight",  "mean"),
            max_weight = ("weight",  "max"),
            min_weight = ("weight",  "min"),
        )
        .reset_index()
    )
    result = grp[grp["etf_count"] >= MIN_ETF_OVERLAP].copy()
    log.info(f"  交集個股：{len(result)} 檔")

    # 3. 計算持股變化（月對月）
    log.info("【步驟4】計算持股成長比例")
    if previous is not None:
        prev_low = previous[previous["weight"] <= WEIGHT_THRESHOLD]
        prev_avg = (
            prev_low.groupby("ticker")["weight"]
            .mean()
            .rename("prev_weight")
        )
        result = result.join(prev_avg, on="ticker")
        result["weight_change"] = (result["avg_weight"] - result["prev_weight"]).round(4)
        result["is_growing"]    = result["weight_change"] > 0
        grow_n = result["is_growing"].sum()
        log.info(f"  成長持股：{grow_n} 檔 / 減少：{len(result)-grow_n} 檔")
    else:
        result["prev_weight"]   = None
        result["weight_change"] = None
        result["is_growing"]    = None

    # 4. 整理欄位 & 排序
    result["avg_weight"] = result["avg_weight"].round(4)
    result["report_date"] = datetime.now().strftime("%Y-%m-%d")
    result = result.sort_values(
        ["etf_count", "is_growing", "avg_weight"],
        ascending=[False, False, True]
    ).reset_index(drop=True)

    return result


# ══════════════════════════════════════════════
#  報告輸出層
# ══════════════════════════════════════════════

def save_csv(df: pd.DataFrame, ds: str) -> Path:
    path = REPORT_DIR / f"intersection_{ds}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    log.info(f"CSV 報告：{path}")
    return path


def save_html(df: pd.DataFrame, ds: str) -> Path:
    total   = len(df)
    growing = int(df["is_growing"].sum()) if df["is_growing"].notna().any() else "－"
    avg_w   = round(df["avg_weight"].mean(), 2) if not df.empty else 0

    rows_html = ""
    for i, row in df.iterrows():
        # 成長標籤
        chg = row["weight_change"]
        if pd.isna(chg) or chg is None:
            chg_html = '<span class="tag neu">首次收錄</span>'
        elif chg > 0:
            chg_html = f'<span class="tag grow">▲ +{chg:.2f}%</span>'
        else:
            chg_html = f'<span class="tag shrink">▼ {chg:.2f}%</span>'

        # ETF徽章
        etf_badges = "".join(
            f'<span class="etf-badge">{e.strip()}</span>'
            for e in row["etf_ids"].split(",")
        )

        # 列底色：成長=淡綠，減少=淡紅
        bg = ""
        if pd.notna(row.get("is_growing")):
            bg = 'style="background:#f0faf4"' if row["is_growing"] else 'style="background:#fff8f8"'

        rows_html += f"""
        <tr {bg}>
          <td><b>{row['ticker']}</b><br><small style="color:#666">{row['name']}</small></td>
          <td style="text-align:center">{row['etf_count']}</td>
          <td>{etf_badges}</td>
          <td style="text-align:center">{row['avg_weight']:.2f}%</td>
          <td style="text-align:center">{chg_html}</td>
          <td style="text-align:center;font-size:11px;color:#888">{row['etf_names']}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETF 低持股交集分析 {ds}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:"Noto Sans TC",Arial,sans-serif;background:#f0f2f5;padding:20px;color:#222}}
  .wrap{{max-width:1100px;margin:0 auto}}
  .hero{{background:linear-gradient(135deg,#1a3a5c,#2980b9);color:#fff;padding:28px 32px;border-radius:14px;margin-bottom:20px}}
  .hero h1{{font-size:22px;font-weight:700}}
  .hero p{{font-size:13px;opacity:.75;margin-top:6px}}
  .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:20px}}
  .kpi{{background:#fff;padding:18px 20px;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  .kpi .lbl{{font-size:11px;color:#888;letter-spacing:.5px;text-transform:uppercase}}
  .kpi .val{{font-size:28px;font-weight:700;color:#1a3a5c;margin-top:4px}}
  .card{{background:#fff;border-radius:10px;box-shadow:0 1px 6px rgba(0,0,0,.08);overflow:hidden}}
  .card-hd{{padding:14px 20px;border-bottom:1px solid #eee;font-weight:600;color:#1a3a5c;font-size:15px}}
  table{{width:100%;border-collapse:collapse}}
  th{{background:#f7f8fa;padding:11px 14px;text-align:left;font-size:12px;color:#666;border-bottom:2px solid #eee;white-space:nowrap}}
  td{{padding:12px 14px;border-bottom:1px solid #f2f2f2;font-size:13px;vertical-align:middle}}
  tr:hover td{{background:#fafbff!important}}
  .etf-badge{{display:inline-block;background:#e3f0fb;color:#1a6caa;font-size:11px;padding:2px 7px;border-radius:4px;margin:2px;font-weight:600}}
  .tag{{display:inline-block;font-size:11px;padding:3px 9px;border-radius:12px;font-weight:600}}
  .grow{{background:#e6f9ee;color:#1e7e45}}
  .shrink{{background:#fdecea;color:#b92222}}
  .neu{{background:#ececec;color:#666}}
  .note{{text-align:center;font-size:11px;color:#bbb;margin-top:18px}}
  @media(max-width:600px){{td,th{{font-size:11px;padding:8px 8px}}}}
</style>
</head>
<body><div class="wrap">

<div class="hero">
  <h1>📊 台灣主動式 ETF 低持股交集分析</h1>
  <p>分析日期：{ds} ／ 持股篩選：≤ {WEIGHT_THRESHOLD}% ／ 交集條件：≥ {MIN_ETF_OVERLAP} 檔ETF同時持有</p>
</div>

<div class="kpis">
  <div class="kpi"><div class="lbl">交集個股</div><div class="val">{total}</div></div>
  <div class="kpi"><div class="lbl">成長持股</div><div class="val" style="color:#1e7e45">{growing}</div></div>
  <div class="kpi"><div class="lbl">平均持股比</div><div class="val">{avg_w}%</div></div>
  <div class="kpi"><div class="lbl">分析ETF數</div><div class="val">{len(TARGET_ETFS)}</div></div>
</div>

<div class="card">
  <div class="card-hd">交集持股明細（綠底＝持股比例成長中）</div>
  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th>個股</th>
      <th style="text-align:center">交集數</th>
      <th>持有ETF</th>
      <th style="text-align:center">平均持股</th>
      <th style="text-align:center">持股變化</th>
      <th style="text-align:center">ETF全名</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  </div>
</div>

<p class="note">本報告由自動腳本產生 · 資料來源：TWSE OpenAPI / MoneyDJ · 僅供研究參考，不構成任何投資建議</p>
</div></body></html>"""

    path = REPORT_DIR / f"report_{ds}.html"
    path.write_text(html, encoding="utf-8")
    log.info(f"HTML報告：{path}")
    return path


def print_terminal_summary(df: pd.DataFrame):
    """在終端機印出重點摘要"""
    log.info("━"*52)
    log.info("【分析摘要】")
    log.info(f"  交集個股總數：{len(df)} 檔")
    if df["is_growing"].notna().any():
        grow = df[df["is_growing"] == True]
        log.info(f"  成長持股：{len(grow)} 檔")
        for _, r in grow.iterrows():
            chg = f"+{r['weight_change']:.2f}%" if r['weight_change'] else ""
            log.info(f"    ► {r['ticker']} {r['name']:10s}  持股 {r['avg_weight']:.2f}%  {chg}  [{r['etf_ids']}]")
    log.info("━"*52)


# ══════════════════════════════════════════════
#  主程式
# ══════════════════════════════════════════════

def main():
    setup_logging()
    log.info("╔══════════════════════════════════════════════╗")
    log.info("║  台灣ETF低持股交集分析  v2.0                 ║")
    log.info(f"║  {datetime.now():%Y-%m-%d %H:%M:%S}                       ║")
    log.info("╚══════════════════════════════════════════════╝")

    ds = datetime.now().strftime("%Y%m%d")

    current  = fetch_all()           # Step 1: 抓取
    previous = load_prev()           # Step 2: 載入歷史
    result   = analyze(current, previous)   # Step 3 & 4: 分析

    log.info("━"*52)
    log.info("【步驟5】輸出報告")
    save_csv(result, ds)
    html_path = save_html(result, ds)
    print_terminal_summary(result)

    log.info(f"✅ 完成！請用瀏覽器開啟：{html_path}")


if __name__ == "__main__":
    main()
