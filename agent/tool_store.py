"""自定义工具存储与 CRUD，支持两种结构：

- http 型：config 存 method/url/headers/body_template/timeout，
  执行时由后端发起 HTTP 请求，响应文本作为工具结果。
- local 型（本地函数）：config 存 formula/datasource_id/sql_template，
  执行时可选先参数化查 MySQL 取数，再安全公式求值。

敏感字段（headers）加密存储。另管理内置工具的启用状态。
"""
import json
import re
import sqlite3
import threading
from datetime import datetime

import config
from crypto import decrypt, encrypt

_lock = threading.Lock()

NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
ALLOWED_PARAM_TYPES = ("string", "number", "integer", "boolean")
HTTP_METHODS = ("GET", "POST", "PUT", "DELETE")

# 内置工具名（与 agent/tools.py 的 TOOLS 保持一致），用于重名校验
BUILTIN_TOOL_NAMES = ("search_knowledge_base", "get_current_time", "read_file")


def _get_conn():
    conn = sqlite3.connect(config.SETTINGS_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _lock, _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('http', 'local')),
                description TEXT NOT NULL,
                parameters TEXT NOT NULL,
                config TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS builtin_tool_flags (
                name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.commit()


_init_db()


# ── 参数定义 → OpenAI function parameters schema ──

def build_parameters_schema(params: list) -> dict:
    """把 [{name, type, description, required}] 转为 JSON Schema。"""
    properties = {}
    required = []
    for p in params:
        name = (p.get("name") or "").strip()
        if not NAME_RE.match(name):
            raise ValueError(f"参数名不合法: {name}（字母开头，仅含字母/数字/_-）")
        if name in properties:
            raise ValueError(f"参数名重复: {name}")
        ptype = p.get("type") or "string"
        if ptype not in ALLOWED_PARAM_TYPES:
            raise ValueError(f"参数类型不合法: {ptype}")
        properties[name] = {
            "type": ptype,
            "description": (p.get("description") or "").strip(),
        }
        if p.get("required"):
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


def schema_to_params(schema: dict) -> list:
    """JSON Schema 还原为表单参数列表。"""
    props = (schema or {}).get("properties", {})
    required = set((schema or {}).get("required", []))
    return [
        {
            "name": name,
            "type": spec.get("type", "string"),
            "description": spec.get("description", ""),
            "required": name in required,
        }
        for name, spec in props.items()
    ]


# ── config 校验与加解密 ──

def _validate_and_pack_config(tool_type: str, cfg: dict, param_names: list) -> str:
    """校验 config 并返回加密后的 JSON 字符串。"""
    cfg = cfg or {}
    if tool_type == "http":
        method = (cfg.get("method") or "GET").upper()
        if method not in HTTP_METHODS:
            raise ValueError(f"不支持的请求方法: {method}")
        url = (cfg.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("URL 必须以 http:// 或 https:// 开头")
        timeout = cfg.get("timeout") or 15
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            raise ValueError("超时时间必须是整数（秒）")
        if not 1 <= timeout <= 120:
            raise ValueError("超时时间需在 1-120 秒之间")
        headers = cfg.get("headers") or {}
        if isinstance(headers, str):
            try:
                headers = json.loads(headers) if headers.strip() else {}
            except json.JSONDecodeError:
                raise ValueError("请求头必须是合法的 JSON 对象")
        if not isinstance(headers, dict):
            raise ValueError("请求头必须是 JSON 对象")
        packed = {
            "method": method,
            "url": url,
            "headers_enc": encrypt(json.dumps(headers, ensure_ascii=False)),
            "body_template": cfg.get("body_template") or "",
            "timeout": timeout,
        }
    elif tool_type == "local":
        from agent import datasource
        from agent.formula_eval import validate_formula

        formula = (cfg.get("formula") or "").strip()
        validate_formula(formula, param_names)

        sql_template = (cfg.get("sql_template") or "").strip()
        datasource_id = cfg.get("datasource_id")
        if sql_template:
            if not datasource_id:
                raise ValueError("配置了 SQL 模板就必须选择数据源")
            if not datasource.get_datasource(int(datasource_id)):
                raise ValueError("所选数据源不存在")
            sql_params = datasource.extract_sql_params(sql_template)
            unknown = [p for p in sql_params if p not in param_names]
            if unknown:
                raise ValueError(
                    f"SQL 模板参数 {', '.join(unknown)} 未在工具参数中定义"
                )
        elif datasource_id:
            raise ValueError("未配置 SQL 模板时不应选择数据源")

        packed = {
            "formula": formula,
            "datasource_id": int(datasource_id) if datasource_id else None,
            "sql_template": sql_template,
        }
    else:
        raise ValueError(f"未知工具类型: {tool_type}（仅支持 http / local）")
    return json.dumps(packed, ensure_ascii=False)


def _unpack_config(tool_type: str, raw: str) -> dict:
    cfg = json.loads(raw)
    if tool_type == "http":
        headers = {}
        if cfg.get("headers_enc"):
            try:
                headers = json.loads(decrypt(cfg["headers_enc"]))
            except Exception:
                headers = {}
        return {
            "method": cfg.get("method", "GET"),
            "url": cfg.get("url", ""),
            "headers": headers,
            "body_template": cfg.get("body_template", ""),
            "timeout": cfg.get("timeout", 15),
        }
    return {
        "formula": cfg.get("formula", ""),
        "datasource_id": cfg.get("datasource_id"),
        "sql_template": cfg.get("sql_template", ""),
    }


# ── CRUD ──

def _row_to_dict(row) -> dict:
    d = dict(row)
    d["parameters"] = json.loads(d["parameters"])
    d["config"] = _unpack_config(d["type"], d["config"])
    d["enabled"] = bool(d["enabled"])
    return d


def list_custom_tools() -> list:
    with _lock, _get_conn() as conn:
        rows = conn.execute("SELECT * FROM custom_tools ORDER BY id").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_custom_tool(tool_id: int):
    with _lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM custom_tools WHERE id = ?", (tool_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_custom_tool_by_name(name: str, only_enabled: bool = True):
    sql = "SELECT * FROM custom_tools WHERE name = ?"
    if only_enabled:
        sql += " AND enabled = 1"
    with _lock, _get_conn() as conn:
        row = conn.execute(sql, (name,)).fetchone()
    return _row_to_dict(row) if row else None


def _validate_common(name: str, tool_type: str, description: str, params: list):
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("工具名须字母开头，仅含字母/数字/_-，最长 64 位")
    if name in BUILTIN_TOOL_NAMES:
        raise ValueError(f"工具名与内置工具冲突: {name}")
    if tool_type not in ("http", "local"):
        raise ValueError("工具类型仅支持 http / local")
    if not (description or "").strip():
        raise ValueError("工具描述不能为空（模型靠描述判断何时调用）")
    if not params:
        raise ValueError("至少定义一个参数")
    return name


def create_tool(name: str, tool_type: str, description: str,
                params: list, cfg: dict) -> dict:
    name = _validate_common(name, tool_type, description, params)
    schema = build_parameters_schema(params)
    param_names = [p["name"].strip() for p in params]
    config_raw = _validate_and_pack_config(tool_type, cfg, param_names)
    try:
        with _lock, _get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO custom_tools (name, type, description, parameters, config) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, tool_type, description.strip(),
                 json.dumps(schema, ensure_ascii=False), config_raw),
            )
            conn.commit()
            new_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"工具名已存在: {name}")
    return get_custom_tool(new_id)


def update_tool(tool_id: int, name: str, tool_type: str, description: str,
                params: list, cfg: dict):
    name = _validate_common(name, tool_type, description, params)
    schema = build_parameters_schema(params)
    param_names = [p["name"].strip() for p in params]
    config_raw = _validate_and_pack_config(tool_type, cfg, param_names)
    try:
        with _lock, _get_conn() as conn:
            cur = conn.execute(
                "UPDATE custom_tools SET name=?, type=?, description=?, "
                "parameters=?, config=?, updated_at=? WHERE id=?",
                (name, tool_type, description.strip(),
                 json.dumps(schema, ensure_ascii=False), config_raw,
                 datetime.now().isoformat(timespec="seconds"), tool_id),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"工具名已存在: {name}")
    return get_custom_tool(tool_id)


def delete_tool(tool_id: int) -> bool:
    with _lock, _get_conn() as conn:
        cur = conn.execute("DELETE FROM custom_tools WHERE id = ?", (tool_id,))
        conn.commit()
    return cur.rowcount > 0


def set_tool_enabled(tool_id: int, enabled: bool):
    with _lock, _get_conn() as conn:
        conn.execute(
            "UPDATE custom_tools SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, datetime.now().isoformat(timespec="seconds"), tool_id),
        )
        conn.commit()
    return get_custom_tool(tool_id)


def list_enabled_custom_tools() -> list:
    with _lock, _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM custom_tools WHERE enabled = 1 ORDER BY id"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── 内置工具启用状态 ──

def get_builtin_flags() -> dict:
    """返回 {内置工具名: 是否启用}，默认全部启用。"""
    with _lock, _get_conn() as conn:
        rows = conn.execute("SELECT name, enabled FROM builtin_tool_flags").fetchall()
    flags = {name: True for name in BUILTIN_TOOL_NAMES}
    for r in rows:
        if r["name"] in flags:
            flags[r["name"]] = bool(r["enabled"])
    return flags


def set_builtin_enabled(name: str, enabled: bool) -> bool:
    if name not in BUILTIN_TOOL_NAMES:
        return False
    with _lock, _get_conn() as conn:
        conn.execute(
            "INSERT INTO builtin_tool_flags (name, enabled) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled",
            (name, 1 if enabled else 0),
        )
        conn.commit()
    return True


def is_datasource_in_use(ds_id: int) -> bool:
    """数据源是否被自定义工具引用。"""
    with _lock, _get_conn() as conn:
        rows = conn.execute(
            "SELECT config FROM custom_tools WHERE type = 'local'"
        ).fetchall()
    for r in rows:
        try:
            if json.loads(r["config"]).get("datasource_id") == ds_id:
                return True
        except Exception:
            continue
    return False
