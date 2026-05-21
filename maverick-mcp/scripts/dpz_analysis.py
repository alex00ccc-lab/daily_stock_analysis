"""
DPZ (Domino's Pizza) 综合技术面 + 基本面分析

数据源 fallback 链: yfinance → Tiingo → yfinance 本地缓存 → N/A 降级
技术面和基本面独立获取，一个失败不影响另一个。
"""

import json
import os
import sys
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

# 加载 .env 文件
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if key not in os.environ:
                    os.environ[key] = val

# 确保 Windows 控制台使用 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

NA = "N/A"

# ── 0. 工具函数 ──────────────────────────────────────────────

def _na(val, fmt=".2f", prefix="$"):
    """格式化数值，None 返回 N/A"""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return NA
    return f"{prefix}{val:{fmt}}"

def _na_pct(val, fmt=".2f", signed=True):
    """格式化百分比"""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return NA
    sign = "+" if signed and val > 0 else ""
    return f"{sign}{val:{fmt}}%"

# ── 1. 多源价格历史获取 ─────────────────────────────────────

def fetch_yfinance(symbol):
    """首选: yfinance"""
    import yfinance as yf
    s = yf.Ticker(symbol)
    h = s.history(period="1y")
    if h.empty:
        raise ValueError("Empty data")
    return h, s

def fetch_tiingo(symbol):
    """备选: Tiingo API"""
    api_key = os.getenv("TIINGO_API_KEY") or os.getenv("TIINGO_API_TOKEN")
    if not api_key or "your_tiingo" in api_key.lower():
        raise ValueError("Tiingo API key 未配置")

    import requests
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    url = f"https://api.tiingo.com/tiingo/daily/{symbol}/prices"
    params = {"startDate": start_date, "endDate": end_date, "token": api_key, "format": "json"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("Tiingo returned empty data")

    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    # Tiingo returns both raw (open/high/low/close/volume) and adjusted
    # (adjOpen/adjHigh/adjLow/adjClose/adjVolume). Prefer adjusted, drop raw.
    has_adj = any(c.startswith("adj") for c in df.columns)
    if has_adj:
        # Keep only adjusted OHLCV + divCash/splitFactor, rename to standard names
        keep_cols = {}
        for c in df.columns:
            if c == "adjOpen":
                keep_cols[c] = "open"
            elif c == "adjHigh":
                keep_cols[c] = "high"
            elif c == "adjLow":
                keep_cols[c] = "low"
            elif c == "adjClose":
                keep_cols[c] = "close"
            elif c == "adjVolume":
                keep_cols[c] = "volume"
            elif c in ("divCash", "splitFactor"):
                keep_cols[c] = c.lower()
            # Skip raw OHLCV columns
        df = df[list(keep_cols.keys())].rename(columns=keep_cols)
    else:
        # Fallback: lowercase raw columns
        df.columns = [c.lower() for c in df.columns]
        keep = [c for c in df.columns if c in ("open", "high", "low", "close", "volume")]
        df = df[keep]
    # Drop rows with NaN close
    df = df.dropna(subset=["close"])
    return df, None

def fetch_yfinance_cache(symbol):
    """备选: yfinance 本地磁盘缓存"""
    import yfinance as yf

    # Try common cache locations
    cache_dirs = [
        Path.home() / "yfinance_cache",
        Path.home() / ".cache" / "yfinance",
        Path(os.getenv("TEMP", "/tmp")) / "yfinance",
    ]

    for cache_dir in cache_dirs:
        if not cache_dir.exists():
            continue
        # yfinance caches by ticker in parquet/pickle files
        for pattern in [f"**/{symbol}*.parquet", f"**/{symbol}*.pkl", f"**/{symbol}*.csv"]:
            for f in cache_dir.glob(pattern):
                try:
                    if f.suffix == ".parquet":
                        df = pd.read_parquet(f)
                    elif f.suffix == ".csv":
                        df = pd.read_csv(f, index_col=0, parse_dates=True)
                    else:
                        df = pd.read_pickle(f)
                    if not df.empty and "Close" in df.columns or "close" in df.columns:
                        return df, None
                except Exception:
                    continue

    raise ValueError("本地缓存中无该股票数据")

def fetch_price_history(symbol):
    """
    多源 fallback 获取价格历史。
    返回 (DataFrame, ticker_obj, source_name)
    """
    sources = [
        ("yfinance", fetch_yfinance),
        ("Tiingo", fetch_tiingo),
        ("yfinance 本地缓存", fetch_yfinance_cache),
    ]

    for name, fetcher in sources:
        try:
            print(f"  尝试数据源: {name}...", end=" ")
            hist, stock = fetcher(symbol)
            print("成功")
            return hist, stock, name
        except Exception as e:
            print(f"失败 ({type(e).__name__})")

    return None, None, None

# ── 2. 基本面获取 ───────────────────────────────────────────

def fetch_fundamentals(symbol, stock_obj):
    """
    独立获取基本面数据。
    Fallback: yfinance .info → Tiingo fundamentals → N/A
    """
    # 先尝试用已有的 stock 对象
    if stock_obj is not None:
        try:
            info = stock_obj.info
            if info and info.get("symbol"):
                return info, "yfinance (已有 session)"
        except Exception:
            pass

    # 回退：新建 yfinance session
    try:
        import yfinance as yf
        s = yf.Ticker(symbol)
        info = s.info
        if info and info.get("symbol"):
            return info, "yfinance (新建 session)"
    except Exception:
        pass

    # 回退：Tiingo fundamentals API
    api_key = os.getenv("TIINGO_API_KEY") or os.getenv("TIINGO_API_TOKEN")
    if api_key and "your_tiingo" not in api_key.lower():
        try:
            import requests
            url = f"https://api.tiingo.com/tiingo/fundamentals/{symbol}/daily"
            resp = requests.get(url, params={"token": api_key}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data:
                latest = data[-1] if isinstance(data, list) else data
                info = _parse_tiingo_fundamentals(latest, symbol)
                return info, "Tiingo"
        except Exception:
            pass

    return {}, None


def _parse_tiingo_fundamentals(meta, symbol):
    """将 Tiingo fundamentals 转换为类似 yfinance .info 的格式"""
    info = {"symbol": symbol}
    series = meta.get("series", {})
    info["marketCap"] = series.get("marketCap")
    info["enterpriseValue"] = series.get("enterpriseVal")
    info["trailingPE"] = series.get("peRatio")
    info["priceToBook"] = series.get("pbRatio")
    info["trailingPegRatio"] = series.get("pegRatio")
    info["totalRevenue"] = series.get("revenue")
    info["grossMargins"] = series.get("grossMargin")
    info["profitMargins"] = series.get("profitMargin")
    info["returnOnEquity"] = series.get("roe")
    info["returnOnAssets"] = series.get("roa")
    info["trailingEps"] = series.get("eps")
    info["bookValue"] = series.get("bookValuePerShare")
    info["dividendYield"] = series.get("dividendYield")
    info["debtToEquity"] = series.get("debtToEquity")
    info["currentRatio"] = series.get("currentRatio")
    info["freeCashflow"] = series.get("freeCashFlow")
    info["revenueGrowth"] = series.get("revenueGrowth")
    info["earningsGrowth"] = series.get("earningsGrowth")
    info["beta"] = series.get("beta")
    info["fiftyTwoWeekHigh"] = meta.get("high52Week") or series.get("high52Week")
    info["fiftyTwoWeekLow"] = meta.get("low52Week") or series.get("low52Week")
    info["shortPercentOfFloat"] = series.get("shortPercentOfFloat")
    info["recommendationKey"] = None  # Tiingo doesn't provide analyst ratings
    info["targetMeanPrice"] = None
    # Clean None values
    return {k: v for k, v in info.items() if v is not None}

# ── 3. 主流程 ───────────────────────────────────────────────

def _scalar(val):
    """安全提取标量值：如果是 Series 则取第一个元素"""
    if isinstance(val, pd.Series):
        return val.iloc[0] if len(val) > 0 else None
    return val

def main(symbol="DPZ"):
    print("=" * 70)
    print(f"  {symbol} — 综合技术面 + 基本面分析")
    print(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    # ─── 获取价格历史 ───
    print("\n[数据获取]")
    hist, stock, price_source = fetch_price_history(symbol)
    tech_available = hist is not None and not hist.empty
    price_source = price_source or "无可用数据源"

    # ─── 获取基本面 ───
    info, fund_source = fetch_fundamentals(symbol, stock)
    fund_available = info is not None and len(info) > 0
    fund_source = fund_source or "无可用数据源"

    print(f"\n  价格历史: {price_source}")
    print(f"  基本面:   {fund_source}")

    # ─── 技术分析 (如有数据) ───
    if tech_available:
        hist.columns = [c.lower() for c in hist.columns]
        # Remove duplicate index entries if any
        hist = hist[~hist.index.duplicated(keep="last")]

        try:
            import pandas_ta as ta
        except ImportError:
            import subprocess, sys
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas-ta", "-q"])
            import pandas_ta as ta

        df = hist.copy()

        def _safe_ta(fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                return None

        df["ema_21"] = _safe_ta(ta.ema, df["close"], length=min(21, len(df)-1))
        df["sma_50"] = _safe_ta(ta.sma, df["close"], length=min(50, len(df)-1))
        df["sma_200"] = _safe_ta(ta.sma, df["close"], length=min(200, len(df)-1))
        df["rsi"] = _safe_ta(ta.rsi, df["close"], length=min(14, len(df)-1))

        macd_slow = min(26, max(13, len(df) - 1))
        macd = _safe_ta(ta.macd, df["close"], fast=12, slow=macd_slow, signal=9)
        if macd is not None and not macd.empty:
            for col in macd.columns:
                col_lower = col.lower()
                if "macdh" in col_lower:
                    df["macd_hist"] = macd[col]
                elif "macds" in col_lower:
                    df["macd_signal"] = macd[col]
                elif "macd" in col_lower:
                    df["macd"] = macd[col]

        bb_len = min(20, max(5, len(df) - 1))
        bb = _safe_ta(ta.bbands, df["close"], length=bb_len, std=2)
        if bb is not None and not bb.empty:
            for col in bb.columns:
                col_lower = col.lower()
                if "bbu" in col_lower or col_lower.startswith("ub"):
                    df["bb_upper"] = bb[col]
                elif "bbm" in col_lower or col_lower.startswith("mb"):
                    df["bb_middle"] = bb[col]
                elif "bbl" in col_lower or col_lower.startswith("lb"):
                    df["bb_lower"] = bb[col]

        df["atr"] = _safe_ta(ta.atr, df["high"], df["low"], df["close"], length=min(14, max(2, len(df)-1)))
        df["volume_sma_20"] = _safe_ta(ta.sma, df["volume"], length=min(20, len(df)-1))

        stoch = _safe_ta(ta.stoch, df["high"], df["low"], df["close"])
        if stoch is not None and not stoch.empty:
            for col in stoch.columns:
                col_lower = col.lower()
                if "stochk" in col_lower:
                    df["stoch_k"] = stoch[col]
                elif "stochd" in col_lower:
                    df["stoch_d"] = stoch[col]

    # ── 4. 输出 ──────────────────────────────────────────────

    # 安全转换 latest 为标量 dict，避免 Series format 错误
    if tech_available:
        latest = {col: _scalar(df[col].iloc[-1]) for col in df.columns}
        prev = {col: _scalar(df[col].iloc[-2]) for col in df.columns}
        week_idx = -5 if len(df) >= 5 else 0
        month_idx = -21 if len(df) >= 21 else 0
        week_ago = {col: _scalar(df[col].iloc[week_idx]) for col in df.columns}
        month_ago = {col: _scalar(df[col].iloc[month_idx]) for col in df.columns}

    # ─── 行情概览 ───
    print(f"\n{'─' * 70}")
    print("  【行情概览】")
    print(f"{'─' * 70}")

    if tech_available:
        print(f"  最新收盘价:    ${latest['close']:.2f}")
        print(f"  52周最高:      ${_scalar(df['high'].max()):.2f}")
        print(f"  52周最低:      ${_scalar(df['low'].min()):.2f}")
        first_close = _scalar(df['close'].iloc[0])
        print(f"  52周涨跌幅:    {((latest['close'] / first_close) - 1) * 100:.2f}%")

        chg_1d = ((latest['close'] / prev['close']) - 1) * 100
        chg_5d = ((latest['close'] / week_ago['close']) - 1) * 100
        chg_1m = ((latest['close'] / month_ago['close']) - 1) * 100
        print(f"\n  日涨跌:        {_na_pct(chg_1d)}")
        print(f"  5日涨跌:       {_na_pct(chg_5d)}")
        print(f"  1月涨跌:       {_na_pct(chg_1m)}")
    else:
        print(f"  {NA} — 价格数据不可用 ({price_source})")

    # ─── 均线分析 ───
    print(f"\n{'─' * 70}")
    print("  【均线系统】")
    print(f"{'─' * 70}")

    if tech_available:
        for name, col, desc in [
            ("EMA 21", "ema_21", "短期趋势"),
            ("SMA 50", "sma_50", "中期趋势"),
            ("SMA 200", "sma_200", "长期趋势"),
        ]:
            val = latest.get(col)
            if pd.notna(val):
                diff_pct = ((latest["close"] / val) - 1) * 100
                above = "之上" if latest["close"] > val else "之下"
                print(f"  {name}: ${val:.2f}  |  价格在均线{above} {diff_pct:+.2f}%  ({desc})")
            else:
                print(f"  {name}: {NA}")

        if all(pd.notna(latest.get(c)) for c in ["ema_21", "sma_50", "sma_200"]):
            e21, s50, s200 = latest["ema_21"], latest["sma_50"], latest["sma_200"]
            if e21 > s50 > s200:
                alignment = "多头排列 (bullish) ↑"
            elif e21 < s50 < s200:
                alignment = "空头排列 (bearish) ↓"
            else:
                alignment = "交叉缠绕 (震荡)"
            print(f"\n  均线排列: {alignment}")
        else:
            print(f"\n  均线排列: {NA}")
    else:
        print(f"  {NA} — 无价格数据，无法计算均线")

    # ─── 动量指标 ───
    print(f"\n{'─' * 70}")
    print("  【动量指标】")
    print(f"{'─' * 70}")

    if tech_available:
        rsi_val = latest.get("rsi")
        if pd.notna(rsi_val):
            if rsi_val > 70:
                rsi_signal = "超买区域"
            elif rsi_val < 30:
                rsi_signal = "超卖区域"
            elif rsi_val > 50:
                rsi_signal = "偏多"
            else:
                rsi_signal = "偏空"
            print(f"  RSI (14):      {rsi_val:.1f}  ->  {rsi_signal}")
        else:
            print(f"  RSI (14):      {NA}")

        if pd.notna(latest.get("macd")) and pd.notna(latest.get("macd_signal")):
            macd_hist_val = latest.get("macd_hist")
            hist_dir = ""
            if macd_hist_val is not None and pd.notna(macd_hist_val):
                hist_dir = "转多" if macd_hist_val > 0 else "转空"
                hist_last = prev.get("macd_hist")
                if hist_last is not None and pd.notna(hist_last):
                    if macd_hist_val > 0 and hist_last <= 0:
                        hist_dir = "金叉信号 ✨"
                    elif macd_hist_val < 0 and hist_last >= 0:
                        hist_dir = "死叉信号"
            print(f"  MACD:          {latest['macd']:.2f}")
            print(f"  MACD Signal:   {latest['macd_signal']:.2f}")
            print(f"  MACD Hist:     {macd_hist_val:.2f}  ->  {hist_dir}")
        else:
            print(f"  MACD:          {NA}")

        if pd.notna(latest.get("stoch_k")) and pd.notna(latest.get("stoch_d")):
            k, d = latest["stoch_k"], latest["stoch_d"]
            if k > 80:
                kd_signal = "超买"
            elif k < 20:
                kd_signal = "超卖"
            else:
                kd_signal = "中性"
            print(f"  KD (Stochastic): K={k:.1f}, D={d:.1f}  ->  {kd_signal}")
        else:
            print(f"  KD (Stochastic): {NA}")
    else:
        print(f"  {NA} — 无价格数据，无法计算动量指标")

    # ─── 布林带 ───
    if tech_available and pd.notna(latest.get("bb_upper")):
        bb_width = ((latest["bb_upper"] - latest["bb_lower"]) / latest["bb_middle"]) * 100
        bb_pos = ((latest["close"] - latest["bb_lower"]) / (latest["bb_upper"] - latest["bb_lower"])) * 100
        print(f"\n  布林带宽度:    {bb_width:.1f}%")
        print(f"  价格位置:      带宽的 {bb_pos:.1f}% (0%=下轨, 100%=上轨)")

    # ─── ATR ───
    if tech_available and pd.notna(latest.get("atr")):
        atr_pct = (latest["atr"] / latest["close"]) * 100
        print(f"  ATR (14):      ${latest['atr']:.2f}  (占价格 {atr_pct:.2f}%)")

    # ─── 成交量 ───
    if tech_available and pd.notna(latest.get("volume_sma_20")):
        vol_ratio = latest["volume"] / latest["volume_sma_20"]
        vol_desc = "放量" if vol_ratio > 1.5 else ("缩量" if vol_ratio < 0.5 else "正常")
        print(f"\n  成交量 vs 20日均量: {vol_ratio:.2f}x  ->  {vol_desc}")

    # ─── 基本面 ───
    print(f"\n{'─' * 70}")
    print(f"  【基本面】")
    print(f"{'─' * 70}")

    if fund_available:
        fields = [
            ("市值", "marketCap", "B", 1e9, ".2f"),
            ("企业价值", "enterpriseValue", "B", 1e9, ".2f"),
            ("市盈率 (PE)", "trailingPE", "", 1, ".2f"),
            ("远期市盈率", "forwardPE", "", 1, ".2f"),
            ("PEG 比率", "pegRatio", "", 1, ".2f"),
            ("市净率 (PB)", "priceToBook", "", 1, ".2f"),
            ("市销率 (PS)", "priceToSalesTrailing12Months", "", 1, ".2f"),
            ("企业价值/EBITDA", "enterpriseToEbitda", "", 1, ".2f"),
            ("营收 (ttm)", "totalRevenue", "B", 1e9, ".2f"),
            ("毛利率", "grossMargins", "%", 0.01, ".1f"),
            ("净利润率", "profitMargins", "%", 0.01, ".1f"),
            ("ROE", "returnOnEquity", "%", 0.01, ".1f"),
            ("ROA", "returnOnAssets", "%", 0.01, ".1f"),
            ("每股收益", "trailingEps", "", 1, ".2f"),
            ("每股账面价值", "bookValue", "", 1, ".2f"),
            ("股息率", "dividendYield", "%", 0.01, ".2f"),
            ("负债权益比", "debtToEquity", "", 1, ".2f"),
            ("流动比率", "currentRatio", "", 1, ".2f"),
            ("自由现金流", "freeCashflow", "B", 1e9, ".2f"),
            ("营收增速 (YoY)", "revenueGrowth", "%", 0.01, ".1f"),
            ("盈利增速 (YoY)", "earningsGrowth", "%", 0.01, ".1f"),
            ("Beta", "beta", "", 1, ".2f"),
            ("52周最高", "fiftyTwoWeekHigh", "", 1, ".2f"),
            ("52周最低", "fiftyTwoWeekLow", "", 1, ".2f"),
            ("做空比例", "shortPercentOfFloat", "%", 0.01, ".1f"),
            ("分析师目标均价", "targetMeanPrice", "", 1, ".2f"),
            ("推荐评级", "recommendationKey", "", 1, ""),
        ]
        for label, key, unit, divisor, fmt in fields:
            val = info.get(key)
            if val is None:
                print(f"  {label}: {NA}")
                continue
            if unit == "" and fmt == "":
                print(f"  {label}: {val}")
            elif unit == "%" and fmt == ".1f":
                print(f"  {label}: {val / divisor:{fmt}}%")
            else:
                print(f"  {label}: {val / divisor:{fmt}}{unit}")
    else:
        if price_source == "Tiingo":
            fund_fail_note = " (yfinance 限流, Tiingo 基本面 API 仅限 Dow 30 免费)"
        else:
            fund_fail_note = ""
        print(f"  {NA} — 无法获取基本面数据 ({fund_source}{fund_fail_note})")

    # ─── 综合研判 ───
    print(f"\n{'─' * 70}")
    print("  【综合研判摘要】")
    print(f"{'─' * 70}")

    if tech_available or fund_available:
        bullish = []
        bearish = []

        if tech_available:
            if all(pd.notna(latest.get(c)) for c in ["ema_21", "sma_50", "sma_200"]):
                if latest["close"] > latest["sma_200"]:
                    bullish.append("价格在200日均线上方 (长期趋势偏多)")
                else:
                    bearish.append("价格在200日均线下方 (长期趋势偏空)")
                if latest["ema_21"] > latest["sma_50"]:
                    bullish.append("EMA21 > SMA50 (短期动能向上)")
                else:
                    bearish.append("EMA21 < SMA50 (短期动能向下)")

            rsi_val = latest.get("rsi")
            if pd.notna(rsi_val):
                if rsi_val > 50:
                    bullish.append(f"RSI={rsi_val:.1f} 在偏多区域")
                elif rsi_val < 50:
                    bearish.append(f"RSI={rsi_val:.1f} 在偏空区域")

            macd_h = latest.get("macd_hist")
            if pd.notna(macd_h):
                if macd_h > 0:
                    bullish.append("MACD柱状图为正 (动能偏多)")
                else:
                    bearish.append("MACD柱状图为负 (动能偏空)")

        if fund_available:
            pe = info.get("trailingPE")
            if pe is not None:
                if pe < 20:
                    bullish.append(f"PE={pe:.1f} 估值偏低")
                elif pe > 35:
                    bearish.append(f"PE={pe:.1f} 估值偏高")

            peg = info.get("pegRatio")
            if peg is not None:
                if peg < 1:
                    bullish.append(f"PEG={peg:.2f} < 1 (成长性被低估)")
                elif peg > 2:
                    bearish.append(f"PEG={peg:.2f} > 2 (成长性被高估)")

            rev_growth = info.get("revenueGrowth")
            if rev_growth is not None:
                if rev_growth > 0.1:
                    bullish.append(f"营收增长+{rev_growth*100:.1f}% (成长强劲)")
                elif rev_growth < 0:
                    bearish.append(f"营收下滑{rev_growth*100:.1f}%")

        print("\n  看多信号:")
        for s in bullish:
            print(f"    + {s}")
        if not bullish:
            print(f"    ({NA} — 无明显看多信号)")

        print("\n  看空信号:")
        for s in bearish:
            print(f"    - {s}")
        if not bearish:
            print(f"    ({NA} — 无明显看空信号)")

        total = len(bullish) + len(bearish)
        if total > 0:
            bull_ratio = len(bullish) / total * 100
            print(f"\n  综合倾向: {bull_ratio:.0f}% 偏多 / {100-bull_ratio:.0f}% 偏空")
            if bull_ratio >= 65:
                print("  整体研判: 技术面偏多，基本面信号积极")
            elif bull_ratio <= 35:
                print("  整体研判: 技术面偏空，需谨慎")
            else:
                print("  整体研判: 信号混合，建议观望或轻仓")
        else:
            print(f"\n  综合倾向: {NA} (信号不足，无法判断)")
    else:
        print(f"\n  {NA} — 技术面和基本面数据均不可用")
        print(f"  价格数据源: {price_source}")
        print(f"  基本面数据源: {fund_source}")

    # ─── 关键价位 ───
    print(f"\n{'─' * 70}")
    print("  【关键价位参考】")
    print(f"{'─' * 70}")

    if tech_available:
        if pd.notna(latest.get("sma_200")):
            print(f"  长期支撑: SMA200 ${latest['sma_200']:.2f}")
        if pd.notna(latest.get("sma_50")):
            print(f"  中期支撑: SMA50 ${latest['sma_50']:.2f}")

    if fund_available:
        high_52 = info.get("fiftyTwoWeekHigh")
        low_52 = info.get("fiftyTwoWeekLow")
        print(f"  52周高点: {_na(high_52)} (阻力)")
        print(f"  52周低点: {_na(low_52)} (强支撑)")
    elif not tech_available:
        print(f"  {NA}")

    # ─── 数据源说明 ───
    print(f"\n{'─' * 70}")
    print("  【数据来源】")
    print(f"{'─' * 70}")
    print(f"  价格数据: {price_source}")
    print(f"  基本面:   {fund_source}")

    print(f"\n  免责声明: 以上分析仅供参考和教育目的，不构成投资建议。")
    print(f"            过去业绩不代表未来表现。投资决策请咨询专业金融顾问。")
    print("=" * 70)


if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "DPZ"
    main(symbol)
