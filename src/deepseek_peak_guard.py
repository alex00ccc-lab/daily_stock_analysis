"""DeepSeek 峰谷定价守护 — 高峰时段跳过 LLM 调用（除非 force）。

DeepSeek 自 2026-08-17 起按北京时间分时定价：
  - 高峰: 09:00–12:00 与 14:00–18:00（价格 = 空闲的 2 倍）
  - 空闲: 其余时段（17h/天）

本模块只做纯时钟判断，与交易日历无关；周末在高峰时段同样视为高峰
（保守处理，符合 DeepSeek 按钟计费，而非按交易日）。

用法（各 LLM 调用点开头）::

    from src.deepseek_peak_guard import should_skip_llm
    if should_skip_llm(force=force):
        logger.info("LLM call deferred: Beijing peak hours, use --force to override")
        return None

DSA 为 PUBLIC 仓库（Actions 免费分钟），主防线是闲时调度；本 guard 只做兜底，
同样跳过不睡。
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional

logger = logging.getLogger("deepseek_peak_guard")

TZ_BEIJING = timezone(timedelta(hours=8))  # 北京无夏令时，固定 +8

# 北京时间高峰窗口 (HH, MM)
_PEAK_WINDOWS = ((time(9, 0), time(12, 0)), (time(14, 0), time(18, 0)))


def _now_beijing(now: Optional[datetime] = None) -> datetime:
    """返回北京时间；`now` 若传入，按「已是北京时间」解释（供测试注入）。"""
    if now is None:
        return datetime.now(TZ_BEIJING)
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ_BEIJING)
    return now.astimezone(TZ_BEIJING)


def in_peak(now: Optional[datetime] = None) -> bool:
    """当前是否处于北京高峰时段。"""
    t = _now_beijing(now).timetz().replace(tzinfo=None)
    return any(start <= t < end for start, end in _PEAK_WINDOWS)


def should_skip_llm(force: bool = False, now: Optional[datetime] = None) -> bool:
    """True = 调用方不得调用 DeepSeek（当前为高峰且未 force）。"""
    if force:
        return False
    return in_peak(now)


class PeakHourError(RuntimeError):
    """高峰期强制阻塞异常（严格调用方使用 ensure_off_peak）。"""


def ensure_off_peak(force: bool = False, now: Optional[datetime] = None) -> None:
    """高峰且未 force 时抛 PeakHourError；否则静默通过。"""
    if should_skip_llm(force=force, now=now):
        t = _now_beijing(now)
        raise PeakHourError(
            f"Beijing peak hours {t.strftime('%H:%M')} — LLM call blocked (use force to override)"
        )
