# -*- coding: utf-8 -*-
"""market_data_reader — 从 PUBLIC 仓库 market-data-collector 读已算好的指标 JSON（不重新抓 quote）。

架构依据（详见 holdings-briefing 方案 Part 4）：
  market-data-collector 是 PUBLIC 仓库，``market_data/config/watchlist.json`` 与
  ``market_data/data/{YYYY-MM-DD}/indicators/{SYMBOL}.json`` 随抓取管线每日推送。
  自选股不需要自己再抓一遍数据——直接读这条公开管道即可。

职责边界：
  - 只读、只解析、只缓存原始字节到本地（CI 里 6h TTL 降低重复网络请求）
  - 不做任何技术指标计算（指标是 holdings-briefing 的 indicators.py 算好发布的）
  - 不调用任何 LLM

数据源基准 URL::
    https://raw.githubusercontent.com/alex00ccc-lab/market-data-collector/{branch}/...

分支：默认 ``master``（对齐 CI 里 ``git pull origin master``），可用 ``MARKET_DATA_BRANCH`` 覆盖。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

_REPO = "alex00ccc-lab/market-data-collector"
_DEFAULT_BRANCH = os.getenv("MARKET_DATA_BRANCH", "master")
_RAW_BASE = f"https://raw.githubusercontent.com/{_REPO}/"

# 本地缓存根（CI 内为仓库内 cache/，可用环境变量覆盖到 /tmp 避免污染）
_CACHE_ROOT = Path(os.getenv("WATCHLIST_CACHE_DIR", str(Path(__file__).resolve().parent.parent / "cache" / "watchlist")))
_CACHE_ROOT.mkdir(parents=True, exist_ok=True)

_REQUEST_TIMEOUT = 20
# 自选股清单缓存 TTL（秒）：6 小时。清单变动频率低，短期缓存省一次网络往返。
_WATCHLIST_TTL = 6 * 3600


def _http_get(url: str) -> Optional[bytes]:
    """GET 一个公开 URL，返回 bytes；404/网络异常返回 None（调用方优雅降级）。"""
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content
    except Exception as e:  # noqa: BLE001 — 外部网络调用，任何异常都降级
        logger.warning("GET %s failed: %s", url, e)
        return None


def _fetch_json(url: str) -> Optional[dict]:
    raw = _http_get(url)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("JSON parse failed for %s: %s", url, e)
        return None


# ── 自选股清单 ──────────────────────────────────────────────────────────────
def fetch_watchlist_symbols(branch: Optional[str] = None) -> list[str]:
    """读公开 config/watchlist.json，返回 symbols 列表（大写，去重，保序）。

    单一事实源：自选股清单不再由 DSA 的 GitHub Variable/Secret 维护，
    改由 holdings-briefing 的 ``market_data/config/watchlist.json`` 一处维护。
    """
    branch = branch or _DEFAULT_BRANCH
    cache_file = _CACHE_ROOT / f"watchlist_{branch}.json"
    if cache_file.exists():
        age = datetime.now().timestamp() - cache_file.stat().st_mtime
        if age < _WATCHLIST_TTL:
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                syms = [s.get("symbol") for s in data.get("symbols", []) if s.get("symbol")]
                if syms:
                    return [s.upper() for s in syms]
            except Exception:  # noqa: BLE001 — 缓存坏了就重取
                pass

    data = _fetch_json(f"{_RAW_BASE}{branch}/config/watchlist.json")
    if not data:
        # 分支回退：master 404 时试 main（部分 fork 用 main）
        alt = "main" if branch == "master" else "master"
        data = _fetch_json(f"{_RAW_BASE}{alt}/config/watchlist.json")
        branch = alt
    if not data:
        logger.warning("watchlist.json 无法获取（%s），返回空列表", branch)
        return []

    try:
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 — 缓存写失败不影响功能
        pass

    return [s.get("symbol").upper() for s in data.get("symbols", []) if s.get("symbol")]


# ── 单标的指标 JSON ────────────────────────────────────────────────────────
def _candidate_dates(days_back: int = 10) -> list[str]:
    """候选日期（BJT，从今天往前 days_back 天），交易日数据必在其中之一。

    美股收盘 16:00 ET ≈ 次日 04:00 BJT，故本地 fetch 通常当天凌晨后写入当日目录；
    这里按 BJT「今天」起回退，最坏覆盖到上一交易周。
    """
    now = datetime.now(timezone(timedelta(hours=8)))
    out = []
    for i in range(days_back + 1):
        d = now.date() - timedelta(days=i)
        out.append(d.isoformat())
    return out


def fetch_indicators(symbol: str, branch: Optional[str] = None, days_back: int = 10) -> Optional[dict]:
    """读该标的「最新日期」的 indicators JSON，找不到返回 None。

    用日期回退（不从 GitHub API 列目录，避免 rate limit）：依次尝试最近
    ``days_back`` 个 BJT 日期目录下的 ``{SYMBOL}.json``，命中即返回。
    """
    branch = branch or _DEFAULT_BRANCH
    sym = symbol.upper()  # 文件名与 watchlist symbol 一致（含港股点号如 1888.HK.json）
    for date_s in _candidate_dates(days_back):
        url = f"{_RAW_BASE}{branch}/data/{date_s}/indicators/{sym}.json"
        data = _fetch_json(url)
        if data is not None:
            return data
    logger.info("no indicators found for %s within %d days", symbol, days_back)
    return None


def fetch_all_indicators(symbols: list[str], branch: Optional[str] = None, days_back: int = 10) -> dict[str, Optional[dict]]:
    """批量读指标：{symbol: indicators dict | None}。None 表示该标的最近无数据。"""
    return {s: fetch_indicators(s, branch=branch, days_back=days_back) for s in symbols}


# ── 基本面（估值灯：PE/PB/股息/市值）────────────────────────────────────────
def fetch_fundamentals(symbol: str, branch: Optional[str] = None, days_back: int = 10) -> Optional[dict]:
    """读该标的「最新日期」的 fundamentals JSON（pe_ratio/pb_ratio/dividend_yield/market_cap），找不到返回 None。

    基本面是 weekly 抓取（每周六 08:00 BJT 由 fetch-weekly 写 ``data/{date}/fundamentals/``），
    与指标不同——只有每周一个快照。故用日期回退（同 indicators）定位最近一次命中；
    新加入自选股的标的在下一轮 weekly 抓取覆盖前会返回 None（调用方优雅降级为「待回填」）。
    """
    branch = branch or _DEFAULT_BRANCH
    sym = symbol.upper()  # 文件名与 watchlist symbol 一致
    for date_s in _candidate_dates(days_back):
        url = f"{_RAW_BASE}{branch}/data/{date_s}/fundamentals/{sym}.json"
        data = _fetch_json(url)
        if data is not None:
            return data
    logger.info("no fundamentals found for %s within %d days", symbol, days_back)
    return None


def fetch_all_fundamentals(symbols: list[str], branch: Optional[str] = None, days_back: int = 10) -> dict[str, Optional[dict]]:
    """批量读基本面：{symbol: fundamentals dict | None}。None 表示最近无快照。"""
    return {s: fetch_fundamentals(s, branch=branch, days_back=days_back) for s in symbols}


# ── 期权墙（max pain / call wall / put wall / ATM IV / IV-HV）────────────────
def fetch_options(symbol: str, branch: Optional[str] = None, days_back: int = 10) -> Optional[dict]:
    """读该标的「最新日期」的 options JSON（max_pain/call_wall/put_wall/atm_iv/iv_hv_gap），找不到返回 None。

    期权墙是 weekly 抓取（周六 08:00 BJT 由 fetch-weekly 写 ``data/{date}/options/``），
    与 fundamentals 同频。用日期回退定位最近一次命中；新标的或抓取失败返回 None（调用方降级「待回填」）。
    """
    branch = branch or _DEFAULT_BRANCH
    sym = symbol.upper()
    for date_s in _candidate_dates(days_back):
        url = f"{_RAW_BASE}{branch}/data/{date_s}/options/{sym}.json"
        data = _fetch_json(url)
        if data is not None:
            return data
    logger.info("no options found for %s within %d days", symbol, days_back)
    return None


def fetch_all_options(symbols: list[str], branch: Optional[str] = None, days_back: int = 10) -> dict[str, Optional[dict]]:
    """批量读期权墙：{symbol: options dict | None}。None 表示最近无快照。"""
    return {s: fetch_options(s, branch=branch, days_back=days_back) for s in symbols}


# ── 周度历史序列 ────────────────────────────────────────────────────────────
def fetch_indicators_history(symbol: str, branch: Optional[str] = None, days: int = 8) -> list[tuple[str, dict]]:
    """读该标的近 ``days`` 个自然日里**所有命中日期**的指标序列。

    返回 ``[(date_str, indicators_dict), ...]``，按日期**升序**（最旧在前、最新在后）。
    命中不到任何一天 → 返回空 list（调用方跳过该标的，优雅降级）。
    用于周报的「一周完整数据」：周涨跌 / 周高低 / 周量比 / 本周通过闸门天数。
    """
    branch = branch or _DEFAULT_BRANCH
    sym = symbol.upper()
    out: list[tuple[str, dict]] = []
    for date_s in reversed(_candidate_dates(days)):
        url = f"{_RAW_BASE}{branch}/data/{date_s}/indicators/{sym}.json"
        data = _fetch_json(url)
        if data is not None:
            out.append((date_s, data))
    return out


def fetch_macro_history(branch: Optional[str] = None, days: int = 8) -> list[tuple[str, dict]]:
    """读近 ``days`` 个自然日里**所有命中日期**的 ``macro.json`` 序列（升序）。

    返回 ``[(date_str, macro_dict), ...]``；macro_dict 形如 ``{"^VIX": {name, price, date, change_pct}, ...}``。
    用于周报的「重大指标周环比」。
    """
    branch = branch or _DEFAULT_BRANCH
    out: list[tuple[str, dict]] = []
    for date_s in reversed(_candidate_dates(days)):
        url = f"{_RAW_BASE}{branch}/data/{date_s}/macro.json"
        data = _fetch_json(url)
        if data is not None:
            out.append((date_s, data))
    return out
