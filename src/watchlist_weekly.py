# -*- coding: utf-8 -*-
"""watchlist_weekly — 周报第②③段的装配层：宏观指标周环比 + 新闻头条（0 LLM 数字）。

职责：
  - assemble_macro(macro_history)：把一周 ``macro.json`` 序列压成「每指标周环比」，
    全部数字由本模块算，LLM 不参与。
  - fetch_news(days)：复用 DSA 的 ``search_service.SearchService`` 抓本周头条，
    无 key / 依赖缺失时优雅返回空 list（周报照出，只缺新闻段）。

P0 纪律：本模块只产出「名字 + 方向 + 数字」，不调用任何 LLM；文字归纳在 watchlist_llm 层。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 新闻搜索 key 环境变量（逗号分隔，可多 key 负载均衡，与 search_service 对齐）
_NEWS_KEY_ENVVARS = {
    "bocha": "BOCHA_API_KEYS",
    "tavily": "TAVILY_API_KEYS",
    "serpapi": "SERPAPI_API_KEYS",
    "brave": "BRAVE_API_KEYS",
    "minimax": "MINIMAX_API_KEYS",
    "anspire": "ANSPIRE_API_KEYS",
}


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN（yfinance 周末返回的 NaN-close bar 会一路传播成 "+nan%"）→ None，由上层 if c is not None 过滤
    return None if f != f else f


def assemble_macro(macro_history: list[tuple[str, dict]]) -> list[dict]:
    """把一周 macro.json 序列压成「每指标周环比」。

    ``macro_history``：``[(date_str, macro_dict), ...]`` 升序。
    以**最新一日**的符号集合为准，逐指标用「最早 vs 最新」的价格算周环比（百分比）。
    任一价格缺失 → ``week_change_pct=None``（渲染层标注「无数据」，不编造）。
    """
    if not macro_history:
        return []
    first_date, first = macro_history[0]
    last_date, last = macro_history[-1]

    out: list[dict] = []
    for sym, info in last.items():
        if not isinstance(info, dict):
            continue
        name = info.get("name") or sym
        latest_price = _num(info.get("price"))
        first_info = first.get(sym) if isinstance(first.get(sym), dict) else {}
        first_price = _num(first_info.get("price"))
        chg = None
        if latest_price is not None and first_price is not None and first_price != 0:
            chg = round((latest_price / first_price - 1.0) * 100.0, 2)
        out.append({
            "symbol": sym,
            "name": name,
            "price": latest_price,
            "date": info.get("date") or last_date,
            "prev_price": first_price,
            "week_change_pct": chg,
        })
    return out


def _split_keys(envvar: str) -> list[str]:
    return [k.strip() for k in os.getenv(envvar, "").split(",") if k.strip()]


def _news_keys() -> dict[str, list[str]]:
    return {name: _split_keys(env) for name, env in _NEWS_KEY_ENVVARS.items()}


def fetch_news(days: int = 7) -> list[dict]:
    """抓本周市场/宏观头条，返回 ``[{title, snippet, url, source, date}]``。

    无任何 key / search_service 依赖缺失 / 网络失败 → 返回空 list（优雅降级）。
    """
    keys = _news_keys()
    if not any(keys.values()):
        logger.info("无任何新闻搜索 key（BOCHA/TAVILY/SERPAPI/...），跳过新闻段")
        return []

    try:
        from src.search_service import SearchService
    except Exception as e:  # noqa: BLE001 — 依赖缺失（newspaper/tenacity 等）降级
        logger.warning("search_service 不可用，跳过新闻段: %s", e)
        return []

    try:
        svc = SearchService(
            bocha_keys=keys["bocha"],
            tavily_keys=keys["tavily"],
            serpapi_keys=keys["serpapi"],
            brave_keys=keys["brave"],
            minimax_keys=keys["minimax"],
            anspire_keys=keys["anspire"],
            news_max_age_days=max(1, days),
            news_strategy_profile="medium",  # medium = 7 天窗口
        )
        resp = svc.search_stock_news(
            stock_code="market",
            stock_name="美股市场",
            max_results=8,
            focus_keywords=["美股 市场 宏观 本周 重大新闻"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("新闻搜索失败，跳过新闻段: %s", e)
        return []

    if not resp or not getattr(resp, "success", False) or not resp.results:
        return []

    out: list[dict] = []
    for r in resp.results[:8]:
        out.append({
            "title": r.title or "",
            "snippet": (r.snippet or "")[:300],
            "url": r.url or "",
            "source": r.source or "",
            "date": r.published_date or "",
        })
    return out
