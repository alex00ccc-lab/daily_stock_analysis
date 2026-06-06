# 🔧 系统优化更新 — 2026-06-06

## 更新摘要

本次更新修复了 4 组问题，核心改动：**LLM 回退策略重构（DeepSeek 主选 + Gemini 备用）+ 美股板块数据修复 + 复权一致性修复**。

---

## 一、问题诊断

### 1. A 股个股分析大量失败 🔴

**现象**：每日 A 股（含港股）工作流 `daily_analysis.yml` 生成的分析报告中缺少个股决策仪表盘，仅有大盘复盘内容。

**根因**：
- Gemini 免费配额在早间美股分析（UTC 01:00）中大量消耗，到下午 A 股分析时段（UTC 10:00）遭遇全球高峰拥堵 + 配额枯竭
- 回退到 OpenAI 时协议不匹配（`OPENAI_API_KEY` 实际存的是 DeepSeek key），返回 401
- `DEEPSEEK_API_KEY` 未单独配置，deepseek 回退链路无法生效

**日志证据**（6月1日-5日）：
```
Gemini: ServiceUnavailableError - UNAVAILABLE
↓ fallback
OpenAI: 401 - Incorrect API key provided
↓ all models exhausted
成功: 1, 失败: 7
```

### 2. 美股/港股大盘报告无板块数据 🟡

**根因**：
- `US_PROFILE.has_sector_rankings = False` — 硬编码跳过
- `HK_PROFILE.has_sector_rankings = False` — 同
- 现有 `get_sector_rankings()` 仅支持 Akshare/Efinance/Tushare 中国数据源
- yfinance/finnhub/alphavantage 均未实现板块接口

### 3. 港股未加入自选股 🟡

GitHub Actions Variables 中 `STOCK_LIST` 仅有 6 位 A 股代码，无 5 位港股代码。

### 4. 复权价格数据混合不一致 🟡

Issue #234 将实时行情价与 DB 缓存的历史前复权价混合计算涨跌幅。当 DB 数据拉取后发生除权事件时，缓存的 `yesterday.close` 使用旧调整基准，导致自算涨跌幅出现偏差。

---

## 二、代码修改清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `.github/workflows/daily_analysis.yml` | 修改 | `LITELLM_MODEL=deepseek/deepseek-chat` + `MARKET_REVIEW_MODEL=gemini/gemini-2.5-flash` |
| `.github/workflows/daily_analysis_us.yml` | 修改 | 同上 |
| `src/config.py` | 修改 | 新增 `market_review_model` + `force_kline_refresh_days` 配置字段 |
| `src/analyzer.py` | 修改 | `_call_litellm()`/`generate_text()` 支持 `override_model`；Prompt 增加复权/PE/PB 标注 |
| `src/market_analyzer.py` | 修改 | 大盘复盘使用 `MARKET_REVIEW_MODEL` 专用模型 |
| `src/core/market_profile.py` | 修改 | 美股/港股 `has_sector_rankings` → `True` |
| `data_provider/yfinance_fetcher.py` | 新增 | `get_sector_rankings()` 通过 11 只 SPDR 板块 ETF 获取美股板块排名 |
| `src/core/pipeline.py` | 修改 | 涨跌幅优先实时源 `change_pct`；K 线定期强制刷新 |
| `src/storage.py` | 修改 | 新增 `get_latest_data_date()` 方法 |

Git commits: `8e6853c` → `96ec774` → `75409a4` → `4fa4b3c`

---

## 三、模型调用流程（改动后）

```
个股分析 (A股/美股)
  ┌─ DeepSeek (primary)    → 主力模型，稳定可靠
  └─ Gemini (fallback)     → DeepSeek 不可用时自动切换

大盘复盘 (A股/美股/港股)
  ┌─ Gemini (primary)      → 专用额度审核大盘，保证准确性
  └─ DeepSeek (fallback)   → Gemini 不可用时自动回退
```

### 板块数据获取

```
A 股 → Akshare(东方财富) → Efinance → Tushare → 模板降级
美股 → Yfinance(SPDR ETF) → 模板降级
港股 → 尝试 Akshare → 模板降级
```

---

## 四、验证结果

### A 股工作流（Run #27060675313）

| 指标 | 修改前 | 修改后 |
|------|--------|--------|
| 成功/失败 | 1/7 | **8/8** ✅ |
| 主模型 | gemini | **deepseek/deepseek-chat** |
| 耗时 | ~7 min | ~5 min |

```
LITELLM_MODEL: deepseek/deepseek-chat ✅
MARKET_REVIEW_MODEL: gemini/gemini-2.5-flash ✅
[LLM解析] 湖南裕能 分析完成: 看空, 评分 25
[LLM解析] 大族激光 分析完成: 看空, 评分 25
[LLM解析] 安孚科技 分析完成: 震荡, 评分 45
... (8/8 全部成功)
```

### 美股工作流（Run #27061075402）

| 指标 | 结果 |
|------|------|
| 成功/失败 | **2/2** ✅ |
| 主模型 | **deepseek/deepseek-chat** |
| 耗时 | ~52 sec |

```
LITELLM_MODEL: deepseek/deepseek-chat ✅
MARKET_REVIEW_MODEL: gemini/gemini-2.5-flash ✅
[LLM解析] NOK 分析完成: 卖出, 评分 25
[LLM解析] PL 分析完成: 卖出, 评分 10
成功: 2, 失败: 0
```

---

## ⚠️ 待用户手动操作

以下配置项需要在 GitHub 仓库设置页面中手动完成：

### 1. 确认 Secrets 配置

| Secret | 状态 | 说明 |
|--------|------|------|
| `DEEPSEEK_API_KEY` | ✅ 已配置 | 本次验证通过，无需修改 |
| `GEMINI_API_KEY` | ✅ 已配置 | 用作大盘复盘和大盘回退 |
| `OPENAI_API_KEY` | 可选 | 当前为空，无需操作 |

> 💡 如果你的 DeepSeek key 之前存在 `OPENAI_API_KEY` 中且现在仍保留，建议删除以避免回退链误用。

### 2. 添加港股到自选股（可选）

编辑 GitHub Actions **Variables** → `STOCK_LIST`，添加港股 5 位代码（逗号分隔），例如：

```
600519,000001,300750,00700,09988,01810
```

### 3. 自定义大盘复盘模型（可选）

默认使用 `gemini/gemini-2.5-flash`。如需修改，在 Variables 中添加：

| Variable | 值 |
|----------|-----|
| `MARKET_REVIEW_MODEL` | `gemini/gemini-2.5-flash`（或自定义） |

### 4. 调整 K 线刷新频率（可选）

默认每 5 个交易日强制刷新。在 Variables 中添加：

| Variable | 值 |
|----------|-----|
| `FORCE_KLINE_REFRESH_DAYS` | `5`（默认） |

---

## 五、回滚方案

如需回退到修改前的状态（Gemini 主选），在 GitHub Actions Variables 中设置：

| Variable | 值 |
|----------|-----|
| `LITELLM_MODEL` | `gemini/gemini-2.5-flash` |

或直接删除 `LITELLM_MODEL` Variable，系统将自动从可用 API Key 推断主模型。
