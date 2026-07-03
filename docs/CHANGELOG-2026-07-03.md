# 🔧 迭代修复日志 — 2026-07-03

## 更新摘要

本次迭代修复了 **3 组问题**，核心改动：**交易日判断假日误判修复 + 休市通知 + 美股自选股列表 Variable/Secret 优先级冲突修复**。

---

## 一、问题诊断

### 1. 美股假日凌晨被错误跳过 🔴

**现象**：7月3日（周五，美国独立日假期）未收到美股分析报告，但前一天 7月2日是正常交易日，应有数据可分析。

**根因**：`src/core/trading_calendar.py` 中 `get_open_markets_today()` 使用 `datetime.now(tz).date()` 直接判断「今天是不是交易日」，未考虑「当前是否有已完成交易数据可分析」。7月3日美东凌晨 00:39 时，该函数检查 7月3日（假日）→ 返回 False → 跳过分析。而已有的 `get_effective_trading_date()` 函数正确实现了假日回溯 + 收盘前后判断。

**影响范围**：3 个市场（A 股 cn / 港股 hk / 美股 us）× 4 处调用点（`main.py` × 2、`bot/commands/market.py`、`api/v1/endpoints/analysis.py`）。A 股 Workflow（`daily_analysis.yml`）无 Bash 层交易日检查，完全暴露在 Bug 之下；美股 Workflow 有 Bash 层兜底但被 Python 层拦住。

**日志证据**（Run #28638814217）：
```
Bash 层: 美股交易日，将执行分析（check_date=7月2日）✅
Python 层: 今日所有相关市场均为非交易日，跳过执行 ❌
```

### 2. 休市时无任何通知 🔵

`main.py` 两处 `should_skip` / `effective_region == ''` 直接 `return`，用户完全不知道发生了什么。`bot/commands/market.py:124-133` 已有休市通知标准模式，但 `main.py` 未复用。

### 3. 美股自选股列表未更新 🔴

**现象**：用户在 `secrets.US_STOCK_LIST` 中添加了新自选美股，但报告仍显示旧列表（10 只固定个股）。

**根因**：仓库中同时存在 **两份** `US_STOCK_LIST`：

| 来源 | 值 | 最后更新 |
|------|-----|----------|
| **Variable** `US_STOCK_LIST` | 旧列表（10 只） | 6月10日 |
| **Secret** `US_STOCK_LIST` | 新列表（用户更新） | 7月2日 |

Workflow 取值链为 `vars.us_stock_list || secrets.us_stock_list || vars.US_STOCK_LIST || secrets.US_STOCK_LIST`。GitHub 自动将变量名转大写，`vars.us_stock_list` 匹配到 `US_STOCK_LIST` Variable → 永远命中旧列表，Secret 中的新列表永远读不到。

---

## 二、代码修改清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/core/trading_calendar.py` | **修复** | `get_open_markets_today()` 改用 `get_effective_trading_date(mkt)` 替代 `datetime.now(tz).date()` |
| `main.py` | **新增** | `_send_market_closed_notification()` 辅助函数 + 2 处调用 |
| `.github/workflows/daily_analysis_us.yml` | **修复** | `US_STOCK_LIST` 取值链改为 `secrets.US_STOCK_LIST \|\| vars.US_STOCK_LIST`（Secret 优先生效） |
| `.github/workflows/daily_analysis.yml` | **修复** | 同上 |

Git commits: `fd74088` → `7975583` → `9e73254`

---

## 三、`get_open_markets_today()` 修复详情

### 修改前（幼稚判断）

```python
tz = ZoneInfo(tz_name)
today = datetime.now(tz).date()       # 只看"今天"
if is_market_open(mkt, today):        # 假日 → False
```

### 修改后（时间感知判断）

```python
effective_date = get_effective_trading_date(mkt)  # 复用已有智能逻辑
if is_market_open(mkt, effective_date):           # 自动回溯到最近交易日
```

`get_effective_trading_date()` 逻辑（已存在于 `trading_calendar.py:119-164`）：
- 非交易日 → 自动回溯到最近交易日
- 交易日已收盘 → 返回今天
- 交易日未收盘 → 返回前一个交易日

### 跨市场验证矩阵

| 场景 | 市场时区 | 修复前 Today | 修复后 Effective Date | 结果 |
|------|---------|-------------|----------------------|------|
| 美股独立日前夜 | ET 7/3 00:39 | 7/3(假日)❌ | 7/2(交易日) | ✅ |
| A 股春节期间 | CN 假日 18:00 | 假日❌ | 前一个交易日 | ✅ |
| 港股假期 | HK 假日 18:00 | 假日❌ | 前一个交易日 | ✅ |
| 周末 | 周六/周日 | 周末❌ | 周五 | ✅ |
| 交易日收盘后 | 各市场 | 正常✅ | 正常 | ✅ |

---

## 四、US_STOCK_LIST 优先级修复详情

### 修改前

```
vars.us_stock_list → secrets.us_stock_list → vars.US_STOCK_LIST → secrets.US_STOCK_LIST
      ↓                       ↓                       ↓                    ↓
 匹配旧Variable(大写转换)      空                   旧列表(命中!)          永远不读取
```

### 修改后

```
secrets.US_STOCK_LIST → vars.US_STOCK_LIST → secrets.us_stock_list → vars.us_stock_list
       ↓                      ↓
 新列表(命中!) ✅          旧列表(兜底)
```

---

## 五、手动验证结果

### 美股工作流（Run #28643791653，force_run=true）

| 指标 | 结果 |
|------|------|
| 成功/失败 | **10/10** ✅ |
| 耗时 | 5 分 30 秒 |
| LLM 模型 | `deepseek/deepseek-chat` |
| 生成报告 | `report_20260703.md`（27KB）+ `market_review_20260703.md`（8KB） |
| 钉钉推送 | **2/2 成功** ✅ |

```
已配置 1 个通知渠道：自定义Webhook
自定义 Webhook 1（钉钉）推送成功
通知发送完成：成功 1 个，失败 0 个
大盘复盘推送成功
自定义 Webhook 1（钉钉）推送成功
通知发送完成：成功 1 个，失败 0 个
已推送美股决策仪表盘 (10只)
```

---

## 六、影响范围总结

| 市场 | Workflow | 修复前 | 修复后 |
|------|---------|--------|--------|
| 美股 | `daily_analysis_us.yml` | Bash 放行 + Python 拦住 → 假日丢失报告 | ✅ 正常 |
| A 股 | `daily_analysis.yml` | 无 Bash 兜底 → 春节/国庆静默跳过 | ✅ 正常 |
| 港股 | 同上 | 港股假日静默跳过 | ✅ 正常 |
| 全部 | — | 休市无通知 | ✅ 推送休市通知 |

---

## 七、回滚方案

如需回退 `get_open_markets_today()` 修改：

```bash
git revert fd74088
```

如需回退 Workflow 优先级修改：

```bash
git revert 9e73254
```
