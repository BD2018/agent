"""MySQL 数据源管理：连接配置（密码加密存储）+ 参数化查询执行。

供带取数需求的「本地函数型」工具使用：SQL 模板由管理员预先配置，
运行时只代入模型给出的条件参数（命名参数绑定），不拼接字符串，
从根源防止 SQL 注入与越权查询。
"""
import re
import sqlite3
import threading

import config
from crypto import decrypt, encrypt

_lock = threading.Lock()

# SQL 模板中的命名参数：:name 形式
_PARAM_RE = re.compile(r":([A-Za-z_]\w*)")


def _get_conn():
    conn = sqlite3.connect(config.SETTINGS_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _lock, _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS datasources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 3306,
                db_name TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


_init_db()


def _row_to_dict(row, include_password: bool = False) -> dict:
    d = dict(row)
    if include_password:
        d["password"] = decrypt(d["password"])
    else:
        d["password"] = "******" if d["password"] else ""
    return d


def list_datasources() -> list:
    with _lock, _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM datasources ORDER BY id"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_datasource(ds_id: int, include_password: bool = True):
    with _lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM datasources WHERE id = ?", (ds_id,)
        ).fetchone()
    return _row_to_dict(row, include_password) if row else None


def create_datasource(name: str, host: str, port: int, db_name: str,
                      username: str, password: str) -> dict:
    with _lock, _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO datasources (name, host, port, db_name, username, password) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, host, port, db_name, username, encrypt(password)),
        )
        conn.commit()
        new_id = cur.lastrowid
    return get_datasource(new_id, include_password=False)


def update_datasource(ds_id: int, name: str, host: str, port: int,
                      db_name: str, username: str, password: str):
    """password 传空串表示保持原密码不变。"""
    with _lock, _get_conn() as conn:
        if password:
            conn.execute(
                "UPDATE datasources SET name=?, host=?, port=?, db_name=?, "
                "username=?, password=? WHERE id=?",
                (name, host, port, db_name, username, encrypt(password), ds_id),
            )
        else:
            conn.execute(
                "UPDATE datasources SET name=?, host=?, port=?, db_name=?, "
                "username=? WHERE id=?",
                (name, host, port, db_name, username, ds_id),
            )
        conn.commit()
    return get_datasource(ds_id, include_password=False)


def delete_datasource(ds_id: int) -> bool:
    with _lock, _get_conn() as conn:
        cur = conn.execute("DELETE FROM datasources WHERE id = ?", (ds_id,))
        conn.commit()
    return cur.rowcount > 0


def extract_sql_params(sql_template: str) -> list:
    """提取 SQL 模板中的 :name 参数名（去重保序）。"""
    seen, names = set(), []
    for m in _PARAM_RE.finditer(sql_template or ""):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            names.append(m.group(1))
    return names


def _connect(ds: dict):
    import pymysql
    return pymysql.connect(
        host=ds["host"],
        port=int(ds["port"]),
        user=ds["username"],
        password=ds["password"],
        database=ds["db_name"],
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )


def test_connection(ds: dict):
    """测试连接，返回 (ok, message)。ds 需含明文 password。"""
    try:
        conn = _connect(ds)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return True, f"连接成功：{ds['host']}:{ds['port']}/{ds['db_name']}"
    except Exception as e:
        return False, f"连接失败：{e}"


def run_query(ds: dict, sql_template: str, params: dict) -> list:
    """参数化执行 SQL 模板，返回字典行列表（最多 MAX_QUERY_ROWS 行）。

    模板中的 :name 占位符转换为 pymysql 的 %(name)s 命名参数，
    由驱动负责转义，避免注入。
    """
    names = extract_sql_params(sql_template)
    missing = [n for n in names if n not in params or params[n] in (None, "")]
    if missing:
        raise ValueError(f"缺少查询参数: {', '.join(missing)}")

    sql = _PARAM_RE.sub(r"%(\1)s", sql_template)
    bind = {n: params[n] for n in names}

    conn = _connect(ds)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, bind)
            rows = cur.fetchmany(config.MAX_QUERY_ROWS)
        return [dict(r) for r in rows]
    finally:
        conn.close()
