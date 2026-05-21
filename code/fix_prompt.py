#!/usr/bin/env python
"""Compress the output format section in analyzer.py."""
from pathlib import Path

p = Path("src/analyzer.py")
text = p.read_text("utf-8")

# Find the block to replace
start_marker = "重点关注（必须明确回答）："
end_marker = '请输出完整的 JSON 格式决策仪表盘。"""'

idx_start = text.find(start_marker)
idx_end = text.find(end_marker)

if idx_start == -1:
    print("ERROR: start marker not found")
    exit(1)
if idx_end == -1:
    print("ERROR: end marker not found")
    exit(1)

idx_end += len(end_marker)

# Go back to find the if statement
block_start = text.rfind("if use_legacy_default_prompt:", 0, idx_start)
if block_start == -1:
    print("ERROR: block_start not found")
    exit(1)

old_block = text[block_start:idx_end]
print(f"Found block: {len(old_block)} chars")

# Build replacement - preserve the f-string with {news_window_days}
replacement = '''prompt += f"""

### 输出要求
- 输出完整 JSON 决策仪表盘；股票名用中文全称；核心结论一句话说清该买/该卖/该等
- 分空仓/持仓给出不同建议；买入价、止损价、目标价精确到分
- 检查清单每项用 ✅/⚠️/❌ 标记
- 消息面信息不得超出近{news_window_days}日或时间未知；严禁互斥结论同时作为有效依据"""'''

text = text[:block_start] + replacement + text[idx_end:]

p.write_text(text, "utf-8")
print("Done!")
