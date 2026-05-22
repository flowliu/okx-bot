<div align="center">

# OrbitAI

**AI 驱动的永续合约智能网格交易机器人 — OKX**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![OKX SDK](https://img.shields.io/badge/python--okx-0.4.1-green.svg)](https://pypi.org/project/python-okx/)

[简体中文](README.md) · [English](README.en.md)

</div>

---

> ## ⚠️ 重要声明 / Disclaimer
>
> **本程序仅供技术学习、研究与交流，不构成任何投资建议。**
> 加密货币衍生品交易具有极高风险，可能导致全部本金损失。请在充分理解策略原理与代码逻辑后，**先用 OKX 模拟盘充分测试**；投入真实资金即意味着您接受全部风险，由此产生的任何盈亏均由您本人承担。
>
> **作者与贡献者不对任何直接或间接的金融损失负责。** 使用本软件即视为同意上述条款。

---

## ✨ 特性

- 🤖 **多模型 AI 决策**：支持 DeepSeek / OpenAI / Anthropic / Gemini / Qwen / Moonshot / GLM 七大厂商，运行时一键切换
- 📊 **智能网格策略**：AI 实时分析 K 线 + RSI/EMA/ATR/布林带/量比，动态生成订单梯度（趋势/震荡/反转自适应）
- 🎛 **可视化 Web 控制台**：浏览器登录管理 — 启停 Bot、改参数、改 AI Prompt、看实时日志、查每日收益、一键重置（撤单 + 平仓 + 清 DB）
- 📈 **本地化收益统计**：后台每 10 分钟同步 OKX 账单到本地 SQLite，突破 OKX 7 天历史限制，折线图 + 明细表
- 🛡 **健康自检 + 一键初始化**：配置文件损坏自动检测并提示修复（损坏文件自动备份）
- 🌐 **多语言界面**：内置中文 / English 切换，登录页右上角下拉，localStorage 记忆
- 🔄 **OKX 临时错误自动重试**：50013 / TLS 超时等服务端繁忙错误指数退避重试，启动期幂等设置失败不阻塞主循环
- 🔐 **品牌完整性校验**：HMAC-SHA256 防篡改的版本号 / 版权信息

> ⚠️ **风险提示**：加密货币合约交易具有重大风险。本项目仅供学习研究，使用者自负盈亏。**强烈建议先用 OKX 模拟盘充分测试**。

---

## 🚀 快速开始

### 1. 环境要求
- Python 3.10+
- macOS / Linux / WSL
- OKX 账户 + API Key（首次强烈建议用 [模拟盘](https://www.okx.com/trade-demo/)）
- 至少一个 LLM API Key（推荐 DeepSeek，性价比最高）

### 2. 安装

```bash
git clone https://github.com/<your-org>/okx-bot.git
cd okx-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置

```bash
cp .env.example .env
# 编辑 .env，填入 OKX 凭证 + LLM API Key + Web 登录密码
```

生成 WEBUI_SECRET：
```bash
python -c "import secrets;print(secrets.token_hex(32))"
```

### 4. 启动 Web 控制台

```bash
set -a; source .env; set +a
python -m uvicorn webui:app --host 0.0.0.0 --port 8765
```

浏览器打开 <http://localhost:8765>，用 `.env` 里的 `WEBUI_USER/WEBUI_PASS` 登录后，可以：
- 顶部「启动 / 停止 / 重置」按钮管理 Bot 生命周期
- 「日志」tab 看实时 SSE 推送日志，支持切换历史日期 + 分块向上加载
- 「统计」tab 看每日盈亏折线图 + 明细表
- 「参数」tab 配置交易标的、杠杆、AI 模型；「大模型密钥」面板按当前 provider 单独输入
- 「Prompt」tab 调整 AI 提示词（保存即热生效，无需重启）
- 登录页右上角可切换 **中文 / English**

### 5. Docker 部署（推荐生产用）

```bash
# 准备 .env（如还没准备）
cp .env.example .env
vim .env   # 填入凭证

# 一键启动
docker compose up -d

# 查看日志
docker compose logs -f

# 停止
docker compose down
```

镜像：`orbitai:1.0.0`，默认绑 `127.0.0.1:8765`（仅本机访问，安全）。
持久化数据全在 `./data/` 目录，删容器不丢数据。

公网访问请套 Nginx/Caddy 反代 + HTTPS，并把 ports 改成 `"8765:8765"`。

时区默认 `Asia/Shanghai`，按部署地区在 `docker-compose.yml` 修改 `TZ` 环境变量（影响每日收益聚合）。

### 6. 命令行使用（不用 Web UI 也行）

```bash
# 直接跑 Bot
python main.py

# 查看每日收益
python stats.py today
python stats.py week
python stats.py 2026-05-20

# 一键重置（撤单 + 平仓 + 清 DB）
python reset.py

# 测试 OKX 连通性
python check_conn.py
```

---

## ⚙️ 配置详解

所有可调参数都在 Web UI「参数」tab 编辑，保存写入 `runtime_config.json`，**重启 Bot 生效**。

### 基础配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `OKX_DOMAIN` | `https://www.okx.com` | 区域 API 域名（全球 / AWS / 美澳 / 欧盟）|
| `INST_ID` | `ETH-USDT-SWAP` | 交易标的，下拉 16 主流币 |
| `TOTAL_USDT` | `2000` | 总投入资金 |
| `LEVERAGE` | `2` | 杠杆倍数 |
| `GRID_COUNT` | `20` | 机械网格格数 |
| `RANGE_PCT` | `0.20` | 网格区间 ±20% |
| `STOP_LOSS_PCT` | `0.10` | 止损百分比 |
| `POLL_INTERVAL` | `3` | 订单状态轮询间隔（秒）|
| `AI_KLINE_BAR` | `1m` | K 线周期 |
| `AI_KLINE_LIMIT` | `60` | 拉取 K 线根数 |
| `AI_MAX_ORDERS_PER_CALL` | `20` | 每轮最大订单对数 |

### 大模型配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `AI_PROVIDER` | `deepseek` | 厂商：deepseek / openai / anthropic / gemini / qwen / moonshot / glm |
| `AI_MODEL` | `deepseek-chat` | 模型名（按厂商默认或自定义）|
| `AI_DRIVEN_MODE` | `true` | 是否启用 AI 驱动模式 |
| `AI_ENABLED` | `true` | AI 顾问总开关 |
| `AI_INTERVAL_SEC` | `60` | AI 决策刷新间隔（秒）|
| `AI_REQUEST_TIMEOUT` | `30` | AI 请求超时 |
| `AI_VERIFY_SSL` | `false` | SSL 校验（MITM 代理设 false）|

### 大模型密钥
切换 `AI_PROVIDER` 后，密钥面板自动展示对应厂商的输入框。保存在 `llm_keys.json`（权限 600）。

---

## 📐 架构

```
┌─────────────┐      ┌──────────────┐      ┌──────────────┐
│  Web UI     │◄────►│  webui.py    │◄────►│  main.py     │
│ (HTML/JS)   │      │  FastAPI     │      │  Bot 主进程  │
└─────────────┘      └──────┬───────┘      └──────┬───────┘
                            │                      │
                ┌───────────┴────────┐    ┌────────┴────────┐
                ▼                    ▼    ▼                 ▼
        ┌──────────────┐  ┌────────────────┐  ┌──────────────┐
        │ stats_db.py  │  │ ai_advisor.py  │  │   grid.py    │
        │ 收益缓存     │  │ 多 LLM 路由    │  │ 网格 + AI    │
        └──────────────┘  └────────┬───────┘  │  调度核心    │
                                   │           └──────┬───────┘
                                   ▼                  ▼
                          ┌────────────────┐  ┌──────────────┐
                          │ llm_keys.py    │  │  client.py   │
                          │ 7 厂商适配     │  │  OKX SDK     │
                          └────────────────┘  └──────────────┘
```

| 模块 | 职责 |
|---|---|
| `main.py` | Bot 启动入口，写 PID、初始化、进入主循环 |
| `grid.py` | 订单生命周期、对账、AI 调度、重试退避 |
| `ai_advisor.py` | K 线/指标采集、Prompt 构造、多 LLM 调用 |
| `llm_keys.py` | 7 厂商 API 元数据 + 密钥管理 |
| `config_loader.py` | `config.py` 默认值 + `runtime_config.json` 覆盖 |
| `db.py` / `stats_db.py` | SQLite 持久化（订单状态 / 每日收益）|
| `webui.py` | FastAPI：登录、启停、配置、日志 SSE、健康检查 |
| `client.py` | OKX SDK 客户端工厂（支持多区域域名）|
| `stats.py` | 命令行版每日收益统计 |
| `reset.py` | 撤所有单 + 市价平仓 + 清 DB |

---

## 🔌 多 LLM 厂商

| 厂商 | 默认模型 | 注册入口 |
|---|---|---|
| DeepSeek | `deepseek-chat` | <https://platform.deepseek.com> |
| OpenAI | `gpt-4o-mini` | <https://platform.openai.com> |
| Anthropic | `claude-haiku-4-5` | <https://console.anthropic.com> |
| Google Gemini | `gemini-2.0-flash` | <https://aistudio.google.com/apikey> |
| 通义千问 Qwen | `qwen-turbo` | <https://dashscope.console.aliyun.com> |
| Moonshot | `moonshot-v1-8k` | <https://platform.moonshot.cn> |
| 智谱 GLM | `glm-4-flash` | <https://open.bigmodel.cn> |

切换在 Web UI「参数 → 大模型配置 → AI_PROVIDER」下拉，密钥在下方「大模型密钥」面板配置。

---

## 🌐 OKX 区域域名

不同注册地区必须用对应域名，否则鉴权失败：

| 域名 | 适用 |
|---|---|
| `https://www.okx.com` | 全球（默认，中国大陆需代理）|
| `https://aws.okx.com` | AWS 节点，延迟更稳 |
| `https://us.okx.com` | 美国 / 澳大利亚（app.okx.com 注册）|
| `https://eea.okx.com` | 欧盟（my.okx.com 注册）|

在 Web UI「参数 → OKX_DOMAIN」下拉切换。

---

## 🔒 部署安全

- 🌍 **建议使用海外服务器部署**：OKX API 在中国大陆访问受网络/延迟影响，本机或大陆机房经常触发 TLS 握手超时；推荐 AWS Tokyo / Singapore / 香港等节点
- **不要**直接把 8765 端口暴露在公网。建议套 Caddy / Nginx 加 HTTPS + 再加一层 Basic Auth / IP 白名单
- 默认密码 `please-change-me` 必须改，`WEBUI_SECRET` 必须用 `python -c "import secrets;print(secrets.token_hex(32))"` 生成
- `.env`、`runtime_config.json`、`llm_keys.json`、`bot.pid`、`grid.db`、`stats.db`、`logs/` 均已加入 `.gitignore`
- `llm_keys.json` 文件权限 0600，仅文件 owner 可读

---

## 🛟 故障排查

| 现象 | 处理 |
|---|---|
| `50013 Systems are busy` | OKX 服务端繁忙，代码已自动重试（指数退避 5–8 次）|
| `TLS handshake timeout` | 网络抖动，启动期已重试；运行期 AI 调用失败会跳过本轮 |
| 全部挂单失败 `placed=0` | OKX 拒绝（拥堵或参数错），日志会标 ERROR `⚠ 全部挂单失败` |
| `runtime_config.json 解析失败` | Web UI 顶部横幅会提示，一键「初始化」即可（损坏文件备份为 `.corrupt.<ts>`）|
| `prompts/scalp.txt 缺少占位符` | 同上，一键从 `prompts/scalp.default.txt` 还原（旧版备份为 `.bak.<ts>`）|

---

## 🧪 开发

```bash
# 启动 webui dev 模式（修改自动重载）
python -m uvicorn webui:app --reload --port 8765

# 单步测 AI 决策
python -c "import ai_advisor; print(ai_advisor._refresh_once())"

# 看实时日志
tail -f logs/grid_$(date +%Y-%m-%d).log
```

---

## 📂 关键文件

```
okx-bot/
├── main.py                 # Bot 启动入口
├── webui.py                # Web 控制台后端
├── grid.py                 # 网格策略核心
├── ai_advisor.py           # AI 决策模块
├── llm_keys.py             # 多厂商密钥管理
├── config.py               # 默认配置（不要改，改 runtime_config.json）
├── config_loader.py        # 配置加载（含 overlay）
├── client.py               # OKX SDK 客户端工厂
├── db.py                   # 交易状态 SQLite
├── stats_db.py             # 每日收益缓存 SQLite
├── stats.py                # CLI 统计工具
├── notify.py               # 日志告警
├── reset.py                # 重置脚本
├── branding.py             # 版本/版权（HMAC 签名）
├── prompts/
│   ├── scalp.txt           # AI 提示词（可热编辑）
│   └── scalp.default.txt   # 默认模板（用于初始化）
├── static/
│   └── index.html          # Web 控制台前端
├── .env.example            # 环境变量模板
├── requirements.txt
├── LICENSE
└── README.md / README.en.md
```

---

## 📜 License

[MIT](LICENSE) © 2026 [AIPrompt](https://www.aiprompt.vip/)

---

<div align="center">
让 AI 应用更有价值 · Powered by <a href="https://www.aiprompt.vip/">AIPrompt</a>
</div>
