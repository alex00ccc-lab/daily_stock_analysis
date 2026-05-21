# -*- coding: utf-8 -*-
"""
===================================
美国宏观经济与美联储数据模块
===================================

职责：
1. 获取美国主要股指实时数据
2. 获取国债收益率曲线
3. 推断美联储政策立场
4. 提供宏观环境摘要供 AI 分析使用

数据来源：
- yfinance: 指数行情、国债收益率
- 网络搜索: 美联储政策声明、关键经济数据发布
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


# ============================================================
# Data Classes
# ============================================================

@dataclass
class USMarketIndex:
    """美国主要股指"""
    ticker: str
    name: str
    current: float = 0.0
    change_pct: float = 0.0
    change_amount: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ticker': self.ticker,
            'name': self.name,
            'current': self.current,
            'change_pct': self.change_pct,
            'change_amount': self.change_amount,
            'high': self.high,
            'low': self.low,
            'volume': self.volume,
        }


@dataclass
class TreasuryData:
    """国债收益率数据"""
    rate_3m: Optional[float] = None     # 3个月
    rate_2y: Optional[float] = None     # 2年期
    rate_5y: Optional[float] = None     # 5年期
    rate_10y: Optional[float] = None    # 10年期
    rate_30y: Optional[float] = None    # 30年期

    # 关键利差
    spread_2_10: Optional[float] = None  # 2Y-10Y 利差（最重要）
    spread_3m_10: Optional[float] = None # 3M-10Y 利差

    @property
    def yield_curve_status(self) -> str:
        """判断收益率曲线状态"""
        if self.spread_2_10 is None:
            return "未知"
        if self.spread_2_10 > 0.5:
            return "正常陡峭"
        elif self.spread_2_10 > 0:
            return "平坦化"
        elif self.spread_2_10 > -0.5:
            return "轻微倒挂"
        else:
            return "深度倒挂（衰退预警）"

    def to_dict(self) -> Dict[str, Any]:
        return {
            'rate_3m': self.rate_3m,
            'rate_2y': self.rate_2y,
            'rate_5y': self.rate_5y,
            'rate_10y': self.rate_10y,
            'rate_30y': self.rate_30y,
            'spread_2_10': self.spread_2_10,
            'spread_3m_10': self.spread_3m_10,
            'yield_curve_status': self.yield_curve_status,
        }


@dataclass
class FedData:
    """美联储政策数据"""
    fed_funds_rate: Optional[float] = None      # 当前联邦基金利率
    next_meeting_date: str = ""                  # 下次会议日期
    policy_stance: str = "未知"                  # 鹰派/中性/鸽派
    balance_sheet_trend: str = "未知"            # 缩表/维稳/扩表
    rate_outlook: str = ""                       # 利率展望摘要

    def to_dict(self) -> Dict[str, Any]:
        return {
            'fed_funds_rate': self.fed_funds_rate,
            'next_meeting_date': self.next_meeting_date,
            'policy_stance': self.policy_stance,
            'balance_sheet_trend': self.balance_sheet_trend,
            'rate_outlook': self.rate_outlook,
        }


@dataclass
class USMacroData:
    """美国宏观经济关键指标"""
    cpi_yoy: Optional[float] = None          # CPI 同比
    core_cpi_yoy: Optional[float] = None     # 核心 CPI 同比
    unemployment_rate: Optional[float] = None # 失业率
    gdp_growth: Optional[float] = None       # GDP 增长率
    pmi_manufacturing: Optional[float] = None # ISM 制造业 PMI
    pmi_services: Optional[float] = None     # ISM 服务业 PMI
    consumer_sentiment: Optional[float] = None # 消费者信心

    data_source: str = ""                    # 数据来源说明

    def to_dict(self) -> Dict[str, Any]:
        return {
            'cpi_yoy': self.cpi_yoy,
            'core_cpi_yoy': self.core_cpi_yoy,
            'unemployment_rate': self.unemployment_rate,
            'gdp_growth': self.gdp_growth,
            'pmi_manufacturing': self.pmi_manufacturing,
            'pmi_services': self.pmi_services,
            'consumer_sentiment': self.consumer_sentiment,
        }


# ============================================================
# US Macro Fetcher
# ============================================================

class USMacroFetcher:
    """美国宏观经济数据获取器"""

    # 主要美股指数
    US_INDICES = {
        '^GSPC': 'S&P 500',
        '^IXIC': 'NASDAQ Composite',
        '^DJI': 'Dow Jones',
        '^VIX': 'VIX 波动率',
        '^RUT': 'Russell 2000',
    }

    # 国债收益率代码
    TREASURY_TICKERS = {
        '^IRX': '3M',     # 13-week T-bill
        '^FVX': '5Y',     # 5-year
        '^TNX': '10Y',    # 10-year
        '^TYX': '30Y',    # 30-year
    }

    def __init__(self, search_service=None):
        self._search_service = search_service

    def fetch_indices(self) -> List[USMarketIndex]:
        """获取美国主要股指实时行情"""
        indices = []
        try:
            import yfinance as yf

            for ticker, name in self.US_INDICES.items():
                try:
                    stock = yf.Ticker(ticker)
                    info = stock.info or {}
                    fast = stock.fast_info if hasattr(stock, 'fast_info') else None

                    idx = USMarketIndex(
                        ticker=ticker,
                        name=name,
                        current=_safe_float(info.get('regularMarketPrice') or info.get('previousClose', 0)),
                        change_pct=_safe_float(info.get('regularMarketChangePercent', 0)) or 0,
                        prev_close=_safe_float(info.get('previousClose', 0)) or 0,
                        open=_safe_float(info.get('regularMarketOpen', 0)) or 0,
                        high=_safe_float(info.get('regularMarketDayHigh', 0)) or 0,
                        low=_safe_float(info.get('regularMarketDayLow', 0)) or 0,
                        volume=_safe_float(info.get('regularMarketVolume', 0)) or 0,
                    )
                    # 用 fast_info 补充
                    if fast and idx.current == 0:
                        idx.current = _safe_float(getattr(fast, 'last_price', 0)) or 0

                    if idx.current > 0:
                        indices.append(idx)
                        logger.debug(f"[US宏观] {name}: {idx.current:.2f} ({idx.change_pct:+.2f}%)")
                except Exception as e:
                    logger.warning(f"[US宏观] 获取 {name} ({ticker}) 失败: {e}")

            logger.info(f"[US宏观] 获取到 {len(indices)} 个指数行情")
        except Exception as e:
            logger.error(f"[US宏观] 获取指数失败: {e}")

        return indices

    def fetch_treasury_rates(self) -> TreasuryData:
        """获取国债收益率"""
        data = TreasuryData()
        try:
            import yfinance as yf

            for ticker, label in self.TREASURY_TICKERS.items():
                try:
                    stock = yf.Ticker(ticker)
                    info = stock.info or {}
                    rate = _safe_float(info.get('regularMarketPrice') or info.get('previousClose'))

                    if rate is not None:
                        if label == '3M':
                            data.rate_3m = rate
                        elif label == '5Y':
                            data.rate_5y = rate
                        elif label == '10Y':
                            data.rate_10y = rate
                        elif label == '30Y':
                            data.rate_30y = rate
                except Exception as e:
                    logger.warning(f"[US宏观] 获取 {label} 国债收益率失败: {e}")

            # 用 3M 作为联邦基金利率的代理
            if data.rate_3m:
                pass  # kept as rate_3m

            # 2Y: use ^FVX or derive from 10Y spread
            # For 2Y we use a separate ticker if available
            try:
                two_yr = yf.Ticker('^UST2Y')  # Some platforms have this
                info_2y = two_yr.info or {}
                rate_2y = _safe_float(info_2y.get('regularMarketPrice') or info_2y.get('previousClose'))
                if rate_2y:
                    data.rate_2y = rate_2y
            except Exception:
                pass

            # 计算关键利差
            if data.rate_2y is not None and data.rate_10y is not None:
                data.spread_2_10 = round(data.rate_10y - data.rate_2y, 2)
            if data.rate_3m is not None and data.rate_10y is not None:
                data.spread_3m_10 = round(data.rate_10y - data.rate_3m, 2)

            logger.info(
                f"[US宏观] 国债: 10Y={data.rate_10y}%, "
                f"2Y-10Y利差={data.spread_2_10}, "
                f"曲线={data.yield_curve_status}"
            )
        except Exception as e:
            logger.error(f"[US宏观] 获取国债收益率失败: {e}")

        return data

    def fetch_fed_data(self) -> FedData:
        """推断美联储政策数据（基于市场数据）"""
        fed = FedData()

        try:
            import yfinance as yf

            # 用 3M T-bill 作为联邦基金利率代理
            try:
                tbill = yf.Ticker('^IRX')
                info = tbill.info or {}
                rate = _safe_float(info.get('regularMarketPrice') or info.get('previousClose'))
                if rate:
                    fed.fed_funds_rate = rate
            except Exception:
                pass

            # 从利率水平推断政策立场
            if fed.fed_funds_rate:
                if fed.fed_funds_rate > 5:
                    fed.policy_stance = "紧缩高位"
                elif fed.fed_funds_rate > 3:
                    fed.policy_stance = "中性偏紧"
                elif fed.fed_funds_rate > 1:
                    fed.policy_stance = "宽松"
                else:
                    fed.policy_stance = "极度宽松"

            # 估算下次会议（FOMC 约每6周一次）
            fed.next_meeting_date = "待确认（FOMC约每6周一次）"

        except Exception as e:
            logger.warning(f"[US宏观] 获取美联储数据失败: {e}")

        return fed

    def fetch_comprehensive(
        self
    ) -> Dict[str, Any]:
        """
        获取综合宏观数据

        Returns:
            {
                'indices': List[USMarketIndex],
                'treasury': TreasuryData,
                'fed': FedData,
                'macro': USMacroData,
                'sentiment': str (市场情绪摘要),
            }
        """
        logger.info("[US宏观] 开始获取综合宏观数据...")

        indices = self.fetch_indices()
        treasury = self.fetch_treasury_rates()
        fed = self.fetch_fed_data()
        macro = USMacroData(data_source="需要 FRED API Key 获取精确宏观经济数据")

        # 市场情绪判断
        sentiment = self._assess_market_sentiment(indices, treasury)

        result = {
            'indices': indices,
            'treasury': treasury,
            'fed': fed,
            'macro': macro,
            'sentiment': sentiment,
        }

        logger.info(f"[US宏观] 综合宏观数据获取完成，情绪: {sentiment}")
        return result

    def _assess_market_sentiment(
        self,
        indices: List[USMarketIndex],
        treasury: TreasuryData
    ) -> str:
        """评估市场整体情绪"""
        vix_value = None
        sp500_change = 0

        for idx in indices:
            if idx.ticker == '^VIX':
                vix_value = idx.current
            if idx.ticker == '^GSPC':
                sp500_change = idx.change_pct

        sentiment_parts = []

        # VIX 判断
        if vix_value is not None:
            if vix_value < 15:
                sentiment_parts.append("低波动(极度乐观)")
            elif vix_value < 20:
                sentiment_parts.append("正常波动")
            elif vix_value < 30:
                sentiment_parts.append("恐慌上升")
            else:
                sentiment_parts.append("高恐慌")

        # S&P 500 方向
        if sp500_change > 1:
            sentiment_parts.append("强势上涨")
        elif sp500_change > 0:
            sentiment_parts.append("小幅上涨")
        elif sp500_change > -1:
            sentiment_parts.append("小幅下跌")
        else:
            sentiment_parts.append("明显下跌")

        # 收益率曲线
        curve = treasury.yield_curve_status
        if "倒挂" in curve:
            sentiment_parts.append(curve)

        return " | ".join(sentiment_parts)

    def get_macro_summary(self) -> str:
        """生成宏观数据摘要（供 AI 分析使用）"""
        data = self.fetch_comprehensive()

        lines = ["【美国宏观经济数据】"]
        lines.append(f"数据时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

        # 指数
        lines.append("\n## 主要股指")
        for idx in data['indices']:
            direction = "↑" if idx.change_pct > 0 else "↓" if idx.change_pct < 0 else "-"
            lines.append(
                f"  {idx.name} ({idx.ticker}): {idx.current:.2f} "
                f"({direction}{abs(idx.change_pct):.2f}%)"
            )

        # 国债
        t = data['treasury']
        lines.append("\n## 国债收益率")
        for label, rate in [
            ('3M', t.rate_3m), ('2Y', t.rate_2y),
            ('5Y', t.rate_5y), ('10Y', t.rate_10y), ('30Y', t.rate_30y)
        ]:
            if rate is not None:
                lines.append(f"  {label}: {rate:.2f}%")
        lines.append(f"  2Y-10Y利差: {t.spread_2_10}%")
        lines.append(f"  收益率曲线: {t.yield_curve_status}")

        # 美联储
        f = data['fed']
        lines.append("\n## 美联储政策")
        lines.append(f"  联邦基金利率(代理): {f.fed_funds_rate}%")
        lines.append(f"  政策立场: {f.policy_stance}")
        lines.append(f"  下次会议: {f.next_meeting_date}")

        # 市场情绪
        lines.append(f"\n## 市场情绪: {data['sentiment']}")

        # 宏观数据
        m = data['macro']
        lines.append(f"\n## 关键经济指标（来源: {m.data_source}）")
        for label, val in [
            ('CPI 同比', m.cpi_yoy),
            ('核心 CPI 同比', m.core_cpi_yoy),
            ('失业率', m.unemployment_rate),
            ('GDP 增长率', m.gdp_growth),
            ('制造业 PMI', m.pmi_manufacturing),
            ('服务业 PMI', m.pmi_services),
        ]:
            if val is not None:
                lines.append(f"  {label}: {val}%")

        return "\n".join(lines)


def _safe_float(val) -> Optional[float]:
    """安全转换为 float"""
    if val is None:
        return None
    try:
        return round(float(val), 2)
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-8s | %(message)s')

    fetcher = USMacroFetcher()
    summary = fetcher.get_macro_summary()
    print(summary)
