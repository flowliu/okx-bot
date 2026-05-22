# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""OKX Bot Web UI

启动：
    .venv/bin/python -m uvicorn webui:app --host 0.0.0.0 --port 8765

环境变量：
    WEBUI_USER     登录用户名（默认 admin）
    WEBUI_PASS     登录密码（默认 changeme）
    WEBUI_SECRET   session 签名密钥（生产必填）
"""
import hmac
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psutil  # 跨平台进程管理

IS_WINDOWS = sys.platform == "win32"
# Popen 跨平台参数：新建独立进程组以隔离信号 / 控制台
if IS_WINDOWS:
    POPEN_EXTRA = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
else:
    POPEN_EXTRA = {"start_new_session": True}

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from orbitai.config import branding as branding
from orbitai.config import loader as config_loader
from orbitai.config import llm_keys as llm_keys
from orbitai.cli import stats as stats_mod
from orbitai.data import stats_db as stats_db

from orbitai import runtime as _rt

# 运行时数据目录（用户态，可写）
ROOT = _rt.DATA_DIR
PID_FILE = _rt.pid_file_path()
LOG_DIR = _rt.logs_dir()
PROMPT_FILE = _rt.user_prompt_path()
# 默认 prompt 模板：包内打包资源（只读）
PROMPT_DEFAULT_FILE = _rt.PROMPT_DEFAULT_PACKAGE_FILE
# 静态资源：包内打包
STATIC_DIR = _rt.WEB_STATIC_DIR

REQUIRED_PLACEHOLDERS = ("{inst_id}", "{bar}", "{last_px", "{max_n}", "{kl_lines}")

WEBUI_USER = os.getenv("WEBUI_USER", "admin")
WEBUI_PASS = os.getenv("WEBUI_PASS", "")
WEBUI_SECRET = os.getenv("WEBUI_SECRET", "")
WEBUI_HTTPS_ONLY = os.getenv("WEBUI_HTTPS_ONLY", "0").strip() == "1"
# 登录限流配置
LOGIN_MAX_FAILS = int(os.getenv("WEBUI_LOGIN_MAX_FAILS", "5"))
LOGIN_LOCKOUT_SEC = int(os.getenv("WEBUI_LOGIN_LOCKOUT_SEC", "600"))  # 10 min

# 启动期硬校验：默认密码 / 空 secret 会让 webui 处于明显不安全状态，直接拒绝启动
if not WEBUI_PASS or WEBUI_PASS in ("changeme", "please-change-me", "okxbot", "admin", "123456"):
    raise RuntimeError(
        "❌ WEBUI_PASS 未设置或使用了弱密码。请在 .env 中配置一个强密码后再启动 webui。"
    )
if not WEBUI_SECRET or len(WEBUI_SECRET) < 32:
    raise RuntimeError(
        "❌ WEBUI_SECRET 缺失或长度不足 32。请用 "
        "`python -c \"import secrets;print(secrets.token_hex(32))\"` 生成。"
    )

# 登录失败次数：{ip: [(timestamp, ...), ...]}
_login_failures: dict[str, list[float]] = defaultdict(list)
_login_lock = threading.Lock()

_bot_popen: subprocess.Popen | None = None

# ---------- 请求签名（防 CSRF + 防重放）----------
# 写接口必须带 X-Timestamp / X-Nonce / X-Sign，签名 = HMAC-SHA256(sign_key, msg)
# msg = ts + "\n" + nonce + "\n" + method + "\n" + path + "\n" + sha256(body)
SIGN_WINDOW_SEC = 300
import hashlib  # 顶部已 import hmac

_seen_nonces: dict[str, float] = {}
_nonce_lock = threading.Lock()


def _purge_expired_nonces() -> None:
    """清理过期 nonce（一次性清理，触发于每次签名验证）。"""
    cutoff = time.time() - SIGN_WINDOW_SEC * 2
    with _nonce_lock:
        for n in [k for k, t in _seen_nonces.items() if t < cutoff]:
            _seen_nonces.pop(n, None)

EDITABLE_KEYS = {
    # 第一行：域名 + 标的
    "OKX_DOMAIN": str,
    "INST_ID": str,
    # 第二行：厂商 + 模型
    "AI_PROVIDER": str,
    "AI_MODEL": str,
    # 资金/杠杆
    "TOTAL_USDT": float, "LEVERAGE": int,
    # 机械网格
    "GRID_COUNT": int, "RANGE_PCT": float, "STOP_LOSS_PCT": float,
    # 主循环轮询间隔
    "POLL_INTERVAL": int,
    # AI 模式
    "AI_DRIVEN_MODE": bool, "AI_ENABLED": bool,
    "AI_INTERVAL_SEC": int, "AI_KLINE_BAR": str, "AI_KLINE_LIMIT": int,
    "AI_MAX_ORDERS_PER_CALL": int,
    "AI_REQUEST_TIMEOUT": int, "AI_VERIFY_SSL": bool,
}

app = FastAPI(title="OKX Bot Console", docs_url=None, redoc_url=None, openapi_url=None)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """补全常见安全响应头。"""
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        # 注：用了 tailwindcss + chart.js CDN，需要允许相应来源
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' https://cdn.tailwindcss.com 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' https://cdn.tailwindcss.com; "
            "frame-ancestors 'none';"
        )
        if WEBUI_HTTPS_ONLY:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=WEBUI_SECRET,
    https_only=WEBUI_HTTPS_ONLY,
    same_site="lax",  # 跨站表单 POST 不会带 cookie，防 CSRF
    max_age=8 * 3600,  # 8h，避免长期 session 被劫持
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------- helpers ----------

def require_login(request: Request) -> None:
    if not request.session.get("user"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")


async def require_signed(request: Request) -> None:
    """敏感写接口：必须已登录 + 带合法的 X-Timestamp/X-Nonce/X-Sign 头。"""
    require_login(request)
    sign_key = request.session.get("sign_key")
    if not sign_key:
        # 旧 session 无 sign_key —— 强制重新登录
        raise HTTPException(401, "签名密钥缺失，请重新登录")

    ts = request.headers.get("x-timestamp", "")
    nonce = request.headers.get("x-nonce", "")
    sig = request.headers.get("x-sign", "")
    if not (ts and nonce and sig):
        raise HTTPException(400, "缺少签名头 (X-Timestamp / X-Nonce / X-Sign)")

    try:
        ts_int = int(ts)
    except ValueError:
        raise HTTPException(400, "X-Timestamp 必须是 unix 秒整数")

    now = int(time.time())
    if abs(now - ts_int) > SIGN_WINDOW_SEC:
        raise HTTPException(400, f"请求时间偏差过大 ({now - ts_int}s)，请检查客户端时钟")

    # nonce 防重放
    _purge_expired_nonces()
    with _nonce_lock:
        if nonce in _seen_nonces:
            raise HTTPException(400, "nonce 已被使用（防重放）")
        # 长度防呆：避免恶意塞超大 key
        if len(nonce) > 64:
            raise HTTPException(400, "nonce 过长")
        _seen_nonces[nonce] = time.time()

    body = await request.body()
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"{ts}\n{nonce}\n{request.method.upper()}\n{request.url.path}\n{body_hash}"
    expected = hmac.new(sign_key.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(401, "签名验证失败")


def _reap_if_zombie() -> bool:
    """收割我们 Popen 出的僵尸子进程；返回是否已退出。"""
    global _bot_popen
    if _bot_popen is None:
        return False
    rc = _bot_popen.poll()
    if rc is not None:
        _bot_popen = None
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return True
    return False


def bot_pid() -> int | None:
    """跨平台进程探活。
    用 psutil 替代 os.kill(pid, 0) —— 在 Windows 上后者会触发 TerminateProcess 杀进程。
    """
    _reap_if_zombie()
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    try:
        proc = psutil.Process(pid)
        # 僵尸进程（POSIX 概念）视为已退出
        if proc.status() == psutil.STATUS_ZOMBIE:
            raise psutil.NoSuchProcess(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return None
    return pid


def latest_log_file() -> Path | None:
    if not LOG_DIR.exists():
        return None
    candidates = sorted(LOG_DIR.glob("grid_*.log"))
    return candidates[-1] if candidates else None


def coerce(key: str, raw: Any) -> Any:
    t = EDITABLE_KEYS[key]
    if t is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).lower() in ("1", "true", "yes", "on")
    if t is int:
        return int(raw)
    if t is float:
        return float(raw)
    return str(raw)


# ---------- 静态页 ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    page = STATIC_DIR / "index.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>UI 未部署</h1><p>请确认 static/index.html 存在。</p>", status_code=500)


# ---------- 登录 ----------

def _client_ip(request: Request) -> str:
    # 注意：本服务不应直接暴露公网。生产环境前面应有 nginx/caddy 反代，X-Forwarded-For 可信
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_login_lockout(ip: str) -> int:
    """返回剩余冷却秒数。0 表示未锁定。"""
    now = time.time()
    with _login_lock:
        # 清掉过期失败记录
        fails = [t for t in _login_failures[ip] if now - t < LOGIN_LOCKOUT_SEC]
        _login_failures[ip] = fails
        if len(fails) >= LOGIN_MAX_FAILS:
            # 锁定到最早一次失败 + 冷却时间
            return int(LOGIN_LOCKOUT_SEC - (now - fails[0]))
    return 0


def _record_login_failure(ip: str) -> None:
    with _login_lock:
        _login_failures[ip].append(time.time())


def _clear_login_failures(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


@app.post("/api/login")
def api_login(request: Request, username: str = Form(...), password: str = Form(...)) -> JSONResponse:
    ip = _client_ip(request)
    lockout = _check_login_lockout(ip)
    if lockout > 0:
        return JSONResponse(
            {"ok": False, "error": f"登录失败次数过多，请 {lockout} 秒后再试"},
            status_code=429,
        )
    # 常数时间比较，避免 timing attack
    user_ok = hmac.compare_digest(username.encode(), WEBUI_USER.encode())
    pass_ok = hmac.compare_digest(password.encode(), WEBUI_PASS.encode())
    if user_ok and pass_ok:
        _clear_login_failures(ip)
        # 防 session fixation：登录成功后旋转一次 session id
        request.session.clear()
        request.session["user"] = username
        request.session["login_at"] = int(time.time())
        # 生成本次会话的签名密钥；前端缓存 + session 存一份，写接口验签用
        sign_key = secrets.token_hex(32)
        request.session["sign_key"] = sign_key
        return JSONResponse({"ok": True, "sign_key": sign_key})
    _record_login_failure(ip)
    return JSONResponse({"ok": False, "error": "用户名或密码错误"}, status_code=401)


@app.post("/api/logout")
def api_logout(request: Request) -> JSONResponse:
    request.session.clear()
    return JSONResponse({"ok": True})


@app.get("/api/me")
def api_me(request: Request) -> JSONResponse:
    """已登录的话顺带把 sign_key 回传一份，方便前端关 tab 重开或
    sessionStorage 被清后无感恢复（cookie 仍有效）。"""
    user = request.session.get("user")
    return JSONResponse({
        "user": user,
        "sign_key": request.session.get("sign_key") if user else None,
    })


# ---------- 状态 / 启停 ----------

@app.get("/api/status", dependencies=[Depends(require_login)])
def api_status() -> JSONResponse:
    pid = bot_pid()
    return JSONResponse({
        "running": pid is not None,
        "pid": pid,
        "log_file": str(latest_log_file() or ""),
    })


@app.post("/api/start", dependencies=[Depends(require_signed)])
def api_start() -> JSONResponse:
    if bot_pid() is not None:
        return JSONResponse({"ok": False, "error": "已在运行"}, status_code=409)
    # 用当前解释器（确保跟 webui 跑在同一个 venv），通过 -m 启动 cli 入口
    py = sys.executable
    LOG_DIR.mkdir(exist_ok=True)
    out = LOG_DIR / "bot.stdout.log"
    err = LOG_DIR / "bot.stderr.log"
    env = os.environ.copy()
    env["ORBITAI_DATA"] = str(ROOT)  # 子进程也使用同一份数据目录
    global _bot_popen
    # 用 with 块持有 fd, 子进程 fork 后父端就能关闭（POSIX 下子进程已 dup 了 fd）
    with open(out, "ab") as fout, open(err, "ab") as ferr:
        _bot_popen = subprocess.Popen(
            [py, "-m", "orbitai.cli.main"],
            cwd=str(ROOT),
            stdout=fout,
            stderr=ferr,
            env=env,
            **POPEN_EXTRA,
        )
    # 等 pid 文件落盘（最多 8 秒；TLS 握手 / OKX 调用 / venv 加载都可能稍慢）
    pid = None
    for _ in range(80):
        if _bot_popen.poll() is not None:
            # 子进程已退出（启动失败），无需再等 pid
            break
        pid = bot_pid()
        if pid is not None:
            break
        time.sleep(0.1)

    if pid is not None:
        return JSONResponse({"ok": True, "pid": pid})

    # 启动失败：把 stderr 尾巴返给前端，方便用户排查
    rc = _bot_popen.poll() if _bot_popen else None
    err_tail = ""
    try:
        err_text = err.read_text(encoding="utf-8", errors="replace")
        err_tail = "\n".join(err_text.strip().splitlines()[-20:])
    except OSError:
        pass
    return JSONResponse({
        "ok": False,
        "pid": None,
        "returncode": rc,
        "error": f"Bot 进程未能启动（returncode={rc}）。日志尾部：",
        "stderr_tail": err_tail or "（stderr 为空，看 logs/bot.stdout.log）",
    }, status_code=500)


def _terminate_bot(pid: int) -> None:
    """优雅停 bot 进程（跨平台）。

    POSIX: SIGTERM 让 grid.py 的 _signal_handler 跑撤单清理。
    Windows: 没有 SIGTERM，用 CTRL_BREAK_EVENT 发给进程组（前面 Popen 用了
    CREATE_NEW_PROCESS_GROUP），grid.py 的 _signal_handler 也会捕获到。
    """
    if IS_WINDOWS:
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except (OSError, AttributeError):
            # 退化为 terminate
            try:
                psutil.Process(pid).terminate()
            except psutil.Error:
                pass
    else:
        os.kill(pid, signal.SIGTERM)


def _force_kill_bot(pid: int) -> None:
    """超时兜底强杀。"""
    try:
        psutil.Process(pid).kill()  # 跨平台：POSIX=SIGKILL, Windows=TerminateProcess
    except psutil.Error:
        pass


@app.post("/api/stop", dependencies=[Depends(require_signed)])
def api_stop() -> JSONResponse:
    pid = bot_pid()
    if pid is None:
        return JSONResponse({"ok": False, "error": "未在运行"}, status_code=409)
    try:
        _terminate_bot(pid)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    # 等优雅退出，最多 30s（撤单 + 清理 db 可能耗时）
    for _ in range(300):
        if bot_pid() is None:
            return JSONResponse({"ok": True})
        time.sleep(0.1)
    # 兜底强杀
    _force_kill_bot(pid)
    for _ in range(20):
        if bot_pid() is None:
            break
        time.sleep(0.1)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return JSONResponse({"ok": True, "forced": True})


# ---------- 关于 / 版本 ----------

@app.get("/api/about")
def api_about() -> JSONResponse:
    """返回品牌信息 + 版本 + 服务器时区。可未登录访问（页脚 / 顶栏用）。"""
    info = branding.info()
    now = datetime.now().astimezone()
    info["tz"] = time.strftime("%Z")  # e.g. CST / UTC / JST
    info["tz_offset"] = now.strftime("%z")  # e.g. +0800
    return JSONResponse(info)


# ---------- 配置文件健康检查 / 初始化 ----------

def _prompt_status() -> dict:
    info = {"file": str(PROMPT_FILE), "name": "prompts/scalp.txt"}
    if not PROMPT_FILE.exists():
        return {**info, "ok": False, "exists": False,
                "error": "AI 提示词文件缺失（AI 调用会因为 user_msg 为空而失败）",
                "note": "可从 prompts/scalp.default.txt 初始化"}
    try:
        text = PROMPT_FILE.read_text(encoding="utf-8")
    except OSError as e:
        return {**info, "ok": False, "exists": True, "error": f"读取失败：{e}"}
    missing = [p for p in REQUIRED_PLACEHOLDERS if p not in text]
    if missing:
        return {**info, "ok": False, "exists": True,
                "error": f"缺少必需占位符：{', '.join(missing)}"}
    return {**info, "ok": True, "exists": True, "error": ""}


def _init_prompt(backup: bool = True) -> tuple[bool, str, str]:
    """复原 prompts/scalp.txt 为 prompts/scalp.default.txt。
    返回 (ok, backup_path, error)。
    """
    if not PROMPT_DEFAULT_FILE.exists():
        return False, "", "默认模板 prompts/scalp.default.txt 不存在，无法初始化"
    backup_path = ""
    PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if backup and PROMPT_FILE.exists():
        backup_path = str(PROMPT_FILE) + f".bak.{int(time.time())}"
        try:
            os.rename(PROMPT_FILE, backup_path)
        except OSError:
            backup_path = ""
    try:
        shutil.copy(PROMPT_DEFAULT_FILE, PROMPT_FILE)
        return True, backup_path, ""
    except OSError as e:
        return False, backup_path, f"复制失败：{e}"


@app.get("/api/health", dependencies=[Depends(require_login)])
def api_health() -> JSONResponse:
    """检查所有运行时配置文件状态。"""
    runtime = config_loader.status()
    keys = llm_keys.file_status()
    prompt = _prompt_status()
    items = [
        {"key": "runtime_config", **runtime},
        {"key": "llm_keys",       **keys},
        {"key": "prompt",         **prompt},
    ]
    all_ok = all(i["ok"] for i in items)
    return JSONResponse({"all_ok": all_ok, "items": items})


@app.post("/api/init", dependencies=[Depends(require_signed)])
async def api_init(request: Request) -> JSONResponse:
    """初始化指定配置文件。
    body: {"key": "runtime_config"|"llm_keys"|"prompt"}
    损坏的旧文件会被改名为 *.corrupt.<ts> / *.bak.<ts> 保留。
    """
    payload = await request.json()
    key = (payload.get("key") or "").strip()
    if key == "runtime_config":
        backup = config_loader.init_file()
        return JSONResponse({"ok": True, "key": key, "backup": backup,
                             "msg": "已重置为空 overlay"})
    if key == "llm_keys":
        backup = llm_keys.init_file()
        return JSONResponse({"ok": True, "key": key, "backup": backup,
                             "msg": "已重置为空密钥库（不影响 .env 里的回退）"})
    if key == "prompt":
        ok, backup, err = _init_prompt()
        if not ok:
            return JSONResponse({"ok": False, "key": key, "error": err}, status_code=500)
        return JSONResponse({"ok": True, "key": key, "backup": backup,
                             "msg": "已从 prompts/scalp.default.txt 还原"})
    raise HTTPException(400, "未知 key（仅支持 runtime_config / llm_keys / prompt）")


# ---------- 重置 ----------

@app.post("/api/reset", dependencies=[Depends(require_signed)])
def api_reset() -> JSONResponse:
    """运行 reset.py：撤所有挂单 + 平所有持仓 + 清 grid.db。
    Bot 必须先停掉，避免和主循环抢 OKX 接口。
    """
    if bot_pid() is not None:
        return JSONResponse({"ok": False, "error": "Bot 仍在运行，请先停止"}, status_code=409)
    env = os.environ.copy()
    env["ORBITAI_DATA"] = str(ROOT)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "orbitai.cli.reset"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "reset.py 超时（>120s）"}, status_code=504)
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(output.strip().splitlines()[-30:])
    return JSONResponse({
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": tail,
    }, status_code=200 if proc.returncode == 0 else 500)


# ---------- 配置 ----------

@app.get("/api/config", dependencies=[Depends(require_login)])
def api_get_config() -> JSONResponse:
    all_cfg = config_loader.all_config()
    editable = {k: all_cfg.get(k) for k in EDITABLE_KEYS}
    return JSONResponse({"editable": editable, "all": all_cfg})


@app.post("/api/config", dependencies=[Depends(require_signed)])
async def api_set_config(request: Request) -> JSONResponse:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "payload must be object")
    updates: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for k, v in payload.items():
        if k not in EDITABLE_KEYS:
            errors[k] = "字段不可编辑"
            continue
        try:
            updates[k] = coerce(k, v)
        except (TypeError, ValueError) as e:
            errors[k] = f"格式错误: {e}"
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    config_loader.set_overlay(updates)
    return JSONResponse({"ok": True, "updated": updates, "hint": "重启 Bot 生效"})


# ---------- LLM Provider Keys ----------

@app.get("/api/llm/keys", dependencies=[Depends(require_login)])
def api_llm_keys() -> JSONResponse:
    """返回各 provider 是否已配置 key（不返回 key 本身）+ 当前选中 provider。"""
    return JSONResponse({
        "current": (config_loader.get("AI_PROVIDER") or "deepseek").lower(),
        "providers": llm_keys.status_all(),
    })


@app.post("/api/llm/keys", dependencies=[Depends(require_signed)])
async def api_llm_set_key(request: Request) -> JSONResponse:
    """设置/清除某个 provider 的 key。{provider, key}；key 为空字符串=清除。"""
    payload = await request.json()
    provider = (payload.get("provider") or "").lower()
    key = payload.get("key", "")
    if provider not in llm_keys.PROVIDERS:
        raise HTTPException(400, "未知 provider")
    if not isinstance(key, str):
        raise HTTPException(400, "key 必须是字符串")
    llm_keys.set_key(provider, key.strip())
    return JSONResponse({"ok": True, "provider": provider, "configured": llm_keys.has_key(provider)})


# ---------- Prompt ----------

@app.get("/api/prompt", dependencies=[Depends(require_login)])
def api_get_prompt() -> JSONResponse:
    try:
        text = PROMPT_FILE.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"读取失败: {e}")
    return JSONResponse({"content": text, "path": str(PROMPT_FILE)})


@app.post("/api/prompt", dependencies=[Depends(require_signed)])
async def api_set_prompt(request: Request) -> JSONResponse:
    payload = await request.json()
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(400, "content 不能为空")
    # 简单校验：占位符必须存在
    required = ["{inst_id}", "{bar}", "{last_px", "{max_n}", "{kl_lines}"]
    missing = [t for t in required if t not in content]
    if missing:
        raise HTTPException(400, f"缺少必需占位符: {', '.join(missing)}")
    PROMPT_FILE.parent.mkdir(exist_ok=True)
    PROMPT_FILE.write_text(content, encoding="utf-8")
    return JSONResponse({"ok": True, "hint": "下一轮 AI 调用自动加载新 prompt"})


# ---------- 日志 SSE ----------

@app.get("/api/logs/stream", dependencies=[Depends(require_login)])
def api_logs_stream() -> StreamingResponse:
    def gen():
        path = latest_log_file()
        if path is None:
            yield "data: [日志文件尚未生成]\n\n"
            # 等文件出现
            while True:
                time.sleep(2)
                path = latest_log_file()
                if path is not None:
                    yield f"data: [找到日志 {path.name}]\n\n"
                    break
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            # 跳到末尾，只推新增
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    # 文件可能轮转
                    cur = latest_log_file()
                    if cur is not None and cur != path:
                        path = cur
                        f.close()
                        f = open(path, "r", encoding="utf-8", errors="replace")
                        yield f"data: [日志切换至 {path.name}]\n\n"
                    continue
                # SSE 每条消息以 data: 开头，\n\n 结束
                yield f"data: {line.rstrip()}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ---------- 统计 ----------

# 后台 sync 线程：每 10 分钟从 OKX 拉最近 7 天 upsert 到 stats.db
SYNC_INTERVAL_SEC = 600
SYNC_WINDOW_DAYS = 7
_sync_event = threading.Event()
_sync_running = threading.Lock()


def _sync_once() -> dict:
    """从 OKX 拉最近 7 天账单，按天聚合 upsert 到 stats.db。
    返回 {ok, days_synced, error}。
    """
    if not _sync_running.acquire(blocking=False):
        return {"ok": False, "error": "已有 sync 在跑"}
    try:
        today = datetime.now().date()
        day_list = [today - timedelta(days=i) for i in range(SYNC_WINDOW_DAYS - 1, -1, -1)]
        start = datetime.combine(day_list[0], datetime.min.time())
        end = datetime.combine(day_list[-1], datetime.min.time()) + timedelta(days=1)
        bills = stats_mod.fetch_bills_in_range(
            int(start.timestamp() * 1000),
            int(end.timestamp() * 1000),
        )
        by_day = stats_mod.summarize_by_day(bills)
        now_ts = int(time.time())
        for d in day_list:
            ds = d.strftime("%Y-%m-%d")
            b = by_day.get(ds, {"trades": 0, "closes": 0, "gross_pnl": 0.0, "fee": 0.0, "net_pnl": 0.0})
            stats_db.upsert_day({
                "date": ds,
                "trades": b["trades"],
                "closes": b["closes"],
                "gross_pnl": round(b["gross_pnl"], 4),
                "fee": round(b["fee"], 4),
                "net_pnl": round(b.get("net_pnl", b["gross_pnl"] + b["fee"]), 4),
            }, now_ts)
        stats_db.set_meta("last_sync_ts", str(now_ts))
        stats_db.set_meta("last_sync_err", "")
        return {"ok": True, "days_synced": len(day_list), "ts": now_ts}
    except Exception as e:
        stats_db.set_meta("last_sync_err", str(e)[:300])
        return {"ok": False, "error": str(e)}
    finally:
        _sync_running.release()


def _sync_loop() -> None:
    # 启动后立即跑一次，之后每 SYNC_INTERVAL_SEC 跑一次；_sync_event.set() 可立即触发
    while True:
        _sync_once()
        _sync_event.wait(timeout=SYNC_INTERVAL_SEC)
        _sync_event.clear()


@app.on_event("startup")
def _on_startup() -> None:
    stats_db.init_db()
    threading.Thread(target=_sync_loop, daemon=True, name="stats-sync").start()


@app.get("/api/stats", dependencies=[Depends(require_login)])
def api_stats(days: int = 7) -> JSONResponse:
    days = max(1, min(days, 730))  # 上限 2 年防呆
    today = datetime.now().date()
    start_d = today - timedelta(days=days - 1)
    db_rows = {r["date"]: r for r in stats_db.get_range(
        start_d.strftime("%Y-%m-%d"),
        today.strftime("%Y-%m-%d"),
    )}
    rows = []
    total_trades = total_closes = 0
    total_pnl = total_fee = 0.0
    for i in range(days):
        d = start_d + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        b = db_rows.get(ds, {
            "date": ds, "trades": 0, "closes": 0,
            "gross_pnl": 0.0, "fee": 0.0, "net_pnl": 0.0,
        })
        rows.append({
            "date": ds,
            "trades": b["trades"],
            "closes": b["closes"],
            "gross_pnl": b["gross_pnl"],
            "fee": b["fee"],
            "net_pnl": b["net_pnl"],
        })
        total_trades += b["trades"]
        total_closes += b["closes"]
        total_pnl += b["gross_pnl"]
        total_fee += b["fee"]
    last_sync_ts = stats_db.get_meta("last_sync_ts")
    last_sync_err = stats_db.get_meta("last_sync_err") or ""
    earliest = stats_db.earliest_date()

    # 今日 / 昨日数据（不受 days 范围限制，直接从 db 查）
    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    extra_dates = stats_db.get_range(yesterday_str, today_str)
    extra_map = {r["date"]: r for r in extra_dates}
    today_row = extra_map.get(today_str)
    yesterday_row = extra_map.get(yesterday_str)

    return JSONResponse({
        "days": days,
        "rows": rows,
        "total": {
            "trades": total_trades,
            "closes": total_closes,
            "gross_pnl": round(total_pnl, 4),
            "fee": round(total_fee, 4),
            "net_pnl": round(total_pnl + total_fee, 4),
        },
        "today": {
            "date": today_str,
            "trades": today_row["trades"] if today_row else 0,
            "net_pnl": today_row["net_pnl"] if today_row else 0.0,
        },
        "yesterday": {
            "date": yesterday_str,
            "trades": yesterday_row["trades"] if yesterday_row else 0,
            "net_pnl": yesterday_row["net_pnl"] if yesterday_row else 0.0,
        },
        "last_sync_ts": int(last_sync_ts) if last_sync_ts else None,
        "last_sync_err": last_sync_err,
        "earliest_date": earliest,
    })


@app.post("/api/stats/sync", dependencies=[Depends(require_signed)])
def api_stats_sync() -> JSONResponse:
    """前端「刷新」按钮：立即触发一次 sync 并等结果（最长 30s）。"""
    result = _sync_once()
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.get("/api/logs/tail", dependencies=[Depends(require_login)])
def api_logs_tail(n: int = 200) -> JSONResponse:
    path = latest_log_file()
    if path is None:
        return JSONResponse({"lines": [], "file": ""})
    n = max(1, min(n, 2000))
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return JSONResponse({"lines": [l.rstrip() for l in lines[-n:]], "file": path.name})


@app.get("/api/logs/files", dependencies=[Depends(require_login)])
def api_logs_files() -> JSONResponse:
    """列出所有 grid_*.log 文件，最新在前。"""
    items = []
    if LOG_DIR.exists():
        for p in sorted(LOG_DIR.glob("grid_*.log"), reverse=True):
            try:
                st = p.stat()
            except OSError:
                continue
            items.append({
                "name": p.name,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
    return JSONResponse({"files": items})


def _safe_log_path(name: str) -> Path:
    """防止路径穿越：必须匹配 grid_*.log 且解析后落在 LOG_DIR 内。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    if not (name.startswith("grid_") and name.endswith(".log")):
        raise HTTPException(400, "只能读 grid_*.log")
    p = (LOG_DIR / name).resolve()
    if not str(p).startswith(str(LOG_DIR.resolve())):
        raise HTTPException(400, "路径越界")
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    return p


@app.get("/api/logs/read", dependencies=[Depends(require_login)])
def api_logs_read(
    file: str,
    end: int = -1,
    chunk: int = 200_000,
) -> JSONResponse:
    """按字节分块读日志。

    参数:
        file:  grid_YYYY-MM-DD.log
        end:   读到此字节为止(不含)；-1=文件末尾
        chunk: 单次最多读多少字节，默认 ~200KB

    返回:
        size:   文件总字节
        start:  本块实际起始字节(对齐到行首)
        end:    本块结束字节
        lines:  本块行列表（已 rstrip）
        has_more_before:  是否还能向更早翻

    典型流程：
        1) 不传 end → 拿到最新尾巴
        2) 用响应里的 start 作为下次的 end，调一次 → 向上翻一页
    """
    path = _safe_log_path(file)
    size = path.stat().st_size
    chunk = max(1024, min(chunk, 2_000_000))  # 1KB~2MB
    if end < 0 or end > size:
        end = size
    start = max(0, end - chunk)
    aligned_start = start
    with open(path, "rb") as f:
        if start > 0:
            # 对齐到下一个行首：避免半行
            f.seek(start)
            f.readline()  # 丢掉这半行
            aligned_start = f.tell()
            if aligned_start >= end:  # chunk 内只有半行
                aligned_start = start
                f.seek(start)
        else:
            f.seek(0)
        data = f.read(end - f.tell())
    text = data.decode("utf-8", errors="replace")
    lines = [l for l in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()  # 末尾空串
    return JSONResponse({
        "file": file,
        "size": size,
        "start": aligned_start,
        "end": end,
        "lines": lines,
        "has_more_before": aligned_start > 0,
    })
