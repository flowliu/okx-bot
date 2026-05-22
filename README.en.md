<div align="center">

# OrbitAI

**An AI-driven grid trading bot for OKX perpetual swaps**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![OKX SDK](https://img.shields.io/badge/python--okx-0.4.1-green.svg)](https://pypi.org/project/python-okx/)

[简体中文](README.md) · [English](README.en.md)

</div>

---

> ## ⚠️ Disclaimer
>
> **This software is provided strictly for technical study, research and discussion. It is NOT financial advice.**
> Crypto derivatives trading carries extreme risk and may lead to total loss of capital. Make sure you fully understand the strategy logic and source code, then **test thoroughly on the OKX demo account first**. Deploying with real funds means you accept all risks and are solely responsible for any gains or losses.
>
> **The authors and contributors are not liable for any direct or indirect financial loss.** Using this software implies you agree to these terms.

---

## ✨ Features

- 🤖 **Multi-LLM decision engine** — DeepSeek / OpenAI / Anthropic / Gemini / Qwen / Moonshot / GLM, switch at runtime
- 📊 **Smart adaptive grid** — AI ingests klines + RSI/EMA/ATR/Bollinger Bands/volume ratio and generates order ladders that adapt to trending vs. ranging markets
- 🎛 **Web console** — Browser-based control: start / stop / **reset** (cancel orders + market-close + wipe DB), edit params, edit AI prompt, live log stream, daily P&L
- 📈 **Local P&L cache** — Background job syncs OKX bills to SQLite every 10 minutes, bypassing the 7-day history limit; line chart + detail table
- 🛡 **Health check + one-click recovery** — Auto-detects corrupted config files and prompts restore (corrupted files are backed up automatically)
- 🌐 **Bilingual UI** — Built-in 中文 / English switcher in the login page top-right, persisted via localStorage
- 🔄 **OKX transient-error retry** — Exponential backoff on `50013` / TLS timeouts; idempotent startup ops (set position mode / leverage) don't block the main loop if temporarily failing
- 🔐 **Brand integrity** — HMAC-SHA256 protected version and copyright info

> ⚠️ **Risk warning**: Crypto perpetual trading is highly risky. This project is for educational purposes only — you are solely responsible for any losses. **Always test on the OKX demo account first.**

---

## 🚀 Quick Start

### 1. Requirements
- Python 3.10+
- macOS / Linux / WSL
- OKX account + API Key (start with the [demo trading account](https://www.okx.com/trade-demo/))
- At least one LLM API key (DeepSeek recommended for best price/performance)

### 2. Install

```bash
git clone https://github.com/<your-org>/okx-bot.git
cd okx-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env: fill in OKX credentials + LLM API keys + Web UI login
```

Generate WEBUI_SECRET:
```bash
python -c "import secrets;print(secrets.token_hex(32))"
```

### 4. Launch the Web Console

```bash
set -a; source .env; set +a
python -m uvicorn webui:app --host 0.0.0.0 --port 8765
```

Open <http://localhost:8765> and log in with `WEBUI_USER` / `WEBUI_PASS` from `.env`. From here you can:
- Use the top **Start / Stop / Reset** buttons to manage the bot lifecycle
- **Logs** tab — live SSE log stream, plus historical date switcher with chunked load-older
- **Stats** tab — daily P&L line chart and detail table
- **Params** tab — symbol / leverage / LLM provider; **LLM Keys** panel shows only the currently selected provider
- **Prompt** tab — edit the AI prompt (hot-reloads on save, no restart needed)
- Switch **中文 / English** in the login page (top-right)

### 5. Command-line usage (no Web UI required)

```bash
# Run the bot directly
python main.py

# Daily P&L
python stats.py today
python stats.py week
python stats.py 2026-05-20

# One-shot reset (cancel orders + market-close positions + wipe DB)
python reset.py

# Test OKX connectivity
python check_conn.py
```

---

## ⚙️ Configuration Reference

All editable params live in the Web UI's **Params** tab. Saving writes to `runtime_config.json`; **restart the bot for changes to take effect**.

### Basic

| Field | Default | Description |
|---|---|---|
| `OKX_DOMAIN` | `https://www.okx.com` | Regional API endpoint (Global / AWS / US-AU / EEA) |
| `INST_ID` | `ETH-USDT-SWAP` | Trading symbol (16 popular USDT-margined perps in dropdown) |
| `TOTAL_USDT` | `2000` | Total capital |
| `LEVERAGE` | `2` | Leverage multiplier |
| `GRID_COUNT` | `20` | Mechanical grid count |
| `RANGE_PCT` | `0.20` | Grid range ±20% |
| `STOP_LOSS_PCT` | `0.10` | Stop-loss percentage |
| `POLL_INTERVAL` | `3` | Order status polling interval (sec) |
| `AI_KLINE_BAR` | `1m` | Kline timeframe |
| `AI_KLINE_LIMIT` | `60` | Number of klines to fetch |
| `AI_MAX_ORDERS_PER_CALL` | `20` | Max order pairs per AI decision |

### LLM

| Field | Default | Description |
|---|---|---|
| `AI_PROVIDER` | `deepseek` | Provider: deepseek / openai / anthropic / gemini / qwen / moonshot / glm |
| `AI_MODEL` | `deepseek-chat` | Model name (per-provider default, or custom) |
| `AI_DRIVEN_MODE` | `true` | Enable AI-driven mode |
| `AI_ENABLED` | `true` | Master AI switch |
| `AI_INTERVAL_SEC` | `60` | AI decision interval (sec) |
| `AI_REQUEST_TIMEOUT` | `30` | LLM request timeout |
| `AI_VERIFY_SSL` | `false` | SSL verification (`false` for MITM proxy environments) |

### LLM Keys
After switching `AI_PROVIDER`, the key panel auto-shows the relevant provider only. Keys are saved to `llm_keys.json` with 0600 permissions.

---

## 📐 Architecture

```
┌─────────────┐      ┌──────────────┐      ┌──────────────┐
│  Web UI     │◄────►│  webui.py    │◄────►│  main.py     │
│ (HTML/JS)   │      │  FastAPI     │      │  Bot process │
└─────────────┘      └──────┬───────┘      └──────┬───────┘
                            │                      │
                ┌───────────┴────────┐    ┌────────┴────────┐
                ▼                    ▼    ▼                 ▼
        ┌──────────────┐  ┌────────────────┐  ┌──────────────┐
        │ stats_db.py  │  │ ai_advisor.py  │  │   grid.py    │
        │ P&L cache    │  │ Multi-LLM      │  │ Grid + AI    │
        └──────────────┘  └────────┬───────┘  │  scheduler   │
                                   │           └──────┬───────┘
                                   ▼                  ▼
                          ┌────────────────┐  ┌──────────────┐
                          │ llm_keys.py    │  │  client.py   │
                          │ 7-provider     │  │  OKX SDK     │
                          │  adapters      │  │              │
                          └────────────────┘  └──────────────┘
```

| Module | Responsibility |
|---|---|
| `main.py` | Bot entrypoint: PID file, init, main loop |
| `grid.py` | Order lifecycle, reconciliation, AI scheduling, retry/backoff |
| `ai_advisor.py` | Kline/indicator collection, prompt building, multi-LLM dispatch |
| `llm_keys.py` | Per-provider API metadata + key storage |
| `config_loader.py` | `config.py` defaults + `runtime_config.json` overlay |
| `db.py` / `stats_db.py` | SQLite persistence (order state / daily P&L) |
| `webui.py` | FastAPI: login, lifecycle, config, log SSE, health checks |
| `client.py` | OKX SDK client factory (regional domain support) |
| `stats.py` | CLI daily P&L tool |
| `reset.py` | Cancel all orders + market-close + wipe DB |

---

## 🔌 LLM Providers

| Provider | Default Model | Signup |
|---|---|---|
| DeepSeek | `deepseek-chat` | <https://platform.deepseek.com> |
| OpenAI | `gpt-4o-mini` | <https://platform.openai.com> |
| Anthropic | `claude-haiku-4-5` | <https://console.anthropic.com> |
| Google Gemini | `gemini-2.0-flash` | <https://aistudio.google.com/apikey> |
| Qwen | `qwen-turbo` | <https://dashscope.console.aliyun.com> |
| Moonshot | `moonshot-v1-8k` | <https://platform.moonshot.cn> |
| Zhipu GLM | `glm-4-flash` | <https://open.bigmodel.cn> |

Switch via Web UI → **Params → LLM → AI_PROVIDER**, then configure the key in the **LLM Keys** panel below.

---

## 🌐 OKX Regional Endpoints

OKX requires region-specific API domains; using the wrong one will fail authentication:

| Domain | Audience |
|---|---|
| `https://www.okx.com` | Global (default; mainland China requires a proxy) |
| `https://aws.okx.com` | AWS node, lower-latency |
| `https://us.okx.com` | US / Australia (registered at app.okx.com) |
| `https://eea.okx.com` | EEA (registered at my.okx.com) |

Switch via Web UI → **Params → OKX_DOMAIN**.

---

## 🔒 Deployment Security

- 🌍 **Deploy on an overseas server**: OKX APIs from mainland China suffer from latency and unstable TLS handshakes — use AWS Tokyo / Singapore / Hong Kong or similar regions instead
- **Do not** expose port 8765 directly on the public internet. Put it behind Caddy / Nginx with HTTPS + an extra layer of Basic Auth or IP allowlist
- The default password `please-change-me` must be changed. Generate `WEBUI_SECRET` with `python -c "import secrets;print(secrets.token_hex(32))"`
- `.env`, `runtime_config.json`, `llm_keys.json`, `bot.pid`, `grid.db`, `stats.db`, `logs/` are all gitignored
- `llm_keys.json` is chmod 0600 (owner-readable only)

---

## 🛟 Troubleshooting

| Symptom | Resolution |
|---|---|
| `50013 Systems are busy` | OKX is overloaded — retried automatically (exponential backoff, 5–8 attempts) |
| `TLS handshake timeout` | Transient network — retries on startup; live AI calls skip that cycle |
| All orders rejected `placed=0` | OKX-side rejection (overload or invalid params); logged as ERROR `⚠ all placements failed` |
| `runtime_config.json parse error` | Web UI shows a top banner — click **Initialize** (corrupted file backed up as `.corrupt.<ts>`) |
| `prompts/scalp.txt missing placeholders` | Same — one-click restore from `prompts/scalp.default.txt` (old file backed up as `.bak.<ts>`) |

---

## 🧪 Development

```bash
# Webui dev mode (auto-reload)
python -m uvicorn webui:app --reload --port 8765

# Single AI decision (dry run)
python -c "import ai_advisor; print(ai_advisor._refresh_once())"

# Live log
tail -f logs/grid_$(date +%Y-%m-%d).log
```

---

## 📂 Project Layout

```
okx-bot/
├── main.py                 # Bot entrypoint
├── webui.py                # Web console backend
├── grid.py                 # Grid strategy core
├── ai_advisor.py           # AI decision module
├── llm_keys.py             # Multi-provider key store
├── config.py               # Defaults (do not edit; edit runtime_config.json)
├── config_loader.py        # Config loader (with overlay)
├── client.py               # OKX SDK client factory
├── db.py                   # Trade state SQLite
├── stats_db.py             # Daily P&L SQLite
├── stats.py                # CLI stats tool
├── notify.py               # Log alerting
├── reset.py                # Reset script
├── branding.py             # Version/copyright (HMAC-signed)
├── prompts/
│   ├── scalp.txt           # AI prompt (hot-editable)
│   └── scalp.default.txt   # Default template (used for init)
├── static/
│   └── index.html          # Web console frontend
├── .env.example            # Environment variable template
├── requirements.txt
├── LICENSE
└── README.md / README.en.md
```

---

## 📜 License

[MIT](LICENSE) © 2026 [AIPrompt](https://www.aiprompt.vip/)

---

<div align="center">
Make AI apps more valuable · Powered by <a href="https://www.aiprompt.vip/">AIPrompt</a>
</div>
