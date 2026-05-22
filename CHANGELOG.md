# Changelog

All notable changes to **OrbitAI** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] — 2026-05-22

🎉 **首次开源发布 / Initial open source release**

### Added — 新增

**核心策略 / Core Strategy**
- AI 驱动的中性网格交易策略，支持趋势 / 震荡 / 反转自适应
- AI driven neutral grid trading with adaptive trend / range / reversal modes
- 技术指标：RSI(14)、EMA(20/60)、ATR(14)、Bollinger Bands(20, 2σ)、Volume Ratio
- 单轮最多 20 对订单，open/close 价位由 LLM 实时决策
- 市价开仓选项（追突破，受限 ≤30%）+ 利润空间自动 normalize（≥0.15%）

**多大模型 / Multi-LLM**
- 7 厂商支持：DeepSeek / OpenAI / Anthropic / Gemini / Qwen / Moonshot / GLM
- 三种 API 风格自动适配：OpenAI-compatible / Anthropic Messages / Gemini generateContent
- 运行时一键切换厂商；密钥独立存 `llm_keys.json`（权限 0600，gitignored）

**Web 控制台 / Web Console**
- FastAPI + 单页 HTML，浏览器即开即用
- 启动 / 停止 / 重置 Bot 生命周期管理（重置 = 撤单 + 平仓 + 清 grid.db）
- 实时日志 SSE 推送 + 历史日志分块加载（防大文件 IO 阻塞）
- 参数编辑（保存 → `runtime_config.json` overlay → 重启 Bot 生效）
- AI Prompt 在线编辑（保存即热生效，无需重启），含必需占位符校验
- 每日收益统计：折线图 + 明细表

**数据持久化 / Persistence**
- 本地 SQLite 缓存 OKX 账单，后台每 10 分钟同步最近 7 天
- 突破 OKX 仅保留 7 天历史的限制，支持回溯任意时段（1-365 天）
- 订单状态机持久化到 `grid.db`，支持崩溃后对账接管

**国际化 / i18n**
- 中文 / English 双语界面，~140 条翻译键
- 登录页右上角下拉切换，浏览器语言自动检测 + localStorage 记忆

**OKX 区域域名 / OKX Regional Endpoints**
- 4 个域名预设：`www` / `aws` / `us` / `eea`，下拉切换避免鉴权失败

**健康自检 / Health Check**
- 启动时自动检测 `runtime_config.json` / `llm_keys.json` / `prompts/scalp.txt` 完整性
- 损坏文件一键初始化（旧文件自动备份为 `.corrupt.<ts>` / `.bak.<ts>`）
- Web UI 顶部琥珀色横幅可视化提示

**容错与重试 / Reliability**
- OKX `50013 Systems are busy` / TLS 超时等临时错误指数退避重试
- 启动期幂等设置（持仓模式、杠杆）失败不阻塞主循环
- AI 调用失败本轮跳过，不影响后续

**安全加固 / Security**
- 弱密码 / 短 `WEBUI_SECRET` 启动期硬校验拒绝运行
- 登录限流：5 次失败按 IP 锁定 10 分钟（429 响应）
- `hmac.compare_digest` 常数时间凭证比较
- 登录成功后旋转 session（防 fixation）
- `SameSite=Lax` cookie + 8 小时 session 上限
- 安全响应头：CSP / X-Frame-Options / X-Content-Type-Options / Referrer-Policy / HSTS（HTTPS 模式）
- 关闭 FastAPI 文档端点（`/docs` / `/redoc` / `/openapi.json`）
- CDN 资源 SRI（Chart.js）+ Tailwind Play CDN 警告
- 日志路径穿越三道防线（字符黑名单 + 文件名白名单 + `resolve()` 检查）
- Subprocess 文件描述符 `with` 块管理，无 fd 泄露

**品牌完整性 / Branding Integrity**
- HMAC-SHA256 签名版本号 / 版权信息，篡改任意字段都会校验失败
- 页脚动态从 `/api/about` 加载，可视化展示版本

**CLI 工具 / CLI Tools**
- `python main.py` — 直接启动 Bot（不经 Web UI）
- `python stats.py today|yesterday|week|YYYY-MM-DD` — 日收益统计
- `python reset.py` — 撤单 + 平仓 + 清 DB
- `python check_conn.py` — OKX 连通性测试

**文档 / Docs**
- README 中英双语（含架构图、配置详解、故障排查、部署安全）
- MIT License + 双语风险免责声明
- `.env.example` 完整模板

### Security 风险提示
- 默认 `WEBUI_USER=admin`，密码必须改成强密码（启动期会拒绝弱密码）
- `WEBUI_SECRET` 必须用 `python -c "import secrets;print(secrets.token_hex(32))"` 生成
- 切勿将 8765 端口直接暴露公网；生产环境务必走 Nginx/Caddy + HTTPS
- 投入真实资金前先用 OKX 模拟盘充分测试

---

## 版本号说明 / Versioning

- `X.0.0` — 主版本号：不兼容的 API 变更 / 数据库结构变更
- `0.X.0` — 次版本号：新功能、向后兼容
- `0.0.X` — 修订号：bug 修复 / 安全补丁

修改 `branding.py` 的版本号时，必须同步重算 `_DATA_B64` 和 `_SIGNATURE`：

```bash
python -c "import base64,hmac,hashlib; \
  p=b'OrbitAI|https://www.aiprompt.vip/|1.2.3'; \
  print(base64.b64encode(p).decode()); \
  print(hmac.new(b'okx-bot-brand-integrity-v1-do-not-share', p, hashlib.sha256).hexdigest())"
```

---

[1.0.0]: https://github.com/flowliu/okx-bot/releases/tag/v1.0.0
