"""工具层：定义 Agent 可用的工具（schema）并负责实际执行。

新增一个工具只需两步：
1. 在 TOOLS 里加一段 OpenAI function calling 格式的描述；
2. 在 execute_tool() 里加一个分支实现真正的逻辑。

按用户隔离：execute_tool(name, arguments, user_id) 在检索知识库时
使用 user_id 对应的 Chroma collection。
"""
import json
from datetime import datetime

import config

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "在我的专属知识库中检索资料。当问题涉及我的私有文档、笔记、项目资料时优先调用。",
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


def _read_file(path):
    """读文件，限制在项目目录内，防止模型越权访问系统文件。"""
    target = (config.BASE_DIR / path).resolve()
    if target != config.BASE_DIR and config.BASE_DIR not in target.parents:
        return "错误：只能读取项目目录内的文件"
    if not target.is_file():
        return f"错误：文件不存在 {path}"
    content = target.read_text(encoding="utf-8", errors="ignore")
    return content[:4000]


def execute_tool(name, arguments_json, user_id):
    """根据模型返回的工具名和参数 JSON，分发到具体实现。

    user_id 用于知识库检索时选择对应的 collection。
    """
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return f"错误：工具参数不是合法 JSON：{arguments_json}"

    if name == "search_knowledge_base":
        from knowledge.retriever import get_kb
        return get_kb(user_id).search(args["query"])
    if name == "get_current_time":
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if name == "read_file":
        return _read_file(args["path"])
    return f"未知工具：{name}"
