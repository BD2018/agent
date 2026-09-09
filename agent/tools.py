"""工具层：定义 Agent 可用的工具（schema）并负责实际执行。

内置工具（search_knowledge_base / get_current_time / read_file）在代码中定义；
自定义工具（http 型 / 本地函数型）由控制台配置，存于 settings.db。
get_all_tools() 合并两者返回 OpenAI tools 列表，每轮对话动态读取；
execute_tool() 按工具名分发：内置分支未命中时走自定义工具执行。

按用户隔离：execute_tool(name, arguments, user_id) 在检索知识库时
使用 user_id 对应的 Chroma collection。
"""
import json
from datetime import datetime

import config

# 内置工具定义（OpenAI function calling 格式）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "在用户的专属知识库中检索资料。对于用户的任何提问，都应优先调用此工具检索知识库中是否有相关内容，再根据检索结果回答。知识库可能包含学习笔记、项目文档、技术资料等各类内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索用的问题或关键词"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前的日期和时间",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取项目目录下某个文本文件的内容（仅限项目目录内）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对项目根目录的文件路径，例如 data/docs/示例.md",
                    }
                },
                "required": ["path"],
            },
        },
    },
]


def get_all_tools():
    """合并内置工具（启用中的）+ 启用的自定义工具。

    每轮对话调用一次，控制台改动立即生效，无需重启。
    """
    from agent.tool_store import get_builtin_flags, list_enabled_custom_tools

    flags = get_builtin_flags()
    tools = [t for t in TOOLS if flags.get(t["function"]["name"], True)]
    for ct in list_enabled_custom_tools():
        tools.append({
            "type": "function",
            "function": {
                "name": ct["name"],
                "description": ct["description"],
                "parameters": ct["parameters"],
            },
        })
    return tools


def _read_file(path):
    """读文件，限制在项目目录内，防止模型越权访问系统文件。"""
    target = (config.BASE_DIR / path).resolve()
    if target != config.BASE_DIR and config.BASE_DIR not in target.parents:
        return "错误：只能读取项目目录内的文件"
    if not target.is_file():
        return f"错误：文件不存在 {path}"
    content = target.read_text(encoding="utf-8", errors="ignore")
    return content[:4000]


def _render_template(tpl, args):
    """把模板中的 {{参数名}} 占位符替换为实际参数值。"""
    out = tpl or ""
    for k, v in args.items():
        out = out.replace("{{" + str(k) + "}}", str(v))
    return out


def _execute_http_tool(tool, args):
    """http 型工具：组装请求并发起调用，响应文本作为工具结果。"""
    import httpx

    cfg = tool["config"]
    method = cfg.get("method", "GET").upper()
    url = _render_template(cfg.get("url", ""), args)
    headers = {
        k: _render_template(v, args) for k, v in (cfg.get("headers") or {}).items()
    }
    timeout = cfg.get("timeout") or 15
    try:
        if method == "GET":
            resp = httpx.get(url, headers=headers, params=args, timeout=timeout)
        else:
            body = _render_template(cfg.get("body_template") or "", args)
            if body.strip():
                try:
                    resp = httpx.request(
                        method, url, headers=headers,
                        json=json.loads(body), timeout=timeout,
                    )
                except json.JSONDecodeError:
                    resp = httpx.request(
                        method, url, headers=headers,
                        content=body.encode("utf-8"), timeout=timeout,
                    )
            else:
                resp = httpx.request(method, url, headers=headers, params=args, timeout=timeout)
        return f"HTTP {resp.status_code}\n{(resp.text or '')[:4000]}"
    except Exception as e:
        return f"错误：工具 HTTP 请求失败：{e}"


def _execute_local_tool(tool, args):
    """local 型工具（本地函数）：可选先查 MySQL 取数，再安全公式求值。"""
    from agent import datasource
    from agent.formula_eval import FormulaError, evaluate

    cfg = tool["config"]
    param_names = list((tool.get("parameters") or {}).get("properties", {}).keys())
    values = {}

    # 1) 配置了数据源时，先参数化查库取数（模型只传查询条件）
    if cfg.get("datasource_id") and cfg.get("sql_template"):
        ds = datasource.get_datasource(cfg["datasource_id"])
        if not ds:
            return "错误：工具配置的数据源不存在"
        try:
            rows = datasource.run_query(ds, cfg["sql_template"], args)
        except Exception as e:
            return f"错误：数据源查询失败：{e}"
        if not rows:
            return "错误：未查询到符合条件的数据，请检查查询条件后重试"
        row = rows[0]
        values.update({
            k: v for k, v in row.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        })

    # 2) 合并模型传入的数值型参数
    for name in param_names:
        if name in args:
            v = args[name]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values[name] = v

    # 3) 公式求值
    try:
        result = evaluate(cfg["formula"], values)
    except FormulaError as e:
        known = ", ".join(f"{k}={v}" for k, v in values.items()) or "无"
        return f"错误：{e}。当前可用变量：{known}"
    return str(result)


def execute_tool(name, arguments_json, user_id):
    """根据模型返回的工具名和参数 JSON，分发到具体实现。

    user_id 用于知识库检索时选择对应的 collection。
    """
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return f"错误：工具参数不是合法 JSON：{arguments_json}"
    if not isinstance(args, dict):
        return "错误：工具参数必须是 JSON 对象"

    if name == "search_knowledge_base":
        if not str(args.get("query") or "").strip():
            return "错误：检索关键词不能为空"
        from knowledge.retriever import get_kb
        return get_kb(user_id).search(args["query"])
    if name == "get_current_time":
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if name == "read_file":
        if not args.get("path"):
            return "错误：缺少文件路径参数"
        return _read_file(args["path"])

    # 自定义工具（仅分发启用中的）
    from agent.tool_store import get_custom_tool_by_name
    tool = get_custom_tool_by_name(name)
    if tool is None:
        return f"未知工具：{name}"
    if tool["type"] == "http":
        return _execute_http_tool(tool, args)
    return _execute_local_tool(tool, args)
