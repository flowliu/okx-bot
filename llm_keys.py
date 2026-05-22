# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""LLM provider 配置（API key + 默认模型）。

key 文件 llm_keys.json 与 .env 隔离，方便 Web UI 单独管理。
读取优先级：llm_keys.json > 环境变量 > 空串。
"""
import json
import os
import threading
import time
from typing import Any

KEYS_FILE = os.path.join(os.path.dirname(__file__), "llm_keys.json")
_lock = threading.Lock()

# provider 元数据：
#   url    OpenAI 兼容的 chat/completions 端点；anthropic/gemini 在 ai_advisor 里单独处理
#   model  默认模型名，UI 可改
#   env    环境变量回退名
#   style  openai | anthropic | gemini —— ai_advisor 据此选择序列化方式
PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "env": "DEEPSEEK_API_KEY",
        "style": "openai",
        "label": "DeepSeek",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "env": "OPENAI_API_KEY",
        "style": "openai",
        "label": "OpenAI",
    },
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-haiku-4-5",
        "env": "ANTHROPIC_API_KEY",
        "style": "anthropic",
        "label": "Anthropic",
    },
    "gemini": {
        # URL 拼接时会带上 model，置空占位
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "model": "gemini-2.0-flash",
        "env": "GEMINI_API_KEY",
        "style": "gemini",
        "label": "Google Gemini",
    },
    "qwen": {
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-turbo",
        "env": "QWEN_API_KEY",
        "style": "openai",
        "label": "通义千问 (Qwen)",
    },
    "moonshot": {
        "url": "https://api.moonshot.cn/v1/chat/completions",
        "model": "moonshot-v1-8k",
        "env": "MOONSHOT_API_KEY",
        "style": "openai",
        "label": "Moonshot",
    },
    "glm": {
        "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4-flash",
        "env": "GLM_API_KEY",
        "style": "openai",
        "label": "智谱 GLM",
    },
}


def _load() -> dict[str, Any]:
    if not os.path.exists(KEYS_FILE):
        return {}
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, Any]) -> None:
    tmp = KEYS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, KEYS_FILE)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except OSError:
        pass


def get_key(provider: str) -> str:
    """读取 provider 的 key：llm_keys.json > 环境变量。"""
    provider = provider.lower()
    if provider not in PROVIDERS:
        return ""
    with _lock:
        data = _load()
    k = (data.get("keys", {}) or {}).get(provider, "").strip()
    if k:
        return k
    env_name = PROVIDERS[provider]["env"]
    return os.getenv(env_name, "").strip()


def set_key(provider: str, key: str) -> None:
    """空字符串表示清除，回退到环境变量。"""
    provider = provider.lower()
    if provider not in PROVIDERS:
        raise ValueError(f"未知 provider: {provider}")
    with _lock:
        data = _load()
        keys = data.setdefault("keys", {})
        if key:
            keys[provider] = key.strip()
        else:
            keys.pop(provider, None)
        _save(data)


def has_key(provider: str) -> bool:
    return bool(get_key(provider))


def file_status() -> dict[str, Any]:
    """返回 llm_keys.json 文件级状态（用于健康检查 / 初始化提示）。"""
    info = {"file": KEYS_FILE, "name": "llm_keys.json"}
    if not os.path.exists(KEYS_FILE):
        return {**info, "ok": True, "exists": False, "error": "",
                "note": "未生成（首次使用 / 全部走环境变量回退）"}
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {**info, "ok": False, "exists": True,
                    "error": "顶层必须是 JSON 对象"}
        return {**info, "ok": True, "exists": True, "error": ""}
    except json.JSONDecodeError as e:
        return {**info, "ok": False, "exists": True,
                "error": f"JSON 解析失败：{e}"}
    except OSError as e:
        return {**info, "ok": False, "exists": True,
                "error": f"读取失败：{e}"}


def init_file(backup: bool = True) -> str:
    """重置 llm_keys.json 为空。返回备份路径(若有)。"""
    backup_path = ""
    with _lock:
        if backup and os.path.exists(KEYS_FILE):
            backup_path = f"{KEYS_FILE}.corrupt.{int(time.time())}"
            try:
                os.rename(KEYS_FILE, backup_path)
            except OSError:
                backup_path = ""
        _save({})
    return backup_path


def status_all() -> dict[str, dict]:
    """UI 用：每个 provider 是否已配置 key + 来源（local/env/none）。"""
    out = {}
    for p in PROVIDERS:
        env_name = PROVIDERS[p]["env"]
        with _lock:
            data = _load()
        local = bool((data.get("keys", {}) or {}).get(p, "").strip())
        env_val = bool(os.getenv(env_name, "").strip())
        if local:
            source = "local"
        elif env_val:
            source = "env"
        else:
            source = "none"
        out[p] = {
            "label": PROVIDERS[p]["label"],
            "default_model": PROVIDERS[p]["model"],
            "configured": local or env_val,
            "source": source,
            "env_name": env_name,
        }
    return out
