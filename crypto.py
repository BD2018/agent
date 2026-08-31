"""Fernet 对称加密工具：用于数据库中 API Key 的加解密存储。

加密密钥来源：
- 优先从 .env 的 ENCRYPTION_KEY 读取
- 不存在时自动生成 32 字节随机密钥并写回 .env
"""
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

from config import BASE_DIR


def _ensure_key() -> bytes:
    """从 .env 读取 ENCRYPTION_KEY，不存在则生成并写入。"""
    env_path = BASE_DIR / ".env"
    existing = os.getenv("ENCRYPTION_KEY")
    if existing:
        return existing.encode()

    key = Fernet.generate_key()
    line = f"ENCRYPTION_KEY={key.decode()}\n"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if "ENCRYPTION_KEY" not in content:
            env_path.write_text(content.rstrip("\n") + "\n" + line, encoding="utf-8")
    else:
        env_path.write_text(line, encoding="utf-8")
    os.environ["ENCRYPTION_KEY"] = key.decode()
    return key


_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_ensure_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """加密明文，返回 base64 字符串。空串原样返回。"""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """解密 base64 字符串，返回明文。空串或非加密串原样返回（兼容旧明文数据）。"""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        return ciphertext
