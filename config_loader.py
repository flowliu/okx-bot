# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""运行时配置 overlay：把 config.py 视为默认，runtime_config.json 覆盖之。

- bot 代码原本用 `config.XXX` 的地方，改成 `config_loader.get('XXX')`
- 也提供 `config_loader.cfg.XXX` 属性形式访问（向后兼容）
- 每次 get 都读最新 JSON（轻量、文件小、毫秒级 IO，无性能瓶颈）
- 路径在 .json 不存在时退化为 config.py
"""
from __future__ import annotations
import json
import os
import threading
import time
from typing import Any

import config as _defaults

RUNTIME_FILE = os.path.join(os.path.dirname(__file__), "runtime_config.json")
_lock = threading.Lock()


def _load_overlay() -> dict[str, Any]:
    if not os.path.exists(RUNTIME_FILE):
        return {}
    try:
        with open(RUNTIME_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def get(key: str, default: Any = None) -> Any:
    overlay = _load_overlay()
    if key in overlay:
        return overlay[key]
    return getattr(_defaults, key, default)


def all_config() -> dict[str, Any]:
    """返回当前生效的完整配置（含 overlay）。给 webui 显示用。"""
    out: dict[str, Any] = {}
    # config.py 里所有大写常量
    for name in dir(_defaults):
        if name.isupper() and not name.startswith("_"):
            out[name] = getattr(_defaults, name)
    out.update(_load_overlay())
    return out


def set_overlay(updates: dict[str, Any]) -> None:
    """合并写入 overlay。已存在的 key 覆盖，新增的添加。"""
    with _lock:
        current = _load_overlay()
        current.update(updates)
        with open(RUNTIME_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2, sort_keys=True)


def clear_overlay() -> None:
    with _lock:
        if os.path.exists(RUNTIME_FILE):
            os.remove(RUNTIME_FILE)


def status() -> dict[str, Any]:
    """返回 runtime_config.json 当前状态。"""
    info = {"file": RUNTIME_FILE, "name": "runtime_config.json"}
    if not os.path.exists(RUNTIME_FILE):
        return {**info, "ok": True, "exists": False, "error": "",
                "note": "未生成（首次使用 / 全部用默认值）"}
    try:
        with open(RUNTIME_FILE, "r", encoding="utf-8") as f:
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
    """重置 runtime_config.json 为空 overlay。返回备份路径(若有)。"""
    backup_path = ""
    with _lock:
        if backup and os.path.exists(RUNTIME_FILE):
            backup_path = f"{RUNTIME_FILE}.corrupt.{int(time.time())}"
            try:
                os.rename(RUNTIME_FILE, backup_path)
            except OSError:
                backup_path = ""
        with open(RUNTIME_FILE, "w", encoding="utf-8") as f:
            f.write("{}\n")
    return backup_path


class _CfgProxy:
    """让 `cfg.AI_KLINE_BAR` 形式也走 overlay。"""
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return get(name)


cfg = _CfgProxy()
