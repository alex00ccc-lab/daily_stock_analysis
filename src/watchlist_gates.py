# -*- coding: utf-8 -*-
"""watchlist_gates — 自选股「是否具备买入条件」的纯 Python 闸门层（0 LLM）。

这是 P0 纪律的机械实现：所有数字（价格、点位、偏离%、量比、共振结论）都由
本模块从已发布的指标 JSON 里**读出来算**，LLM 不参与任何数字输出。

移植自 holdings-briefing 的入场体系（单一判定标准，与持仓系统对齐）：
  - 三灯层  src/entry_timing.py       —— 估值闸 + 技术闸 + 距前高
  - 门禁轨  src/position_quality.py   —— B2 追高闸(chasing +8%) / B3 AVWAP 破位(1.5×量)

消费（读 JSON，不重算）：``resonance.five_q``、``ma``、``bollinger``、``fibonacci``、
``vwap.avwap_60d``、``volume.ratio``、``rsi``、``trend_strength``。
估值闸的 PE 不在日指标里（在周度 fundamentals/），故默认 degraded（跳过）。

阈值单一事实源见模块底部 ``_VAL/_TECH/_DIST/_B2/_B3``。
"""

from __future__ import annotations

from typing import Any, Optional

# ── 阈值（对齐 entry_timing.py / position_quality.py 铁律值）────────────────
_VAL = {"green_max": 15.0, "yellow_max": 25.0}
_TECH = {
    "green_boll": 0.5,   # boll.position < 0.5 且 close ≤ ma20 → 回调到位
    "red_boll": 0.8,     # boll.position ≥ 0.8 → 追高区
    "red_ma20_dev": 0.03,  # close 高于 ma20 > 3% → 追高
}
_DIST = {"green_max": -0.10, "yellow_max": -0.03}
_B2 = {"chasing_dev_pct": 8.0}      # avwap_dev_pct > 8% → chasing
_B3 = {"avwap_break_vol_mult": 1.5}  # avwap_dev<0 且 vol_ratio>1.5 → 破位

_ICON = {"green": "🟢", "yellow": "🟡", "red": "🔴", "degraded": "⚪"}


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN（yfinance 周末返回的 NaN-close bar 会一路传播成 "+nan%"）→ None，由上层 if c is not None 过滤
    return None if f != f else f


def _gate_valuation(pe: Optional[float]) -> dict:
    if pe is None:
        return {"name": "估值闸", "verdict": "degraded", "input": "PE=缺失", "reason": "日指标无 PE，估值闸跳过"}
    if pe < _VAL["green_max"]:
        return {"name": "估值闸", "verdict": "green", "input": f"PE={pe:.2f}", "reason": f"PE<{_VAL['green_max']:.0f} 有安全边际"}
    if pe < _VAL["yellow_max"]:
        return {"name": "估值闸", "verdict": "yellow", "input": f"PE={pe:.2f}", "reason": "PE 合理但不便宜"}
    return {"name": "估值闸", "verdict": "red", "input": f"PE={pe:.2f}", "reason": f"PE≥{_VAL['yellow_max']:.0f} 偏贵"}


def _gate_technical(ind: dict) -> dict:
    boll_pos = _num((ind.get("bollinger") or {}).get("position"))
    close = _num(ind.get("close"))
    ma20 = _num((ind.get("ma") or {}).get("ma20"))
    if boll_pos is None or close is None or ma20 is None:
        return {"name": "技术闸", "verdict": "degraded", "input": "指标=缺失", "reason": "布林/均线缺失"}
    dev = (close - ma20) / ma20
    if boll_pos < _TECH["green_boll"] and close <= ma20:
        return {"name": "技术闸", "verdict": "green", "input": f"boll={boll_pos:.2f} close≤ma20", "reason": "布林位置<0.5 且不高于 ma20，回调到位"}
    if boll_pos >= _TECH["red_boll"] or dev > _TECH["red_ma20_dev"]:
        return {"name": "技术闸", "verdict": "red", "input": f"boll={boll_pos:.2f} dev={dev*100:+.1f}%", "reason": "贴布林上轨/偏离 ma20 超 3%，追高区"}
    return {"name": "技术闸", "verdict": "yellow", "input": f"boll={boll_pos:.2f} dev={dev*100:+.1f}%", "reason": "布林中位，可等待"}


def _gate_distance(ind: dict) -> dict:
    close = _num(ind.get("close"))
    high = _num((ind.get("fibonacci") or {}).get("high"))
    if close is None or high is None:
        return {"name": "距前高", "verdict": "degraded", "input": "高点=缺失", "reason": "fibonacci.high 缺失"}
    dist = (close - high) / high
    if dist <= _DIST["green_max"]:
        return {"name": "距前高", "verdict": "green", "input": f"{(close-high)/high*100:.1f}%", "reason": "距前高 ≤ -10%，已明显回调"}
    if dist <= _DIST["yellow_max"]:
        return {"name": "距前高", "verdict": "yellow", "input": f"{(close-high)/high*100:.1f}%", "reason": "温和回调（-10%~-3%）"}
    return {"name": "距前高", "verdict": "red", "input": f"{(close-high)/high*100:.1f}%", "reason": "贴前高，追高风险"}


def _avwap_dev_pct(ind: dict) -> Optional[float]:
    """(close/avwap_60d − 1)×100。avwap 缺失返回 None（优雅降级）。"""
    close = _num(ind.get("close"))
    avwap = _num((ind.get("vwap") or {}).get("avwap_60d"))
    if close is None or avwap is None or avwap <= 0:
        return None
    return (close / avwap - 1.0) * 100.0


def evaluate(symbol: str, indicators: Optional[dict]) -> dict:
    """对单个标的跑全部闸门，返回结构化判定（含中间值，供通知/周报渲染）。"""
    ind = indicators or {}
    g_val = _gate_valuation(None)  # 日层无 PE，恒 degraded（除非上游回填）
    g_tech = _gate_technical(ind)
    g_dist = _gate_distance(ind)

    avwap_dev = _avwap_dev_pct(ind)
    vol_ratio = _num((ind.get("volume") or {}).get("ratio"))
    chasing = avwap_dev is not None and avwap_dev > _B2["chasing_dev_pct"]
    avwap_break = (avwap_dev is not None and avwap_dev < 0
                   and vol_ratio is not None and vol_ratio > _B3["avwap_break_vol_mult"])

    five_q = (ind.get("resonance") or {}).get("five_q", {})
    overall = (ind.get("resonance") or {}).get("overall", "")

    # ── 买入条件（自选股口径：技术+位置回调到位，估值未知不挡，追高/破位一票否决）──
    failed = []
    if g_tech["verdict"] != "green":
        failed.append("技术闸未到位")
    if g_dist["verdict"] == "red":
        failed.append("贴前高追高风险")
    if g_val["verdict"] == "red":
        failed.append("估值偏贵")
    if chasing:
        failed.append(f"追高(高于 AVWAP {avwap_dev:+.1f}%)")
    if avwap_break:
        failed.append(f"AVWAP 放量破位(量比 {vol_ratio:.1f}×)")
    if overall == "bearish" or overall == "mildly_bearish":
        failed.append(f"五问共振 {overall}")

    passed = not failed

    gates = [g_val, g_tech, g_dist]
    if passed:
        summary = "✅ 具备买入条件：技术回调到位 + 距前高非贴顶"
    else:
        summary = "❌ 暂不具备：" + "；".join(failed)

    return {
        "symbol": symbol,
        "passed": passed,
        "failed": failed,
        "summary": summary,
        "gates": gates,
        "five_q": five_q,
        "resonance_overall": overall,
        "trend_strength": ind.get("trend_strength", ""),
        "close": _num(ind.get("close")),
        "ma20": _num((ind.get("ma") or {}).get("ma20")),
        "boll_position": _num((ind.get("bollinger") or {}).get("position")),
        "rsi": _num((ind.get("rsi") or {}).get("value")),
        "fib_high": _num((ind.get("fibonacci") or {}).get("high")),
        "avwap_dev_pct": round(avwap_dev, 1) if avwap_dev is not None else None,
        "avwap_break": avwap_break,
        "volume_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "chasing": chasing,
        "degraded": any(g["verdict"] == "degraded" for g in gates),
    }


def evaluate_all(indicators_map: dict[str, Optional[dict]]) -> list[dict]:
    """批量判定，返回通过/未通过全量结果（保序）。"""
    return [evaluate(s, indicators_map.get(s)) for s in indicators_map]


def passing_symbols(results: list[dict]) -> list[dict]:
    """只返回通过的标的（用于日层通知 / 周层喂给 LLM 写文字）。"""
    return [r for r in results if r.get("passed")]


# ── 周度指标（周报第①段：一周完整数据，纯 Python 零 LLM）────────────────
_STOP_FALLBACK = 0.92  # 初始止损 -8%（§15.3 铁律，无支撑位时的兜底）


def _weekly_stop_price(ind: dict) -> Optional[float]:
    """由支撑位算止损价：取 close 下方最近的支撑位；无则用 -8% 兜底。"""
    close = _num(ind.get("close"))
    if close is None:
        return None
    supports = ind.get("supports") or []
    below = sorted([_num(s) for s in supports if _num(s) is not None and _num(s) < close])
    if below:
        return round(below[-1], 2)  # 距 close 最近的支撑
    return round(close * _STOP_FALLBACK, 2)


def weekly_metrics(symbol: str, history: list[tuple[str, dict]]) -> Optional[dict]:
    """把一周指标序列聚合为周报①段的结构化事实（全部数字由本函数算）。

    ``history``：``[(date_str, indicators_dict), ...]`` 升序（来自 market_data_reader.fetch_indicators_history）。
    空序列返回 None（调用方跳过）。
    返回字段：week_change_pct / week_high / week_low / vol_ratio_latest / vol_ratio_avg /
    passed_days / n_bars / dates / latest（最新一日的 evaluate 全量结果）+ stop_price。
    """
    if not history:
        return None

    closes = [c for c in (_num(d.get("close")) for _, d in history) if c is not None]
    if not closes:
        return None

    first_close, last_close = closes[0], closes[-1]
    week_change_pct = round((last_close / first_close - 1.0) * 100.0, 2) if first_close else None

    # 周量比：最新一日的 volume.ratio + 窗口内平均（趋势参照）
    vol_ratios = [v for v in (_num((d.get("volume") or {}).get("ratio")) for _, d in history) if v is not None]
    vol_latest = vol_ratios[-1] if vol_ratios else None
    vol_avg = round(sum(vol_ratios) / len(vol_ratios), 2) if vol_ratios else None

    # 本周通过闸门的天数（逐日跑 evaluate，纯 Python）
    passed_days = sum(1 for _, d in history if evaluate(symbol, d).get("passed"))

    latest = evaluate(symbol, history[-1][1])

    return {
        "symbol": symbol,
        "n_bars": len(history),
        "dates": [d for d, _ in history],
        "week_change_pct": week_change_pct,
        "week_high": round(max(closes), 2),
        "week_low": round(min(closes), 2),
        "vol_ratio_latest": vol_latest,
        "vol_ratio_avg": vol_avg,
        "passed_days": passed_days,
        "stop_price": _weekly_stop_price(history[-1][1]),
        "latest": latest,
    }
