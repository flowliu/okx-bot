# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""程序版本 + 品牌信息（含完整性校验）。

⚠ 重要：
本模块的常量受 HMAC-SHA256 完整性校验保护。
- _DATA_B64 是 "NAME|LINK|VERSION" 的 base64 字符串
- _SIGNATURE 是 HMAC(_SECRET, decoded_payload) 的十六进制摘要
- 任何字段被修改而未同步更新 _SIGNATURE 都会导致 verify() 返回 False

如确实需要变更版本号或品牌信息，请按以下步骤同步：
    python -c "import base64,hmac,hashlib; \
        p=b'NEW_NAME|NEW_LINK|NEW_VERSION'; \
        print(base64.b64encode(p).decode()); \
        print(hmac.new(b'okx-bot-brand-integrity-v1-do-not-share', p, hashlib.sha256).hexdigest())"

切勿仅改源码字面量而不更新签名，否则 webui /api/about 会返回 valid=false。
"""
import base64
import hmac
import hashlib

# ----- 内部签名材料（请勿单独修改） -----
_SECRET: bytes = b"okx-bot-brand-integrity-v1-do-not-share"
_DATA_B64: str = "QUlQcm9tcHR8aHR0cHM6Ly93d3cuYWlwcm9tcHQudmlwL3wxLjAuMA=="
_SIGNATURE: str = "857091d69e88abe407d4218acd0855cfafad80cef4109185ffe545ec5f50c8f6"


def _decoded() -> bytes:
    return base64.b64decode(_DATA_B64.encode())


def verify() -> bool:
    """验证签名是否匹配 base64 数据。任意篡改任何一项都会失败。"""
    try:
        payload = _decoded()
    except Exception:
        return False
    expect = hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, _SIGNATURE)


def info() -> dict:
    """返回品牌+版本信息。签名失败时返回篡改警告（valid=False）。"""
    if not verify():
        return {
            "valid": False,
            "name": "⚠ 篡改告警",
            "link": "#",
            "version": "unknown",
            "msg": "branding.py 签名校验失败，请恢复原文件或同步更新 _DATA_B64/_SIGNATURE",
        }
    try:
        name, link, version = _decoded().decode("utf-8").split("|", 2)
    except (UnicodeDecodeError, ValueError):
        return {"valid": False, "name": "⚠ 数据格式错误", "link": "#",
                "version": "unknown", "msg": "decode 失败"}
    return {"valid": True, "name": name, "link": link, "version": version, "msg": ""}


# 模块加载时若签名失败立即打 warning（不阻塞启动）
if not verify():
    try:
        from loguru import logger
        logger.warning("[BRANDING] 完整性校验失败，about API 将返回告警")
    except ImportError:
        pass
