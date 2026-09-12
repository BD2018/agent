# Agent 工具调用与 MCP 接入链路文档

> 本文档描述项目中 Agent 使用可调用工具（内置工具、自定义 HTTP/本地函数工具）和 MCP（Model Context Protocol）工具的完整链路，涵盖工具定义、发现、触发、执行、结果回传的全过程。

> **状态说明**：第 2 章描述的内置/自定义工具链路为**已实现**行为；**第 3 章 MCP 接入为设计方案，代码尚未落地**（`agent/mcp_manager.py` 未创建，`get_all_tools()`/`execute_tool()` 暂不含 MCP 分支），相关代码样例仅供实现时参考。

---

## 目录

1. [整体架构总览](#1-整体架构总览)
2. [可调用工具链路](#2-可调用工具链路)
   - 2.1 [工具类型与定义](#21-工具类型与定义)
   - 2.2 [工具发现：get_all_tools()](#22-工具发现get_all_tools)
   - 2.3 [工具触发：Agent 核心循环](#23-工具触发agent-核心循环)
   - 2.4 [工具执行：execute_tool()](#24-工具执行execute_tool)
   - 2.5 [完整时序：从用户提问到返回结果](#25-完整时序从用户提问到返回结果)
3. [MCP 工具接入链路](#3-mcp-工具接入链路)
   - 3.1 [MCP 概述](#31-mcp-概述)
   - 3.2 [MCP Server 配置与存储](#32-mcp-server-配置与存储)
   - 3.3 [MCP 工具发现](#33-mcp-工具发现)
   - 3.4 [MCP 工具执行](#34-mcp-工具执行)
   - 3.5 [同步/异步桥接](#35-同步异步桥接)
   - 3.6 [MCP 完整时序](#36-mcp-完整时序)
4. [工具管理控制台](#4-工具管理控制台)
5. [安全机制](#5-安全机制)
6. [文件索引](#6-文件索引)

---

## 1. 整体架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                      用户对话入口                             │
│            web/app.py → /api/chat 或 /api/chat/stream        │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                    Agent 核心 (agent/core.py)                │
│                                                              │
│  ┌─────────────┐   ┌──────────────────┐   ┌───────────────┐  │
│  │ 组装 messages│ → │ 调用 LLM (流式)   │ → │ 解析 tool_calls│  │
│  └─────────────┘   └──────────────────┘   └───────┬───────┘  │
│                                                    │         │
│              ┌─────────────────────────────────────┘         │
│              ▼                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │          execute_tool(name, args, user_id)              │  │
│  │              (agent/tools.py)                           │  │
│  └──────────┬──────────┬──────────┬────────────────────────┘  │
│             │          │          │                            │
│             ▼          ▼          ▼                            │
│        ┌────────┐ ┌────────┐ ┌──────────┐                      │
│        │内置工具 │ │自定义  │ │ MCP 工具 │                      │
│        │        │ │工具    │ │          │                      │
│        └────────┘ └────────┘ └──────────┘                      │
│             │          │          │                            │
│             └──────────┴──────────┘                            │
│                       │                                       │
│                       ▼                                       │
│              role=tool 消息追加回 messages                     │
│              下一轮 LLM 调用                                  │
└──────────────────────────────────────────────────────────────  │
```

三种工具来源统一汇入 `get_all_tools()`，Agent 每轮对话动态读取，控制台改动即时生效，无需重启服务（MCP 为规划中的第三种来源，尚未接入，见第 3 章设计方案）。

---

## 2. 可调用工具链路

### 2.1 工具类型与定义

项目中有三类工具，全部以 OpenAI function calling 格式定义：

| 类型 | 定义位置 | 存储方式 | 说明 |
|------|---------|---------|------|
| **内置工具** | `agent/tools.py` 的 `TOOLS` 列表 | 代码硬编码 | `search_knowledge_base`、`get_current_time`、`read_file` |
| **自定义 HTTP 工具** | 控制台配置 | `settings.db` 的 `custom_tools` 表 | 调用外部 HTTP 接口，支持模板变量 |
| **自定义本地函数工具** | 控制台配置 | `settings.db` 的 `custom_tools` 表 | 安全公式求值，可选 MySQL 数据源取数 |

#### 内置工具定义格式

```python
# agent/tools.py
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
    # ... get_current_time, read_file
]
```

#### 自定义工具存储结构

```sql
-- settings.db / custom_tools 表
CREATE TABLE custom_tools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,        -- 工具名（字母开头，仅含字母/数字/_-）
    type TEXT NOT NULL,               -- 'http' 或 'local'
    description TEXT NOT NULL,        -- 工具描述（LLM 据此判断何时调用）
    parameters TEXT NOT NULL,         -- JSON Schema 格式的参数定义
    config TEXT NOT NULL,             -- 加密后的配置（URL/headers/formula 等）
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);
```

**HTTP 型 config**（加密存储）：
```json
{
  "method": "POST",
  "url": "https://api.example.com/data",
  "headers": {"Authorization": "Bearer xxx"},
  "body_template": "{\"code\": \"{{code}}\"}",
  "timeout": 15
}
```

**本地函数型 config**：
```json
{
  "formula": "round(revenue - cost, 2)",
  "datasource_id": 1,
  "sql_template": "SELECT revenue, cost FROM sales WHERE region = :region"
}
```

### 2.2 工具发现：get_all_tools()

**文件**：`agent/tools.py`

每轮对话调用一次，合并所有来源的工具，返回 OpenAI tools 列表：

```python
def get_all_tools():
    from agent.tool_store import get_builtin_flags, list_enabled_custom_tools

    # 1. 内置工具（按启用状态过滤）
    flags = get_builtin_flags()
    tools = [t for t in TOOLS if flags.get(t["function"]["name"], True)]

    # 2. 自定义工具（仅启用的）
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
    # 3. MCP 工具接入后在此扩展（设计方案见第 3 章，尚未实现）
```

**关键特性**：
- 每次调用都重新读取数据库，控制台增删改工具**立即生效**
- 内置工具可单独禁用（`builtin_tool_flags` 表）
- 自定义工具通过 `enabled` 字段控制启停

### 2.3 工具触发：Agent 核心循环

**文件**：`agent/core.py`

Agent 采用「思考 → 行动 → 观察」循环（ReAct 模式），由 LLM 自主决定是否调用工具：

```
用户输入
   │
   ▼
组装 messages = [系统提示 + 滑动窗口历史 + 本轮输入]
   │
   ▼
┌─────────── 循环（最多 MAX_TOOL_ROUNDS=8 轮）──────────┐
│                                                       │
│  调用 LLM (client.chat.completions.create)            │
│  传入 tools=get_all_tools()                           │
│       │                                               │
│       ├── 返回 tool_calls？── 否 ──→ 取 content 作为   │
│       │                       最终回答，退出循环       │
│       │                                               │
│       │ 是                                            │
│       ▼                                               │
│  遍历每个 tool_call：                                   │
│    1. 记录事件："调用工具 xxx(args)"                    │
│    2. 调用 execute_tool(name, args, user_id)           │
│    3. 将结果以 role=tool 追加到 messages               │
│                                                       │
│  继续下一轮循环 ──────────────────────────────────────│
└───────────────────────────────────────────────────────┘
```

**系统提示词**（引导 LLM 正确使用工具）：

```python
SYSTEM_PROMPT = """你是用户的专属 AI 助手，请遵守以下规则：
1. 回答涉及用户私有资料（文档、笔记、项目信息）的问题前，必须先调用 search_knowledge_base 检索知识库；
2. 如果知识库检索结果与问题无关，请如实说明「知识库中没有相关内容」，不要编造；
3. 回答使用简体中文，简洁清晰；
4. 需要知道当前时间时，调用 get_current_time；
5. 涉及数值计算时，若有匹配的计算类工具，必须调用工具获得精确结果，禁止自行心算或估算；
6. 工具返回错误时，根据错误信息修正参数后重试，仍失败则如实告知用户原因。"""
```

**触发机制本质**：LLM 根据**工具的 `description` 字段**和**系统提示词**自主判断是否需要调用工具。工具描述写得越精确，触发准确率越高。

**流式与非流式两种模式**：
- `chat()`：非流式，等待完整响应后返回
- `chat_stream()`：流式，逐 token yield `("text", content)` 或 `("tool", event)`

### 2.4 工具执行：execute_tool()

**文件**：`agent/tools.py`

按工具名分发到具体实现：

```python
def execute_tool(name, arguments_json, user_id):
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return f"错误：工具参数不是合法 JSON：{arguments_json}"
    if not isinstance(args, dict):
        return "错误：工具参数必须是 JSON 对象"

    # ── 内置工具 ──
    if name == "search_knowledge_base":
        if not str(args.get("query") or "").strip():
            return "错误：检索关键词不能为空"
        from knowledge.retriever import get_kb
        return get_kb(user_id).search(args["query"])   # 按用户隔离的向量库
    if name == "get_current_time":
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if name == "read_file":
        if not args.get("path"):
            return "错误：缺少文件路径参数"
        return _read_file(args["path"])               # 限制项目目录内

    # ── MCP 工具（设计方案，尚未实现；接入后按 mcp__ 前缀路由，见第 3 章）──

    # ── 自定义工具 ──
    tool = get_custom_tool_by_name(name)
    if tool is None:
        return f"未知工具：{name}"
    if tool["type"] == "http":
        return _execute_http_tool(tool, args)
    return _execute_local_tool(tool, args)
```

#### HTTP 工具执行流程

```
_execute_http_tool(tool, args)
   │
   ├── 从模板渲染 URL：{{code}} → 实际值
   ├── 从模板渲染 headers
   ├── GET → httpx.get(url, params=args)
   │   POST/PUT/DELETE → 渲染 body_template → httpx.request(...)
   │
   └── 返回 "HTTP {status_code}\n{响应文本前4000字符}"
```

#### 本地函数工具执行流程

```
_execute_local_tool(tool, args)
   │
   ├── 1. 配置了数据源？
   │     是 → 参数化 SQL 查询 MySQL（:param → %s 绑定）
   │         取回数值列 → values dict
   │
   ├── 2. 合并模型传入的数值型参数 → values dict
   │
   ├── 3. 安全公式求值
   │     formula_eval.evaluate("round(revenue - cost, 2)", values)
   │     └── AST 白名单解析：仅允许 + - * / // % ** 和
   │         abs/round/min/max/sqrt/floor/ceil
   │
   └── 返回计算结果（字符串）
```

### 2.5 完整时序：从用户提问到返回结果

以「收入1000万成本350.55万，利润多少」为例：

```
用户："收入1000万成本350.55万，利润多少"
  │
  ▼
[web/app.py] POST /api/chat → agent.chat(message)
  │
  ▼
[agent/core.py] 组装 messages
  │  系统提示："涉及数值计算时，若有匹配的计算类工具，必须调用工具..."
  │  工具列表：[search_kb, get_time, read_file, calc_profit(revenue, cost)]
  │
  ▼
[LLM 调用] 模型分析：
  - 用户问利润计算 → 匹配 calc_profit 工具描述
  - 返回 tool_calls: [{name: "calc_profit", arguments: {"revenue":1000, "cost":350.55}}]
  │
  ▼
[execute_tool("calc_profit", '{"revenue":1000,"cost":350.55}', user_id)]
  │
  ├── get_custom_tool_by_name("calc_profit") → tool
  ├── tool["type"] == "local"
  ├── 无数据源 → 跳过 SQL 查询
  ├── 合并参数：values = {revenue: 1000, cost: 350.55}
  ├── evaluate("round(revenue - cost, 2)", values) → 649.45
  │
  └── 返回 "649.45"
  │
  ▼
[messages 追加] role=tool, content="649.45"
  │
  ▼
[LLM 第二轮调用] 模型读到工具结果 649.45
  → 直接回答（无 tool_calls）："本季度利润为 649.45 万元。"
  │
  ▼
[写入历史] user + assistant 消息存入 MemoryStore
  │
  ▼
[返回] {"answer": "本季度利润为 649.45 万元。", "events": ["调用工具 calc_profit(...)"]}
```

**「先检索再计算」的复合链路**（以「华东区利润多少」为例）：

```
用户："根据知识库里的销售报告，华东区利润是多少？"
  │
  ▼
[第1轮 LLM] → tool_calls: search_knowledge_base({"query": "华东区 利润 销售报告"})
  │
  ▼
[execute_tool → get_kb(user_id).search(query)]
  → 返回知识库中的相关切片内容："华东区销售额为 120 万元，成本为 75 万元。"
  │
  ▼
[messages 追加 tool 结果]
  │
  ▼
[第2轮 LLM] 模型读到检索结果，提取数值
  → tool_calls: calc_profit({"revenue": 120, "cost": 75})
  │
  ▼
[execute_tool → 公式求值] → "45.0"
  │
  ▼
[messages 追加 tool 结果]
  │
  ▼
[第3轮 LLM] → 直接回答："华东区本季度利润为 45 万元。"
```

---

## 3. MCP 工具接入链路（设计方案，尚未实现）

### 3.1 MCP 概述

> ⚠️ 本章全部内容（含 `mcp_servers` 表结构、`agent/mcp_manager.py` 代码、控制台管理界面）为**设计方案**，当前代码库中尚未实现，阅读时请注意区分设计与现状。

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 是一个开放协议，用于让 AI 应用连接外部工具和数据源。本项目作为 **MCP Client**，连接外部 MCP Server，将其工具纳入 Agent 的可调用工具池。

**两种传输方式**：

| 传输 | 适用场景 | 配置项 |
|------|---------|--------|
| **stdio** | 本地 MCP Server（子进程） | command + args + env_vars |
| **SSE / Streamable HTTP** | 远程 MCP Server | url |

### 3.2 MCP Server 配置与存储

**存储表**（`settings.db`）：

```sql
CREATE TABLE mcp_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,         -- 服务器名（用作工具前缀）
    transport TEXT NOT NULL,           -- 'stdio' 或 'sse'
    command TEXT,                      -- stdio: 启动命令 (如 npx)
    args TEXT,                         -- stdio: 命令参数 (JSON 数组)
    env_vars TEXT,                     -- stdio: 环境变量 (JSON, 加密存储)
    url TEXT,                          -- sse: 服务器 URL
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);
```

**stdio 配置示例**（文件系统服务器）：
```json
{
  "name": "filesystem",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data/docs"],
  "env_vars": {}
}
```

### 3.3 MCP 工具发现

**文件**：`agent/mcp_manager.py`

MCP 工具使用命名空间前缀 `mcp__{服务器名}__{工具名}`，避免与内置/自定义工具冲突。

```python
def get_mcp_tools() -> list:
    """连接所有启用的 MCP Server，收集其工具，转为 OpenAI 格式。"""
    tools = []
    for server in list_enabled_mcp_servers():
        try:
            client = _get_or_connect(server)      # 惰性连接，缓存复用
            mcp_tools = _run_async(client.list_tools())
            for tool in mcp_tools.tools:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": f"mcp__{server['name']}__{tool.name}",
                        "description": f"[{server['name']}] {tool.description}",
                        "parameters": tool.input_schema,  # JSON Schema 直接复用
                    },
                })
        except Exception as e:
            # 单个 Server 失败不影响其他工具
            continue
    return tools
```

**格式转换**：MCP 的 `input_schema`（JSON Schema）与 OpenAI function calling 的 `parameters` 格式基本一致，直接透传。

### 3.4 MCP 工具执行

```python
def execute_mcp_tool(full_name: str, args: dict) -> str:
    """执行 mcp__server__tool 格式的工具调用。"""
    # 解析命名空间
    parts = full_name.split("__", 2)  # ["mcp", server_name, tool_name]
    if len(parts) != 3:
        return f"错误：无效的 MCP 工具名：{full_name}"
    _, server_name, tool_name = parts

    # 查找 Server 连接
    server = get_mcp_server_by_name(server_name)
    if not server:
        return f"错误：MCP 服务器不存在：{server_name}"
    client = _get_or_connect(server)

    # 调用 MCP Server 的工具
    result = _run_async(client.call_tool(tool_name, args))

    # 提取文本结果
    texts = [block.text for block in result.content
             if hasattr(block, 'text')]
    if result.is_error:
        return f"错误：MCP 工具返回错误：{'. '.join(texts)}"
    return "\n".join(texts)[:4000]
```

### 3.5 同步/异步桥接

MCP SDK v2 是异步的（async/await），而项目的 Agent 循环是同步的。通过后台守护线程的持久 event loop 桥接：

```python
import asyncio, threading

# 后台 event loop（进程生命周期内复用）
_loop = asyncio.new_event_loop()
_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_thread.start()

def _run_async(coro, timeout=30):
    """在同步上下文中调用 async 函数，提交到后台 loop 等待结果。"""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout)
```

**MCP 连接生命周期**：
- 首次调用 `get_mcp_tools()` 或 `execute_mcp_tool()` 时惰性建立连接
- stdio：启动子进程，通过 stdin/stdout 通信
- 连接缓存在内存中（`_connections: dict[str, Client]`），进程存活期间复用
- Server 被禁用或删除时关闭对应连接

### 3.6 MCP 完整时序

以「读取项目目录下的文件列表」为例（配置了 filesystem MCP Server）：

```
用户："项目目录下有哪些文件？"
  │
  ▼
[get_all_tools()] 合并工具列表
  │  内置工具 + 自定义工具 + MCP 工具
  │  MCP 工具来自 filesystem Server：
  │    mcp__filesystem__read_file
  │    mcp__filesystem__list_directory
  │    mcp__filesystem__write_file
  │
  ▼
[LLM 调用] 模型分析：
  - 用户要列文件 → 匹配 mcp__filesystem__list_directory
  - 返回 tool_calls: [{name: "mcp__filesystem__list_directory",
                        arguments: {"path": "/data/docs"}}]
  │
  ▼
[execute_tool("mcp__filesystem__list_directory", '{"path":"/data/docs"}', user_id)]
  │
  ├── name.startswith("mcp__") → 路由到 execute_mcp_tool
  ├── 解析：server="filesystem", tool="list_directory"
  ├── 查找/建立 filesystem Server 的连接（惰性连接，缓存复用）
  ├── _run_async(client.call_tool("list_directory", {"path": "/data/docs"}))
  │     └── MCP SDK 通过 stdio 向子进程发送请求
  │         子进程执行完毕，返回结果
  │
  └── 返回 "report.pdf\nnotes.txt\n..."
  │
  ▼
[messages 追加] role=tool, content="report.pdf\nnotes.txt\n..."
  │
  ▼
[LLM 第二轮调用] → 直接回答："项目目录下有以下文件：report.pdf、notes.txt..."
```

---

## 4. 工具管理控制台

控制台（`web/static/console.html`）提供以下管理功能：

### 可调用工具管理（控制台 → 工具管理）

| 功能 | 说明 |
|------|------|
| 双视图切换 | 块状卡片视图 / 列表视图，右上角一键切换 |
| 内置工具启停 | 对内置工具单独启用/禁用 |
| 自定义工具 CRUD | 新增、编辑、删除 HTTP 型/本地函数型工具 |
| 工具试调 | 直接传入参数测试工具执行，验证配置正确性 |
| 数据源管理 | 管理本地函数工具使用的 MySQL 数据源，支持连接测试 |

### MCP 服务器管理（设计方案，尚未实现；规划入口：控制台 → 工具管理 → MCP 服务器区块）

| 功能 | 说明 |
|------|------|
| 新增服务器 | 选择 stdio/SSE 传输，填写命令/URL |
| 测试连接 | 连接 Server 并列出其暴露的工具列表 |
| 启用/禁用 | 禁用后该 Server 的工具不再出现在工具列表中 |
| 删除 | 删除配置并关闭连接 |

### API Key 管理（控制台 → 右上角用户名 → 个人资料）

| 功能 | 说明 |
|------|------|
| 创建 API Key | 生成 `sk-xxx` 格式密钥，明文仅显示一次 |
| 启用/禁用 | 禁用后外部系统无法调用 |
| 删除 | 立即失效 |

---

## 5. 安全机制

### 公式求值安全

- **AST 白名单解析**：仅允许算术运算符（`+ - * / // % **`）和白名单函数（`abs round min max sqrt floor ceil`）
- **变量名校验**：公式中使用的变量必须在工具参数列表中定义
- 恶意输入如 `__import__("os").system("rm -rf /")` 会被拦截

### SQL 注入防护

- 本地函数工具的 SQL 模板使用命名参数（`:param`），执行时转为参数化查询（`%(param)s` + 值绑定）
- 数据源必须使用只读账户

### HTTP 工具安全

- URL 必须以 `http://` 或 `https://` 开头
- 请求头（可能含密钥）加密存储
- 超时限制 1-120 秒

### MCP 安全

- MCP 工具名加命名空间前缀，避免与内置/自定义工具冲突
- MCP Server 子进程在服务端运行，建议限制可执行命令范围
- env_vars 中可能含密钥，加密存储
- MCP 工具执行结果截断至 4000 字符

### API Key 安全

- Key 明文仅在创建时返回一次
- 数据库存储 SHA-256 哈希值，只保留前缀用于识别
- 上传的文档进入 Key 所属用户的知识库，按用户隔离

---

## 6. 文件索引

| 文件 | 职责 |
|------|------|
| `agent/core.py` | Agent 核心：LLM 调用 + tool_calls 循环 |
| `agent/tools.py` | 工具定义 + get_all_tools() + execute_tool() 路由 |
| `agent/tool_store.py` | 自定义工具 CRUD + 内置工具启停状态 |
| `agent/formula_eval.py` | 安全公式求值（AST 白名单） |
| `agent/datasource.py` | MySQL 数据源管理 + 参数化查询 |
| `agent/mcp_manager.py` | MCP Server 管理 + 工具发现/执行（待实现） |
| `auth/api_keys.py` | API Key 创建/验证/管理 |
| `web/app.py` | Web 接口：工具管理 API + 开放接口 + 对话接口 |
| `web/static/console.html` | 控制台前端：工具管理双视图 + 数据源 + 个人资料 |
| `web/static/api_docs.html` | 接口文档（静态页，新标签页打开） |
| `config.py` | 全局配置：上传限制、查询行数、切分参数等 |
| `auth/models.py` | 用户管理 + 权限控制（ALL_PAGES 定义菜单可见性） |
