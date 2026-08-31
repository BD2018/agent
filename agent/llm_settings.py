"""LLM 连接设置：支持多模型配置存储 + 启用切换。

设计要点：
- model_configs 表：每行一个模型配置(id, name, base_url, api_key, model, enabled)。
- 同时只有一个 enabled=1 的模型生效，切换时自动禁用其他。
- API Key 加密存储，对外接口仅返回脱敏形式。
- 兼容旧 settings 表：首次启动自动迁移旧数据为一条 model_configs 记录。
- get_llm_settings() 返回当前启用模型的配置，Agent 每轮对话前重新读取。
"""
import sqlite3
import threading

from openai import OpenAI

import config
from crypto import decrypt, encrypt

_lock = threading.Lock()


def _get_conn():
    conn = sqlite3.connect(config.SETTINGS_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _lock, _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_enabled ON model_configs(enabled)"
        )
        conn.commit()
    _migrate_legacy()


def _migrate_legacy():
    """兼容旧 settings 表：如果有旧数据且 model_configs 为空，迁移为一条记录。"""
    with _lock, _get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('llm_base_url','llm_api_key','llm_model')"
            ).fetchall()
        except sqlite3.OperationalError:
            return
        if not rows:
            return
        count = conn.execute("SELECT COUNT(*) FROM model_configs").fetchone()[0]
        if count > 0:
            return
        stored = {r["key"]: r["value"] for r in rows}
        base_url = stored.get("llm_base_url") or config.LLM_BASE_URL
        raw_key = stored.get("llm_api_key", "")
        api_key = decrypt(raw_key) if raw_key else ""
        model = stored.get("llm_model") or config.LLM_MODEL
        conn.execute(
            "INSERT INTO model_configs (name, base_url, api_key, model, enabled) VALUES (?, ?, ?, ?, 1)",
            ("默认模型", base_url, encrypt(api_key) if api_key else "", model),
        )
        conn.commit()


def get_llm_settings() -> dict:
    """返回当前启用模型的配置。"""
    _init_db()
    with _lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM model_configs WHERE enabled = 1 LIMIT 1"
        ).fetchone()
    if row:
        api_key = decrypt(row["api_key"]) if row["api_key"] else ""
        return {
            "base_url": row["base_url"],
            "api_key": api_key,
            "model": row["model"],
            "source": "db",
            "has_api_key": bool(api_key),
            "config_id": row["id"],
            "config_name": row["name"],
        }
    return {
        "base_url": config.LLM_BASE_URL,
        "api_key": "",
        "model": config.LLM_MODEL,
        "source": "env",
        "has_api_key": False,
        "config_id": None,
        "config_name": None,
    }


def list_model_configs() -> list[dict]:
    """返回所有模型配置列表。API Key 脱敏。"""
    _init_db()
    with _lock, _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM model_configs ORDER BY id"
        ).fetchall()
    result = []
    for r in rows:
        api_key = decrypt(r["api_key"]) if r["api_key"] else ""
        result.append({
            "id": r["id"],
            "name": r["name"],
            "base_url": r["base_url"],
            "model": r["model"],
            "api_key_masked": mask_key(api_key),
            "has_api_key": bool(api_key),
            "enabled": bool(r["enabled"]),
        })
    return result


def get_model_config(config_id: int) -> dict | None:
    """返回单个模型配置详情。API Key 脱敏。"""
    _init_db()
    with _lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM model_configs WHERE id = ?", (config_id,)
        ).fetchone()
    if not row:
        return None
    api_key = decrypt(row["api_key"]) if row["api_key"] else ""
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "model": row["model"],
        "api_key_masked": mask_key(api_key),
        "has_api_key": bool(api_key),
        "enabled": bool(row["enabled"]),
    }


def create_model_config(name: str, base_url: str, api_key: str, model: str) -> dict:
    """新增模型配置。返回新记录。"""
    name = (name or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    model = (model or "").strip()
    api_key = (api_key or "").strip()
    if not name:
        raise ValueError("模型名称不能为空")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("API 地址必须以 http:// 或 https:// 开头")
    if not model:
        raise ValueError("模型标识不能为空")

    _init_db()
    with _lock, _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO model_configs (name, base_url, api_key, model, enabled) VALUES (?, ?, ?, ?, 0)",
            (name, base_url, encrypt(api_key) if api_key else "", model),
        )
        conn.commit()
        new_id = cur.lastrowid
    return get_model_config(new_id)


def update_model_config(config_id: int, name: str, base_url: str, api_key: str, model: str) -> dict | None:
    """更新模型配置。api_key 留空表示保持现有 Key 不变。"""
    name = (name or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    model = (model or "").strip()
    api_key = (api_key or "").strip()
    if not name:
        raise ValueError("模型名称不能为空")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("API 地址必须以 http:// 或 https:// 开头")
    if not model:
        raise ValueError("模型标识不能为空")

    _init_db()
    with _lock, _get_conn() as conn:
        row = conn.execute("SELECT * FROM model_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            return None
        if api_key:
            conn.execute(
                "UPDATE model_configs SET name=?, base_url=?, api_key=?, model=? WHERE id=?",
                (name, base_url, encrypt(api_key), model, config_id),
            )
        else:
            conn.execute(
                "UPDATE model_configs SET name=?, base_url=?, model=? WHERE id=?",
                (name, base_url, model, config_id),
            )
        conn.commit()
    return get_model_config(config_id)


def delete_model_config(config_id: int) -> bool:
    """删除模型配置。"""
    _init_db()
    with _lock, _get_conn() as conn:
        row = conn.execute("SELECT enabled FROM model_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            return False
        if row["enabled"]:
            raise ValueError("不能删除当前启用的模型，请先切换到其他模型")
        conn.execute("DELETE FROM model_configs WHERE id = ?", (config_id,))
        conn.commit()
    return True


def enable_model_config(config_id: int) -> dict | None:
    """启用指定模型配置，自动禁用其他所有配置。"""
    _init_db()
    with _lock, _get_conn() as conn:
        row = conn.execute("SELECT * FROM model_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE model_configs SET enabled = 0")
        conn.execute("UPDATE model_configs SET enabled = 1 WHERE id = ?", (config_id,))
        conn.commit()
    return get_model_config(config_id)


def mask_key(api_key: str) -> str:
    """API Key 脱敏：保留前 3 后 4 位。"""
    if not api_key:
        return ""
    if len(api_key) <= 7:
        return "***"
    return f"{api_key[:3]}***{api_key[-4:]}"


def test_llm_connection(base_url: str, api_key: str, model: str):
    """用最小请求验证配置可用性，返回 (是否成功, 提示信息)。"""
    base_url = (base_url or "").strip().rstrip("/")
    model = (model or "").strip()
    if not base_url.startswith(("http://", "https://")):
        return False, "API 地址必须以 http:// 或 https:// 开头"
    if not api_key:
        return False, "API Key 不能为空"
    if not model:
        return False, "模型名称不能为空"

    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=15, max_retries=0)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        if not hasattr(resp, "choices") or not resp.choices:
            return False, "API 返回了无效响应（非标准 OpenAI 格式），请检查 API 地址是否需要加 /v1 后缀"
        return True, "连接成功，配置可用"
    except Exception as e:
        msg = str(e)
        if "401" in msg or "Unauthorized" in msg:
            return False, "API Key 无效或未授权"
        if "404" in msg or "Not Found" in msg:
            return False, f"接口不存在，请检查 API 地址是否需要加 /v1 后缀（当前: {base_url}）"
        return False, f"连接失败：{msg}"


# ---------- 兼容旧接口 ----------
def save_llm_settings(base_url: str, api_key, model: str) -> None:
    """兼容旧接口：更新当前启用模型的配置。"""
    cfg = get_llm_settings()
    if cfg["config_id"]:
        update_model_config(cfg["config_id"], cfg["config_name"] or "默认模型", base_url, api_key, model)
    else:
        create_model_config("默认模型", base_url, api_key or "", model)
        enable_model_config(1)
