# -*- coding: utf-8 -*-
"""
===================================
美股基本面数据获取模块
===================================

职责：
1. 获取美股 PE、EPS、PS、PB、FCF 等核心估值指标
2. 计算自由现金流收益率、营收/盈利增长率
3. 提供估值的文字总结

数据来源：Yahoo Finance (yfinance)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class USFundamentalData:
    """美股基本面数据"""

    ticker: str
    name: str = ""

    # ========== 估值指标 ==========
    pe_trailing: Optional[float] = None       # 市盈率 TTM
    pe_forward: Optional[float] = None        # 前瞻市盈率
    ps_ratio: Optional[float] = None          # 市销率
    pb_ratio: Optional[float] = None          # 市净率
    peg_ratio: Optional[float] = None         # PEG 比率

    # ========== 每股指标 ==========
    eps_trailing: Optional[float] = None      # 每股收益 TTM
    eps_forward: Optional[float] = None       # 前瞻每股收益
    revenue_per_share: Optional[float] = None # 每股营收

    # ========== 现金流 ==========
    free_cashflow: Optional[float] = None     # 自由现金流
    fcf_yield: Optional[float] = None         # 自由现金流收益率
    operating_cashflow: Optional[float] = None

    # ========== 市值与资本 ==========
    market_cap: Optional[float] = None        # 市值
    enterprise_value: Optional[float] = None  # 企业价值
    debt_to_equity: Optional[float] = None    # 负债/权益

    # ========== 成长性 ==========
    revenue_growth: Optional[float] = None    # 营收同比增长率
    earnings_growth: Optional[float] = None   # 盈利同比增长率
    earnings_quarterly_growth: Optional[float] = None

    # ========== 回报 ==========
    dividend_yield: Optional[float] = None    # 股息率
    return_on_equity: Optional[float] = None  # ROE
    profit_margin: Optional[float] = None     # 净利润率

    # ========== 其他 ==========
    beta: Optional[float] = None              # Beta 系数
    short_ratio: Optional[float] = None       # 做空比例
    target_mean_price: Optional[float] = None
    recommendation: str = ""

    fetch_success: bool = True
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ticker': self.ticker,
            'name': self.name,
            'pe_trailing': self.pe_trailing,
            'pe_forward': self.pe_forward,
            'ps_ratio': self.ps_ratio,
            'pb_ratio': self.pb_ratio,
            'peg_ratio': self.peg_ratio,
            'eps_trailing': self.eps_trailing,
            'eps_forward': self.eps_forward,
            'free_cashflow': self.free_cashflow,
            'fcf_yield': self.fcf_yield,
            'market_cap': self.market_cap,
            'debt_to_equity': self.debt_to_equity,
            'revenue_growth': self.revenue_growth,
            'earnings_growth': self.earnings_growth,
            'dividend_yield': self.dividend_yield,
            'return_on_equity': self.return_on_equity,
            'beta': self.beta,
            'short_ratio': self.short_ratio,
        }

    def get_valuation_level(self) -> str:
        """评估估值水平"""
        if self.pe_trailing is None:
            return "未知"
        pe = self.pe_trailing
        if pe <= 0:
            return "亏损"
        elif pe < 15:
            return "低估"
        elif pe < 20:
            return "合理偏低"
        elif pe < 30:
            return "合理偏高"
        else:
            return "高估"

    def get_fcf_assessment(self) -> str:
        """评估自由现金流健康度"""
        if self.fcf_yield is None:
            return "未知"
        if self.fcf_yield >= 5:
            return "充裕"
        elif self.fcf_yield >= 2:
            return "健康"
        elif self.fcf_yield >= 0:
            return "一般"
        else:
            return "紧张"


class USFundamentalFetcher:
    """美股基本面数据获取器"""

    def __init__(self):
        pass

    def fetch(self, ticker: str) -> USFundamentalData:
        """
        获取美股基本面数据

        Args:
            ticker: 美股代码，如 AAPL, GOOGL

        Returns:
            USFundamentalData
        """
        try:
            import yfinance as yf

            stock = yf.Ticker(ticker)
            info = stock.info or {}

            if not info:
                return USFundamentalData(
                    ticker=ticker,
                    fetch_success=False,
                    error_message="无法获取股票信息"
                )

            # 计算自由现金流收益率
            fcf = info.get('freeCashflow')
            mkt_cap = info.get('marketCap')
            fcf_yield = None
            if fcf and mkt_cap and mkt_cap > 0:
                fcf_yield = (fcf / mkt_cap) * 100

            data = USFundamentalData(
                ticker=ticker,
                name=info.get('longName') or info.get('shortName', ''),

                pe_trailing=_safe_float(info.get('trailingPE')),
                pe_forward=_safe_float(info.get('forwardPE')),
                ps_ratio=_safe_float(info.get('priceToSalesTrailing12Months')),
                pb_ratio=_safe_float(info.get('priceToBook')),
                peg_ratio=_safe_float(info.get('pegRatio')),

                eps_trailing=_safe_float(info.get('trailingEps')),
                eps_forward=_safe_float(info.get('forwardEps')),
                revenue_per_share=_safe_float(info.get('revenuePerShare')),

                free_cashflow=fcf,
                fcf_yield=round(fcf_yield, 2) if fcf_yield else None,
                operating_cashflow=_safe_float(info.get('operatingCashflow')),

                market_cap=mkt_cap,
                enterprise_value=_safe_float(info.get('enterpriseValue')),
                debt_to_equity=_safe_float(info.get('debtToEquity')),

                revenue_growth=_pct(info.get('revenueGrowth')),
                earnings_growth=_pct(info.get('earningsGrowth')),
                earnings_quarterly_growth=_pct(info.get('earningsQuarterlyGrowth')),

                dividend_yield=_pct(info.get('dividendYield')),
                return_on_equity=_pct(info.get('returnOnEquity')),
                profit_margin=_pct(info.get('profitMargins')),

                beta=_safe_float(info.get('beta')),
                short_ratio=_safe_float(info.get('shortRatio')),
                target_mean_price=_safe_float(info.get('targetMeanPrice')),
                recommendation=info.get('recommendationKey', ''),

                fetch_success=True,
            )

            logger.info(
                f"[US基本面] {ticker} {data.name}: "
                f"PE={data.pe_trailing}, FCF_Yield={data.fcf_yield}%, "
                f"RevGrowth={data.revenue_growth}%"
            )
            return data

        except Exception as e:
            logger.error(f"[US基本面] {ticker} 获取失败: {e}")
            return USFundamentalData(
                ticker=ticker,
                fetch_success=False,
                error_message=str(e)
            )

    def get_valuation_summary(self, ticker: str) -> str:
        """生成估值摘要文本（供 AI 分析使用）"""
        d = self.fetch(ticker)
        if not d.fetch_success:
            return f"{ticker}: 基本面数据获取失败 ({d.error_message})"

        lines = [f"【{d.name} ({d.ticker}) 基本面数据】"]
        lines.append(f"市值: {_fmt_billion(d.market_cap)}")
        lines.append(f"市盈率(TTM): {d.pe_trailing} | 前瞻PE: {d.pe_forward} | PEG: {d.peg_ratio}")
        lines.append(f"市销率(PS): {d.ps_ratio} | 市净率(PB): {d.pb_ratio}")
        lines.append(f"EPS(TTM): {d.eps_trailing} | 前瞻EPS: {d.eps_forward}")
        lines.append(f"自由现金流收益率: {d.fcf_yield}% | 负债权益比: {d.debt_to_equity}")
        lines.append(f"营收增长率: {d.revenue_growth}% | 盈利增长率: {d.earnings_growth}%")
        lines.append(f"ROE: {d.return_on_equity}% | 净利润率: {d.profit_margin}%")
        lines.append(f"股息率: {d.dividend_yield}% | Beta: {d.beta}")
        lines.append(f"做空比例: {d.short_ratio} | 分析师评级: {d.recommendation}")
        lines.append(f"估值水平: {d.get_valuation_level()} | 现金流: {d.get_fcf_assessment()}")

        return "\n".join(lines)


def _safe_float(val) -> Optional[float]:
    """安全转换为 float，保留2位小数"""
    if val is None:
        return None
    try:
        return round(float(val), 2)
    except (ValueError, TypeError):
        return None


def _pct(val) -> Optional[float]:
    """百分比值转换（yfinance 中增长率已是百分比形式如 0.15 = 15%）"""
    v = _safe_float(val)
    if v is not None:
        return round(v * 100, 2)
    return None


def _fmt_billion(val) -> str:
    """格式化大数值为 B/M"""
    if val is None:
        return "N/A"
    if abs(val) >= 1e12:
        return f"{val/1e12:.2f}T"
    if abs(val) >= 1e9:
        return f"{val/1e9:.2f}B"
    if abs(val) >= 1e6:
        return f"{val/1e6:.2f}M"
    return f"{val:.2f}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-8s | %(message)s')

    fetcher = USFundamentalFetcher()
    for ticker in ['AAPL', 'MSFT', 'NVDA']:
        summary = fetcher.get_valuation_summary(ticker)
        print(summary)
        print()
