"""API Key 落库加密。

上游 key 一律以 Fernet（AES-128-CBC + HMAC）密文存储，主密钥来自
``STEP2API_SECRET``；未设置时自动生成 32 字节随机密钥并写入
``data_dir/.secret_key``（POSIX 下 chmod 600）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings

_PREFIX = "enc:v1:"


class SecretBox:
    """对称加密封装。"""

    def __init__(self, secret: str | bytes) -> None:
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        # 用 sha256 归一到 32 字节，再 base64 成 Fernet 需要的 key
        digest = hashlib.sha256(raw).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            raise ValueError("plaintext 不能为 None")
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return _PREFIX + token

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        if not ciphertext.startswith(_PREFIX):
            # 兼容明文（历史数据或手工写入），直接返回
            return ciphertext
        token = ciphertext[len(_PREFIX) :]
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:  # pragma: no cover - 依赖主密钥错配
            raise ValueError(
                "解密失败：主密钥与数据不匹配（STEP2API_SECRET 是否被改过？）"
            ) from exc


def _load_or_create_secret(settings: Settings) -> str:
    env_secret = settings.secret
    if env_secret:
        return env_secret

    settings.ensure_dirs()
    path: Path = settings.secret_path
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value

    value = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # Windows 上不总是生效，忽略
        pass
    return value


_box: SecretBox | None = None


def get_box(settings: Settings | None = None, reload: bool = False) -> SecretBox:
    """返回进程级单例加密器。"""
    global _box
    if _box is None or reload:
        if settings is None:
            from .config import get_settings

            settings = get_settings()
        _box = SecretBox(_load_or_create_secret(settings))
    return _box


def reset_box() -> None:
    """仅供测试使用。"""
    global _box
    _box = None


# --------------------------------------------------------------------------
# Key 展示辅助
# --------------------------------------------------------------------------


def key_hint(api_key: str) -> str:
    """生成不泄漏内容的 key 指纹，例如 ``sk-…a1b2``。"""
    if not api_key:
        return ""
    if len(api_key) <= 10:
        return api_key[:2] + "…" + api_key[-2:]
    return f"{api_key[:6]}…{api_key[-4:]}"


def key_fingerprint(api_key: str) -> str:
    """稳定指纹，用于去重（不泄漏原文）。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
