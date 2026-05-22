# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""路径与运行时常量。

DATA_DIR  — 用户态持久化目录（默认 CWD），可用 ORBITAI_DATA 环境变量覆盖。
PKG_DIR   — orbitai 包安装目录（src/orbitai），用于读静态资源 / 默认 prompt。
PROMPT_TEMPLATE_PACKAGE  — 默认 prompt 模板的包资源位置。
"""
import os
from pathlib import Path

# 用户工作目录：grid.db / stats.db / runtime_config.json / llm_keys.json /
# bot.pid / logs/ / prompts/scalp.txt 都在这里
DATA_DIR: Path = Path(os.environ.get("ORBITAI_DATA", os.getcwd())).resolve()

# 包内路径：静态资源 + 默认 prompt 模板
PKG_DIR: Path = Path(__file__).parent.resolve()
WEB_STATIC_DIR: Path = PKG_DIR / "web" / "static"
PROMPT_DEFAULT_PACKAGE_FILE: Path = PKG_DIR / "web" / "prompts" / "scalp.default.txt"


def db_path(name: str) -> str:
    """grid.db / stats.db 等位于 DATA_DIR 根下。"""
    return str(DATA_DIR / name)


def runtime_config_path() -> Path:
    return DATA_DIR / "runtime_config.json"


def llm_keys_path() -> Path:
    return DATA_DIR / "llm_keys.json"


def pid_file_path() -> Path:
    return DATA_DIR / "bot.pid"


def logs_dir() -> Path:
    p = DATA_DIR / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_prompt_path() -> Path:
    """用户可编辑的 prompt（默认 ./prompts/scalp.txt）。"""
    return DATA_DIR / "prompts" / "scalp.txt"


def ensure_user_prompt() -> Path:
    """若用户 prompt 不存在则从包内 default 模板复制一份。返回最终路径。"""
    p = user_prompt_path()
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(PROMPT_DEFAULT_PACKAGE_FILE.read_bytes())
    return p
