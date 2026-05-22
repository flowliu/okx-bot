# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""网格策略参数

调参就改这里。这些参数会影响整个网格的形态、风险、收益频率。
"""

# ===== 交易标的 =====
INST_ID = "ETH-USDT-SWAP"

# ===== 资金与杠杆 =====
TOTAL_USDT = 2000          # 总投入资金(USDT)
LEVERAGE = 2               # 杠杆倍数

# ===== 网格形状 =====
GRID_COUNT = 20            # 总格子数(中心价上下各一半)
RANGE_PCT = 0.20           # 区间宽度: 中心价 ±20% → [center*0.8, center*1.2]
GRID_TYPE = "geometric"    # geometric=等比, arithmetic=等差。加密推荐 geometric

# ===== 风控 =====
STOP_LOSS_PCT = 0.10       # 跳出区间外 10% 触发市价清仓 + 暂停
MIN_MARGIN_RATIO = 0.30    # 保证金率低于 30% 时停止新挂单并告警(MVP 暂未实现)

# ===== 运行参数 =====
TD_MODE = "cross"          # cross=全仓, isolated=逐仓。网格用 cross 抗穿仓
POLL_INTERVAL = 3          # REST 轮询订单状态的间隔(秒)
DB_PATH = "grid.db"        # SQLite 持久化文件

# OKX REST 域名。注册地区不同必须使用对应域名,否则鉴权失败:
#   https://www.okx.com    全球(默认,中国大陆需走代理)
#   https://aws.okx.com    AWS 节点,延迟更稳
#   https://us.okx.com     美国 / 澳大利亚(app.okx.com 注册)
#   https://eea.okx.com    欧盟(my.okx.com 注册)
OKX_DOMAIN = "https://www.okx.com"

# ===== AI 顾问 (DeepSeek) =====
# 未配置 DEEPSEEK_API_KEY 时整个 AI 模块退化为「全部放行」,不影响主流程
AI_PROVIDER = "deepseek"   # 模型厂商: deepseek/openai/anthropic/gemini/qwen/moonshot/glm
AI_ENABLED = True          # 总开关
AI_INTERVAL_SEC = 60       # 决策刷新间隔(秒),默认 1 分钟
AI_KLINE_BAR = "1m"        # K 线周期 1m/5m/15m/1H/4H/1D
AI_KLINE_LIMIT = 60        # 取最近 N 根
AI_MODEL = "deepseek-chat" # deepseek-chat 性价比高;深度推理用 deepseek-reasoner
AI_REQUEST_TIMEOUT = 30    # DeepSeek 请求超时(秒)
# 走 MITM 代理(Clash/Surge 默认)时把 TLS 校验直接关掉,免去每次启动 fallback 的 warning;
# 若代理已加 api.deepseek.com 白名单或没用代理,保持 True 走严格校验。
AI_VERIFY_SSL = False

# AI 自动重居中: 开启后,当 AI 给出 recenter_to 价位时自动撤所有开仓挂单并按新中心重发
# 关闭则只在日志里建议(更安全,默认关)
AI_AUTO_RECENTER = False
# 即便 AI 给了建议,价格离当前中心 < N% 时也不重居中(避免噪声)
AI_RECENTER_MIN_DRIFT_PCT = 0.05

# ===== AI 完全驱动模式(实验) =====
# True  → 抛弃机械网格,完全由 AI 每次决定挂哪些 open/close 单
# False → 走 mechanical grid + AI 趋势过滤(默认/稳妥)
# 切换前请先用 reset.py 清理旧数据
AI_DRIVEN_MODE = True
# 单次 AI 调用最多接受多少对(long+short 合计上限),防止 AI 输出过多撑爆资金
AI_MAX_ORDERS_PER_CALL = 20

# AI 自动撤单（轮询期间执行，不等 60s 下一轮 AI 决策）：
#   - 漂移撤：未成交 open 单距当前价超过 AI_AUTO_CANCEL_DRIFT_PCT → 立即撤
#   - 陈旧撤：未成交 open 单挂着超过 AI_AUTO_CANCEL_STALE_SEC → 立即撤
# 被撤的 slot 不影响已成交持仓的 close 单。
AI_AUTO_CANCEL = True
AI_AUTO_CANCEL_DRIFT_PCT = 0.012   # 1.2%
AI_AUTO_CANCEL_STALE_SEC = 300     # 5 min
