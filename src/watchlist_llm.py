# -*- coding: utf-8 -*-
"""watchlist_llm — 自选股周层唯一的 LLM 调用点：只写文字，不输出任何数字（P0）。

P0 纪律的「LLM 零出数」在这里由三层机械保证：
  1. prompt 写死规则：不得输出任何价格/点位/比率/分数；
  2. 输出 schema 只有 4 个字段（symbol/thesis_comment/risk_note/action_hint），
     且 ``action_hint`` 受枚举约束，无数字字段可落；
  3. 解析后对文字做二次数字剥离（defensive，LLM 违规也进不了下游）。

受 deepseek_peak_guard 约束：北京高峰时段默认跳过（除非 force=True），
返回空结果，让调用方降级为「纯数字摘要」。

依赖：``requests``（直连 DeepSeek HTTP API，不引入 LiteLLM 那套多协议路由，
保持本管线零依赖、零 token 泄漏面）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import requests

from src.deepseek_peak_guard import should_skip_llm

logger = logging.getLogger(__name__)

_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
_ALLOWED_HINTS = {"watch", "consider_entry", "wait", "avoid"}

_SYSTEM_PROMPT = (
    "你是自选股买入条件复核助手。用户已用 Python 机械计算过所有技术指标与闸门判定，"
    "你只负责用自然语言解释「为什么这个标的值得/不值得现在关注」以及「下一步该盯什么」。\n"
    "硬性规则（违反即视为失败）：\n"
    "1. 不得输出任何价格、点位、均线数值、百分比、比率、分数、日期；\n"
    "2. 不得复述或引用输入里的任何数字；\n"
    "3. 所有数字由系统计算并在下游附加，你只写定性文字；\n"
    "4. 严格按给定 JSON schema 输出，不要输出 JSON 以外的任何内容。"
)


def _sanitize(text: Any) -> str:
    """剥离文字里的数字/货币符号（防御层：LLM 违规也进不了下游）。"""
    if text is None:
        return ""
    s = str(text)
    s = re.sub(r"-?\d+(?:\.\d+)?%?", "", s)   # 数字与百分比
    s = re.sub(r"[¥$€]", "", s)                # 货币符号
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_json(text: str) -> Optional[dict]:
    """从 LLM 回复里抠出 JSON 对象（容忍前后噪音 / markdown 代码块）。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def generate_prose(
    passing_symbols: list[dict],
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    force: bool = False,
) -> list[dict]:
    """对通过闸门的标的做一次批量 DeepSeek 文字复核。

    返回 ``[{symbol, thesis_comment, risk_note, action_hint}]``，字段全为定性文字，
    ``action_hint`` ∈ {watch, consider_entry, wait, avoid}。
    高峰跳过 / 无 key / 无标的 / 解析失败 → 返回空 list（调用方降级为数字摘要）。
    """
    if not passing_symbols:
        return []

    api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.warning("DEEPSEEK_API_KEY 未配置，跳过 LLM 文字复核")
        return []

    if should_skip_llm(force=force):
        logger.info("LLM call deferred: Beijing peak hours, use --force to override")
        return []

    model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # 给 LLM 的上下文：只给标的代码 + 定性结论（数字由系统下游附加，不送进 prompt 让它复述）
    symbol_blob = [
        {"symbol": r["symbol"], "summary": r.get("summary", ""), "resonance": r.get("resonance_overall", "")}
        for r in passing_symbols
    ]
    user_prompt = (
        "以下标的已通过 Python 闸门（技术回调到位 + 距前高非贴顶）。请针对每个标的，"
        "用中文写一段定性复核（不要出现任何数字）：\n"
        + json.dumps(symbol_blob, ensure_ascii=False)
        + "\n\n严格按以下 schema 输出 JSON 数组（仅此 4 个字段，无数字）："
        ' {"symbols": [{"symbol": "...", "thesis_comment": "...", "risk_note": "...", '
        '"action_hint": "watch|consider_entry|wait|avoid"}]}'
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(
            _DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001 — LLM 调用失败降级，不阻断日层
        logger.warning("DeepSeek prose call failed: %s", e)
        return []

    data = _extract_json(content)
    items = (data or {}).get("symbols") if isinstance(data, dict) else None
    if not isinstance(items, list):
        logger.warning("DeepSeek 输出无法解析为 {symbols:[...]}，降级为数字摘要")
        return []

    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("symbol"):
            continue
        hint = it.get("action_hint", "watch")
        if hint not in _ALLOWED_HINTS:
            hint = "watch"
        out.append({
            "symbol": str(it["symbol"]).upper(),
            "thesis_comment": _sanitize(it.get("thesis_comment")),
            "risk_note": _sanitize(it.get("risk_note")),
            "action_hint": hint,
        })
    return out


# ── 周报第④段：宏观政策总结 + 个股复核（单次批量请求，纯文字零数字）─────
_WEEKLY_SYSTEM_PROMPT = (
    "你是自选股周度复核助手。用户已用 Python 机械计算过所有技术指标、闸门判定与宏观指标周环比，"
    "你只负责用自然语言做三段定性归纳：宏观政策面解读、本周新闻要点、以及每个候选标的的买入复核。\n"
    "硬性规则（违反即视为失败）：\n"
    "1. 不得输出任何价格、点位、均线数值、百分比、比率、分数、日期；\n"
    "2. 不得复述或引用输入里的任何数字；\n"
    "3. 所有数字由系统计算并在下游展示，你只写定性文字；\n"
    "4. 严格按给定 JSON schema 输出，不要输出 JSON 以外的任何内容。"
)


def _direction_zh(pct: Optional[float]) -> str:
    """把周环比数值转成定性方向词（供 LLM 理解，不泄漏具体数字）。"""
    if pct is None:
        return "数据缺失"
    if pct > 0.05:
        return "上涨"
    if pct < -0.05:
        return "下跌"
    return "基本持平"


def generate_weekly_prose(
    passing_symbols: list[dict],
    macro_items: Optional[list[dict]] = None,
    news_items: Optional[list[dict]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    force: bool = False,
) -> Optional[dict]:
    """周报第④段唯一一次 DeepSeek 调用：宏观政策总结 + 新闻要点 + 个股复核。

    输入只送定性方向（宏观指标的方向词、新闻标题、标的代码+定性结论），
    不送任何精确数字；输出三层 schema 全为文字，解析后二次剥离数字兜底。
    返回 ``{"macro_policy": str, "news_brief": str, "symbols": [{symbol, thesis_comment, risk_note, action_hint}]}``；
    高峰跳过 / 无 key / 解析失败 → 返回 None（调用方降级为纯数字周报）。
    """
    macro_items = macro_items or []
    news_items = news_items or []

    api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.warning("DEEPSEEK_API_KEY 未配置，跳过周报 LLM 归纳")
        return None

    if should_skip_llm(force=force):
        logger.info("LLM call deferred: Beijing peak hours, use --force to override")
        return None

    model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    macro_lines = [f"- {m.get('name', m.get('symbol', '?'))}：{_direction_zh(m.get('week_change_pct'))}" for m in macro_items]
    news_lines = [f"- {n.get('title', '')}" for n in news_items[:8]]
    symbol_blob = [
        {"symbol": r["symbol"], "summary": r.get("summary", ""), "resonance": r.get("resonance_overall", "")}
        for r in passing_symbols
    ]

    user_prompt = (
        "请按以下三部分输出周报定性归纳（不要出现任何数字）：\n\n"
        "【宏观指标方向】（系统已算好周环比，你只看方向写政策/流动性解读）\n"
        + ("\n".join(macro_lines) if macro_lines else "（本周无宏观数据）")
        + "\n\n【本周新闻标题】\n"
        + ("\n".join(news_lines) if news_lines else "（本周无新闻）")
        + "\n\n【通过闸门的候选标的】\n"
        + json.dumps(symbol_blob, ensure_ascii=False)
        + "\n\n严格按以下 schema 输出 JSON 对象（macro_policy/news_brief 为字符串，"
          "symbols 为数组，每个元素仅 4 个字段且 action_hint 取枚举值，全程无数字）："
          ' {"macro_policy": "...", "news_brief": "...", '
          '"symbols": [{"symbol": "...", "thesis_comment": "...", "risk_note": "...", '
          '"action_hint": "watch|consider_entry|wait|avoid"}]}'
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _WEEKLY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(
            _DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001 — LLM 失败降级，不阻断周报
        logger.warning("DeepSeek weekly prose call failed: %s", e)
        return None

    data = _extract_json(content)
    if not isinstance(data, dict):
        logger.warning("DeepSeek 周报输出无法解析为 JSON 对象，降级为纯数字周报")
        return None

    items = data.get("symbols")
    symbols = []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict) or not it.get("symbol"):
                continue
            hint = it.get("action_hint", "watch")
            if hint not in _ALLOWED_HINTS:
                hint = "watch"
            symbols.append({
                "symbol": str(it["symbol"]).upper(),
                "thesis_comment": _sanitize(it.get("thesis_comment")),
                "risk_note": _sanitize(it.get("risk_note")),
                "action_hint": hint,
            })

    return {
        "macro_policy": _sanitize(data.get("macro_policy")),
        "news_brief": _sanitize(data.get("news_brief")),
        "symbols": symbols,
    }
