# -*- coding: utf-8 -*-
"""watchlist_pipeline — 自选股周更管线编排（绕开 src/analyzer.py，自包含）。

两个 tier：

  --tier daily  （工作日，零 token）
      读公开管道最新指标 → 跑 watchlist_gates 闸门 → 推送通知，
      列出「具备买入条件」的标的 + Python 算出的数字。**不调用任何 LLM。**

  --tier weekly （周末，闲时，唯一 LLM 调用）
      同上判定 → 对通过标的做一次批量 DeepSeek 文字复核（watchlist_llm，
      只出定性文字、零数字）→ 推送「数字摘要 + 文字复核」合并通知。

通知：企业微信 Webhook（``WECHAT_WEBHOOK_URL`` 环境变量），纯 text/markdown 直连，
不引入 DSA 那套多通道 notification_routing（保持零 token 泄漏面、零重依赖）。

Usage:
  python -m src.watchlist_pipeline --tier daily --notify
  python -m src.watchlist_pipeline --tier weekly --notify --force
  python -m src.watchlist_pipeline --tier daily --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.market_data_reader import (
    fetch_watchlist_symbols,
    fetch_all_indicators,
    fetch_indicators_history,
    fetch_macro_history,
    fetch_all_fundamentals,
    fetch_all_options,
)
from src.watchlist_gates import evaluate_all, passing_symbols, weekly_metrics
from src.watchlist_weekly import assemble_macro, fetch_news
from src.watchlist_llm import generate_prose, generate_weekly_prose

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("watchlist_pipeline")

_HINT_ZH = {
    "consider_entry": "🟢 可考虑分批建仓",
    "watch": "👀 继续观察等更佳点位",
    "wait": "⏸ 暂缓，等待确认",
    "avoid": "🚫 回避",
}

# 闸门判定 → 图标（对齐 watchlist_gates._ICON）
_ICON_ZH = {
    "green": "🟢",
    "yellow": "🟡",
    "red": "🔴",
    "degraded": "⚪",
}

# 企业微信 markdown 单条上限（字节），留余量分批
_WECOM_MARKDOWN_MAX_BYTES = 3800


# ── 通知 ────────────────────────────────────────────────────────────────────
def _bjt_now_str() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def _chunk_markdown(text: str, max_bytes: int = _WECOM_MARKDOWN_MAX_BYTES) -> list[str]:
    """按字节上限对 markdown 分批（简单按行切，不拆断单行，避免超限被微信拒收）。"""
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len((cur + line).encode("utf-8")) > max_bytes and cur:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    return chunks or [text]


def send_wecom_markdown(content: str, dry_run: bool = False) -> bool:
    """发送企业微信 markdown；缺 webhook 或 dry-run 时打印到 stdout。"""
    webhook = os.getenv("WECHAT_WEBHOOK_URL", "")
    if dry_run or not webhook:
        print(content)
        if dry_run:
            print("\n[dry-run] 未发送（--dry-run）")
        elif not webhook:
            print("\n[WECHAT_WEBHOOK_URL 未配置] 未发送")
        return False
    import requests
    ok = True
    for chunk in _chunk_markdown(content):
        try:
            r = requests.post(webhook, json={"msgtype": "markdown", "markdown": {"content": chunk}}, timeout=20)
            if r.status_code != 200 or r.json().get("errcode", 0) != 0:
                logger.warning("wecom send failed: %s %s", r.status_code, r.text[:200])
                ok = False
        except Exception as e:  # noqa: BLE001
            logger.warning("wecom send exception: %s", e)
            ok = False
    return ok


def _symbol_line(r: dict) -> str:
    close = r.get("close")
    ma20 = r.get("ma20")
    avwap = r.get("avwap_dev_pct")
    vol = r.get("volume_ratio")
    rsi = r.get("rsi")
    parts = []
    if close is not None:
        parts.append(f"close {close:.2f}")
    if ma20 is not None:
        parts.append(f"ma20 {ma20:.2f}")
    if avwap is not None:
        parts.append(f"AVWAP偏离 {avwap:+.1f}%")
    if vol is not None:
        parts.append(f"量比 {vol:.2f}")
    if rsi is not None:
        parts.append(f"RSI {rsi:.1f}")
    return " | ".join(parts)


# ── tier 实现 ──────────────────────────────────────────────────────────────
def _build_daily_md(passing: list[dict], all_results: list[dict]) -> str:
    lines = [f"## 📊 自选股买入条件筛查", f"**{_bjt_now_str()} BJT** · 数据源: market-data-collector（公开指标）", ""]
    if passing:
        lines.append(f"✅ **具备买入条件 {len(passing)} 只**：")
        for r in passing:
            lines.append(f"- **{r['symbol']}** — {_symbol_line(r)}")
    else:
        lines.append("⚠️ 今日无标的通过买入条件闸门。")
    # 附通过与否的简表（含未通过的，方便一眼看全貌）
    if all_results:
        lines += ["", "**全量判定**："]
        for r in all_results:
            mark = "✅" if r["passed"] else "❌"
            lines.append(f"- {mark} {r['symbol']}: {r['summary']}")
    return "\n".join(lines)


def run_daily(args) -> int:
    symbols = fetch_watchlist_symbols()
    if not symbols:
        logger.warning("watchlist 为空，退出")
        return 0
    logger.info("watchlist %d symbols: %s", len(symbols), symbols)
    ind_map = fetch_all_indicators(symbols)
    results = evaluate_all(ind_map)
    passing = passing_symbols(results)
    logger.info("passed %d/%d", len(passing), len(results))
    md = _build_daily_md(passing, results)
    send_wecom_markdown(md, dry_run=args.dry_run)
    return 0


def _discipline_block() -> list[str]:
    """买后纪律参数（§15.3/15.4 铁律，写死进周报第④段，作为「系统分析→建仓」最后一道门）。"""
    return [
        "- 仓位：首仓 ≤ 计划 40–50%；单票 ≤15% / 单市场 ≤40% / 现金 ≥10%",
        "- 加仓：金字塔 50/30/20，量必递减，禁倒金字塔",
        "- 止盈：+15% 卖 1/3，+30% 卖到半仓，+50% 留 20–30% 底仓",
        "- 买入前过一遍 §15.6 检查清单（thesis 写下来 / 情绪冷静 / 非开盘 30 分钟内）",
    ]


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v:+.2f}%" if v is not None else "无数据"


def _build_weekly_md(weekly: list[dict], macro_items: list[dict], news_items: list[dict], prose: Optional[dict], fundamentals: Optional[dict] = None, options: Optional[dict] = None) -> str:
    lines = [f"## 📈 自选股周报（决策文件）", f"**{_bjt_now_str()} BJT** · 数据源: market-data-collector（公开指标）", ""]

    # ── ① 一周盘面回顾（Python 算）──
    lines.append("### ① 一周盘面回顾")
    lines.append(f"覆盖 {len(weekly)} 只（有数据者）· 通过闸门天数 / 周涨跌 / 周高低 / 量比")
    for w in weekly:
        latest = w["latest"]
        mark = "✅" if latest.get("passed") else "❌"
        lines.append(
            f"- {mark} **{w['symbol']}**：周涨跌 {_fmt_pct(w.get('week_change_pct'))} · "
            f"周高 {w.get('week_high')} / 周低 {w.get('week_low')} · "
            f"通过 {w.get('passed_days')}/{w.get('n_bars')} 天 · 量比 {w.get('vol_ratio_latest')}"
        )
    lines.append("")

    # ── ② 公司基本面研究 ──
    lines.append("### ② 公司基本面研究")
    if fundamentals:
        lines.append("估值灯（PE/PB/股息/市值，finnhub weekly 快照）：")
        for w in weekly:
            sym = w["symbol"]
            f = fundamentals.get(sym)
            if not f:
                lines.append(f"- **{sym}**：基本面待回填（新加入自选股，下一轮 weekly 抓取后覆盖）")
                continue
            parts = []
            pe = f.get("pe_ratio")
            pb = f.get("pb_ratio")
            dy = f.get("dividend_yield")
            cap = f.get("market_cap")
            ind = f.get("industry") or ""
            if isinstance(pe, (int, float)):
                parts.append(f"PE {pe:.1f}")
            if isinstance(pb, (int, float)):
                parts.append(f"PB {pb:.1f}")
            if isinstance(dy, (int, float)):
                parts.append(f"股息 {dy * 100:.2f}%")
            if cap:
                parts.append(f"市值 {cap}")
            if ind:
                parts.append(f"行业 {ind}")
            lines.append(f"- **{sym}**：" + " · ".join(parts) if parts else f"- **{sym}**：无估值字段")
    else:
        lines.append("⚠️ 本周基本面数据缺失（fetch-weekly 尚未产出或全部抓取失败）。")
    lines.append("")

    # ── ②b 期权墙（max pain / call wall / put wall / ATM IV / IV-HV）──
    lines.append("### ②b 期权墙（机构持仓磁吸 + 隐含波动率）")
    if options:
        for w in weekly:
            sym = w["symbol"]
            o = options.get(sym)
            if not o:
                lines.append(f"- **{sym}**：期权墙待回填（新加入自选股，下一轮 weekly 抓取后覆盖）")
                continue
            parts = []
            mp = o.get("max_pain")
            cw = o.get("call_wall")
            pw = o.get("put_wall")
            iv = o.get("atm_iv")
            gap = o.get("iv_hv_gap")
            if mp is not None:
                parts.append(f"max pain {mp}")
            if cw is not None:
                parts.append(f"call wall {cw}")
            if pw is not None:
                parts.append(f"put wall {pw}")
            if isinstance(iv, (int, float)):
                parts.append(f"ATM IV {iv * 100:.1f}%")
            if isinstance(gap, (int, float)):
                parts.append(f"IV-HV {gap * 100:+.1f}%")
            lines.append(f"- **{sym}**：" + " · ".join(parts) if parts else f"- **{sym}**：无期权墙字段")
    else:
        lines.append("⚠️ 本周期权墙数据缺失（fetch-weekly 尚未产出或全部抓取失败）。")
    lines.append("")

    # ── ③ 宏观指标及影响 ──
    lines.append("### ③ 宏观指标及影响")
    if macro_items:
        lines.append("**本周重大指标周环比**（Python 算）：")
        for m in macro_items:
            chg = _fmt_pct(m.get("week_change_pct"))
            price = m.get("price")
            if price is not None:
                lines.append(f"- {m.get('name')}（{m.get('symbol')}）：{price}，周环比 {chg}")
            else:
                lines.append(f"- {m.get('name')}（{m.get('symbol')}）：周环比 {chg}")
    else:
        lines.append("⚠️ 本周无宏观数据。")
    if news_items:
        lines.append("")
        lines.append("**本周新闻头条**（Python 抓取）：")
        for n in news_items[:6]:
            src = f"（{n.get('source')}）" if n.get("source") else ""
            lines.append(f"- {n.get('title')}{src}")
    else:
        lines.append("（无新闻 key 或抓取失败，新闻段缺省）")
    if prose and prose.get("macro_policy"):
        lines.append("")
        lines.append("**宏观政策面解读**（LLM 文字）：")
        lines.append(prose["macro_policy"])
    if prose and prose.get("news_brief"):
        lines.append("")
        lines.append("**新闻要点归纳**（LLM 文字）：")
        lines.append(prose["news_brief"])
    lines.append("")

    # ── ④ 建仓决策文件 ──
    lines.append("### ④ 建仓决策文件")
    prose_syms = {p["symbol"]: p for p in (prose.get("symbols", []) if prose else [])}
    passing = [w for w in weekly if w["latest"].get("passed")]
    if not passing:
        lines.append("⚠️ 本周无标的通过买入条件闸门——不建仓，继续观察（这正是「慢下来」要的效果）。")
    for w in passing:
        latest = w["latest"]
        lines.append(f"#### {w['symbol']}（通过闸门）")
        # 买点触发（Python 算）
        gate_lines = []
        for g in latest.get("gates", []):
            gate_lines.append(f"{_ICON_ZH.get(g['verdict'], g['verdict'])}{g['name']}")
        lines.append(f"- 买点触发：{' / '.join(gate_lines)}")
        lines.append(f"- 数字（Python 算）: {_symbol_line(latest)}")
        if w.get("stop_price") is not None:
            lines.append(f"- 止损位：{w.get('stop_price')}（由支撑位/ATR 算，或 -8% 兜底）")
        for dline in _discipline_block():
            lines.append(dline)
        p = prose_syms.get(w["symbol"])
        if p:
            hint = _HINT_ZH.get(p["action_hint"], p["action_hint"])
            lines.append(f"- 结论（LLM 文字）: {hint}")
            if p.get("thesis_comment"):
                lines.append(f"  - 逻辑: {p['thesis_comment']}")
            if p.get("risk_note"):
                lines.append(f"  - 风险: {p['risk_note']}")
        else:
            lines.append("- 结论（LLM 文字）: （闲时未跑或未命中，仅数字）")
        lines.append("")
    return "\n".join(lines)


def _write_obsidian(md: str, obsidian_dir: str) -> Optional[str]:
    """把完整四段周报写进 Obsidian 报告目录，返回写盘路径。

    路径：``<obsidian_dir>/自选股/{iso_year}-W{iso_week:02d}-自选股周报.md``（ISO 周，
    对齐 holdings ``周报/2026-W34-复盘周报.md`` 命名）。带 frontmatter（tags/type/date）。
    写盘失败只告警不阻断（微信短摘要照发）。
    """
    try:
        now = datetime.now(timezone(timedelta(hours=8)))
        iso_year, iso_week, _ = now.date().isocalendar()
        rel_dir = os.path.join(obsidian_dir, "自选股")
        os.makedirs(rel_dir, exist_ok=True)
        path = os.path.join(rel_dir, f"{iso_year}-W{iso_week:02d}-自选股周报.md")
        frontmatter = (
            "---\n"
            "tags: [自选股]\n"
            "type: weekly\n"
            f"date: {now.strftime('%Y-%m-%d')}\n"
            "---\n\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(frontmatter + md)
        return path
    except Exception as e:  # noqa: BLE001 — 写盘失败降级，不阻断通知
        logger.warning("Obsidian 写盘失败: %s", e)
        return None


def run_weekly(args) -> int:
    symbols = fetch_watchlist_symbols()
    if not symbols:
        logger.warning("watchlist 为空，退出")
        return 0
    logger.info("weekly watchlist %d symbols: %s", len(symbols), symbols)

    # ① 一周完整数据（历史序列 → 周度指标，纯 Python）
    weekly = []
    for s in symbols:
        hist = fetch_indicators_history(s, days=8)
        wm = weekly_metrics(s, hist)
        if wm:
            weekly.append(wm)
    logger.info("weekly: %d/%d symbols have weekly data", len(weekly), len(symbols))

    # ② 宏观指标周环比（纯 Python）
    macro_items = assemble_macro(fetch_macro_history(days=8))
    logger.info("weekly: %d macro items", len(macro_items))

    # ②b 基本面（估值灯：PE/PB/股息/市值，weekly 快照，纯 Python）
    fundamentals = fetch_all_fundamentals(symbols)
    logger.info("weekly: %d/%d symbols have fundamentals",
                sum(1 for v in fundamentals.values() if v), len(symbols))

    # ②b 期权墙（max pain / call wall / put wall / ATM IV / IV-HV，weekly 快照，纯 Python）
    options = fetch_all_options(symbols)
    logger.info("weekly: %d/%d symbols have options",
                sum(1 for v in options.values() if v), len(symbols))

    # ③ 新闻头条（Python 抓，无 key 降级）
    news_items = fetch_news(days=7)
    logger.info("weekly: %d news headlines", len(news_items))

    # ④ LLM 单次归纳（宏观政策 + 新闻要点 + 个股复核，受 peak guard 约束）
    passing = [w["latest"] for w in weekly if w["latest"].get("passed")]
    prose = generate_weekly_prose(passing, macro_items=macro_items, news_items=news_items, force=args.force)

    md = _build_weekly_md(weekly, macro_items, news_items, prose, fundamentals=fundamentals, options=options)
    send_wecom_markdown(md, dry_run=args.dry_run)
    if getattr(args, "obsidian_dir", None):
        path = _write_obsidian(md, args.obsidian_dir)
        if path:
            logger.info("Obsidian weekly written: %s", path)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="自选股周更管线（daily 零 token / weekly LLM 文字）")
    parser.add_argument("--tier", choices=["daily", "weekly"], required=True)
    parser.add_argument("--notify", action="store_true", help="已默认开启，此参数保留兼容")
    parser.add_argument("--dry-run", action="store_true", help="只打印不发送")
    parser.add_argument("--force", action="store_true", help="忽略北京高峰时段 guard（仅 weekly 的 LLM 调用）")
    parser.add_argument("--obsidian-dir", default=None,
                        help="若传入，把完整周报写盘到 <dir>/自选股/（Obsidian 报告仓库 checkout 路径）")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    if args.tier == "daily":
        return run_daily(args)
    return run_weekly(args)


if __name__ == "__main__":
    raise SystemExit(main())
