# 对话全链路文档：从用户输入文字到返回文字

> 本文档完整追踪一次对话的端到端旅程：用户在输入框敲下文字，到回答逐字出现在屏幕上，中间经过的每一层、每一次读写、每一条分支。
>
> 相关文档：[README.md](README.md)（整体架构与配置）、[TOOL_AND_MCP_GUIDE.md](TOOL_AND_MCP_GUIDE.md)（工具系统与 MCP 设计）。

---

## 1. 全链路总览图

```
┌─────────────────────────── 浏览器（web/static/chat.html）───────────────────────────┐
│                                                                                     │
│  用户输入文字 ──▶ Enter ──▶ sendMessage()                                            │
│      │                        │                                                     │
│      │                 前置检查：已登录(token)？非空？非重入(sending)？                 │
│      │                        │                                                     │
│      │                        ▼                                                     │
│      │            POST /api/chat/stream   {message: "..."}                          │
│      │            Headers: Authorization: Bearer <JWT>                              │
│      │                        │                                                     │
│      │                        ▼                                                     │
│      │            fetch + ReadableStream 逐块读取 SSE 响应                            │
│      │                        │                                                     │
│      │   ┌────────────────────┴──────────────────────┐                              │
│      │   │ 接收线程（网络事件到达）                      │                              │
│      │   │   text 事件 ──▶ pending 缓冲区              │                              │
│      │   │   tool 事件 ──▶ 工具提示区（即时显示）         │                              │
│      │   │   done 事件 ──▶ finishTyping() 收尾          │                              │
│      │   └────────────────┬──────────────────────────┘                              │
│      │                    ▼                                                         │
│      │   ┌──────────────────────────────────────────┐                               │
│      │   │ 渲染线程（requestAnimationFrame，~60fps）  │                               │
│      │   │   每帧从 pending 吐 N 字（N=ceil(长度/8)）   │──▶ 气泡逐字出现 + 闪烁光标      │
│      │   │   缓冲越长吐字越快（自适应追平）              │                               │
│      │   └──────────────────────────────────────────┘                               │
└──────────────────────────────────┬──────────────────────────────────────────────────┘
                                   │ HTTPS(局域网 HTTP)
                                   ▼
┌──────────────────────── FastAPI（web/app.py，单进程）────────────────────────────────┐
│                                                                                     │
│  ① 路由 /api/chat/stream                                                            │
│  ② 认证：get_current_user ──▶ jwt_utils.verify_token(HS256)                          │
│            ──▶ users.db 查用户 ──▶ {id, username, permissions}                       │
│  ③ get_agent(user_id) ──▶ Agent 缓存（每用户一个实例）                                │
│  ④ StreamingResponse(SSE) 包裹 agent.chat_stream(message)                            │
│     生成器把 (type, content) 元组序列化为 "data: {json}\n\n" 推给浏览器                 │
└──────────────────────────────────┬──────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────── Agent 核心（agent/core.py）─────────────────────────────────┐
│                                                                                     │
│  ⑤ 固定问答短路：qa_store.search_qa(user_id, 输入)                                    │
│        命中（语义相似度达标）──▶ 直接 yield 预设回答 + done，结束（不进 LLM）             │
│        未命中 ──▶ 继续                                                                │
│  ⑥ 取模型客户端：llm_settings 读启用模型（settings.db，Key Fernet 解密）                 │
│        base_url / Key 与上次不同 ──▶ 重建 OpenAI 客户端（热切换）                       │
│  ⑦ 组装 messages：                                                                   │
│        [system: 系统提示 + 用户角色人设(users.role_prompt)]                            │
│        + memory.load_recent(20)   ◀── memory.db（按 user_id 隔离的滑动窗口）            │
│        + [user: 本轮输入]                                                            │
│  ⑧ 工具循环（最多 MAX_TOOL_ROUNDS=8 轮）──────────────────────────────┐                │
│        调用 LLM（stream=True，tools=get_all_tools()）                  │               │
│        │                                                             │               │
│        ├── delta.content ──▶ yield("text", 片段) ──▶ SSE ──▶ 浏览器    │               │
│        ├── delta.tool_calls ──▶ 累积完整参数 ──▶ 见下方工具分发          │               │
│        └── 无 tool_calls ──▶ 最终回答：写历史、yield("done")、结束       │               │
│                                                                       │               │
│        工具结果以 role=tool 追加回 messages ────────────────────────────┘（进入下一轮）  │
└──────────────────────────────────┬──────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────── 工具分发（agent/tools.py execute_tool）──────────────────────────┐
│                                                                                     │
│  search_knowledge_base ──▶ 知识库检索（见第 3 节详图）                                 │
│  get_current_time ──────▶ 本地系统时间                                               │
│  read_file ─────────────▶ 读项目目录内文本文件（截断 4000 字符）                        │
│  自定义 HTTP 型 ─────────▶ 模板渲染 URL/headers/body ──▶ httpx 请求外部接口             │
│  自定义本地函数型 ────────▶ MySQL 数据源参数化取数 ──▶ AST 白名单公式求值                 │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 分阶段详解

### 阶段 0：前端准备（chat.html）

| 步骤 | 做什么 | 失败时的表现 |
|------|--------|-------------|
| 登录态检查 | 读 `localStorage.token`，无 token 显示「请先登录」并禁用输入 | 输入框禁用 |
| Key 配置检查 | 页面加载时调 `GET /api/settings/check` | 未配置时禁用输入并引导去控制台 |
| 输入校验 | 文本非空、`sending` 标志防重入 | 直接忽略本次发送 |
| 发起请求 | `POST /api/chat/stream`，携带 JWT，前端设 180 秒超时兜底 | 超时显示错误文案 |

### 阶段 1：认证与路由（web/app.py）

1. FastAPI 依赖 `get_current_user`（auth/dependencies.py）解析 `Authorization: Bearer` 头；
2. `jwt_utils.verify_token` 用 `JWT_SECRET`（HS256）验签与过期检查；
3. 通过后按 `sub`（用户 id）到 users.db 取用户，得到 `{id, username, permissions}`；
4. 校验 `message` 非空（空则 400）；
5. `get_agent(user_id)`：命中 `_agent_cache` 直接复用，否则创建该用户专属 Agent（含各自的 MemoryStore 与知识库句柄）；
6. 返回 `StreamingResponse`，media_type 为 `text/event-stream`，带 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no`（防代理缓冲打断流式）。

### 阶段 2：Agent 组装（agent/core.py `chat_stream`）

按顺序做四件事：

1. **固定问答短路**：`qa_store.search_qa()` 用向量模型在该用户的 `user_{id}_qa` collection 中做语义匹配，命中的是**管理员预置的问答对**——直接把预设回答作为 text 事件推送并结束，**一次 LLM 调用都不发生**（问答对录入与检索向量的写入发生在控制台保存时）；
2. **模型客户端热加载**：`_get_client()` 每轮对话从 settings.db 读取当前启用的模型配置（`enabled=1` 的唯一一条），API Key 经 Fernet 解密；若地址或 Key 与上次不同（管理员在控制台切换了模型），自动重建 OpenAI 客户端——**模型切换无需重启服务**；
3. **系统提示词**：内置 SYSTEM_PROMPT（知识库使用规范、中文回答等）之上，叠加该用户在个人资料里设置的**角色人设**（users 表 `role_prompt` 列）；
4. **历史窗口**：`memory.load_recent(20)` 从 memory.db 取该用户最近 20 条问答（滑动窗口，超出部分自动裁掉），加上本轮输入构成完整 messages。

### 阶段 3：LLM 流式调用与工具决策（核心循环）

每轮循环：

1. 调 `client.chat.completions.create(..., stream=True)`，工具清单来自 `get_all_tools()`——**每轮实时重读**：内置三个工具按 `builtin_tool_flags` 启停过滤，加上 settings.db 中所有 `enabled=1` 的自定义工具。控制台改工具配置，下一轮对话立即生效；
2. **文本增量**：模型吐出的每个 `delta.content` 立即 `yield ("text", 片段)`，经 SSE 推到浏览器——用户在工具还没决定之前就已经开始看到文字；
3. **工具调用**：若 delta 携带 `tool_calls`，流式阶段先把分片到达的函数名与参数**累积完整**（工具参数可能跨多个 chunk），然后逐个执行；
4. 无 tool_calls 即为最终回答：问答对写入 memory.db，yield `("done", "")` 结束；
5. 保险丝：连续 `MAX_TOOL_ROUNDS=8` 轮都未能产出最终回答，输出兜底提示。

### 阶段 4：工具执行分发（agent/tools.py `execute_tool`）

| 工具 | 执行路径 | 数据来源 |
|------|---------|---------|
| `search_knowledge_base` | 检索子链路（见第 3 节） | Chroma + BM25（data/chroma/） |
| `get_current_time` | 本地系统时间 | 无 |
| `read_file` | 路径限制在项目目录内，读文本截断 4000 字符 | 本地文件 |
| HTTP 型自定义工具 | `{{参数}}` 模板渲染 URL/headers/body → httpx 发请求 → `HTTP 状态码 + 响应前 4000 字符` | 外部 HTTP API |
| 本地函数型自定义工具 | 可选先按命名参数化 SQL 查 MySQL（上限 `MAX_QUERY_ROWS=100` 行）→ 合并数值参数 → AST 白名单公式求值 | MySQL 数据源 |

工具结果（含报错信息）一律以 `role=tool` 消息追加回 messages，模型在下一轮据此继续推理或修正参数重试。

### 阶段 5：SSE 传输与前端渲染（打字机）

后端生成器把每个事件序列化为 `data: {"type": "...", "content": "..."}\n\n`；前端 `ReadableStream` 按行解析后**接收与渲染解耦**：

- **接收侧**：text 事件只做一件事——把文本追加进 `pending` 缓冲；
- **渲染侧**：`requestAnimationFrame` 每帧从缓冲吐 `ceil(缓冲长度/8)` 个字。缓冲小（正常流式）→ 稳定打字节奏；缓冲大（模型卡顿后突然来一大段）→ 自动加速追平；
- 气泡末尾带闪烁光标，`done` 事件触发收尾：清空缓冲、与完整文本对齐（防吞字防重复）、移除光标；
- `tool` 事件不进打字机，直接显示在消息下方的「调用工具 …」提示区。

### 阶段 6：收尾与持久化

| 数据 | 写入时机 | 存储 |
|------|---------|------|
| 本轮问答 | 最终回答产出时（QA 短路路径同样写入） | memory.db `messages` 表（按 user_id） |
| 模型配置/工具/数据源/问答对/Key | 管理页操作时（与对话链路无关，但被其读取） | settings.db（敏感字段 Fernet 加密） |
| 文档向量 | 上传入库时（与本链路无关，但被检索读取） | data/chroma/（per-user collection） |

---

## 3. 检索子链路详图（search_knowledge_base 内部）

```
模型发起 search_knowledge_base({"query": "..."})
   │
   ▼
get_kb(user_id) ──▶ 该用户专属 collection：user_{id}_docs
   │
   ▼
┌─ 检索策略自动路由 ─────────────────────────────────────────────┐
│                                                              │
│  知识库为空？ ──▶ 返回「知识库当前为空」提示，模型据此答复用户        │
│                                                              │
│  小库（片段数 ≤ CAG_MAX_CHUNKS=1000                            │
│        且总字数 ≤ CAG_TOKEN_THRESHOLD=12000）？                │
│        ──▶ CAG：跳过检索，全部片段直接拼进上下文                  │
│                                                              │
│  大库且 HYBRID_SEARCH_ENABLED=true（默认）──▶ 混合检索：          │
│     ┌──────────────────┐      ┌──────────────────────┐        │
│     │ BM25 关键词召回    │      │ 向量语义召回           │        │
│     │ jieba 分词         │      │ bge 模型 + 查询指令前缀 │        │
│     │ rank-bm25 打分     │      │ Chroma 余弦相似度      │        │
│     │ 取 top-20         │      │ 取 top-20             │        │
│     └────────┬─────────┘      └──────────┬───────────┘        │
│              └──────────┬───────────────┘                     │
│                         ▼                                     │
│              RRF 倒数排名融合（k=60）                           │
│                         ▼                                     │
│              取 top-3（FINAL_TOP_K），带【来源：文件名】           │
│                         ▼                                     │
│        拼成检索结果文本，作为 role=tool 消息交回模型               │
│                                                              │
│  HYBRID_SEARCH_ENABLED=false ──▶ 退回纯向量 top-3（兜底）        │
└──────────────────────────────────────────────────────────────┘

说明：BM25 索引不重复存内容，直接从 Chroma 读取构建并按用户缓存在内存；
知识库增/删后索引自动失效重建。索引与模型的首次加载由启动预热完成（见第 6 节）。
```

---

## 4. 一次完整对话的时序（带工具调用）

以「根据知识库里的销售报告，华东区利润是多少？」为例（假设已配置本地函数型工具 `calc_profit`）：

```
用户输入          浏览器               后端                         LLM / 外部
   │               │                   │                             │
   │ 敲字 + Enter   │                   │                             │
   │──────────────▶│ POST /chat/stream │                             │
   │               │──────────────────▶│ JWT 验证 ✓                  │
   │               │                   │── QA 短路检查 ──▶ 未命中      │
   │               │                   │── 组装 messages              │
   │               │                   │── 第1轮 LLM(stream) ────────▶│
   │               │                   │◀── tool_calls: search_kb ────│
   │               │                   │── 混合检索（BM25+向量→RRF）     │
   │               │                   │   得到「华东区销售额120万…」     │
   │               │                   │── 第2轮 LLM ────────────────▶│
   │               │                   │◀── tool_calls: calc_profit ──│
   │               │                   │── 参数化查库 + 公式求值 ──▶ 45  │
   │               │                   │── 第3轮 LLM ────────────────▶│
   │               │◀═SSE═text═「华东」══│◀── 无 tool_calls，流式吐字 ────│
   │ 逐字显示 ◀═════│◀═SSE═text═「区利润」│                             │
   │               │◀═SSE═tool═事件─────│ （执行工具时插入的提示）          │
   │               │◀═SSE═done──────────│ 问答写入 memory.db            │
   │ 光标消失，完成   │                   │                             │
```

注意时序上的关键点：**第 1、2 轮期间用户已经在看第 1 轮吐出的文字**（如果模型边推理边输出），工具提示事件穿插出现在消息下方的提示区——流式让等待「长」在了内容里。

---

## 5. SSE 事件协议

| 事件 type | content 内容 | 产生时机 | 前端行为 |
|-----------|-------------|---------|---------|
| `text` | 文本片段（数十字以内） | 模型 delta / QA 短路全文 | 进打字机缓冲逐字渲染 |
| `tool` | `调用工具 xxx(参数)` | 每个工具执行前 | 追加到消息下方提示区（即时显示，不打字） |
| `done` | 空 | 最终回答结束（含 QA 短路） | 收尾：清缓冲、对齐全文、移除光标 |
| `error` | 错误信息 | 后端任何未捕获异常 | 丢弃缓冲，气泡显示「出错了: …」 |

---

## 6. 依赖的后台机制

| 机制 | 说明 | 与本链路的关系 |
|------|------|---------------|
| 启动预热 | 服务启动时后台线程加载 jieba 词典、bge 向量模型、Chroma 客户端 | 首次检索/上传不再承担模型冷启动；`/api/status` 的 `model_loaded` 暴露就绪状态 |
| HF 离线模式 | `HF_HUB_OFFLINE=1`，模型只读本地缓存 | 消除启动联网校验超时导致的加载缓慢/失败 |
| 模型热切换 | 控制台启用另一套模型后清空 Agent 缓存 | 下一轮对话即用新模型，无需重启 |
| BM25 索引缓存 | 按用户缓存于内存，知识库增删后自动失效重建 | 保证检索实时反映最新知识库 |

---

## 7. 异常路径一览

| 场景 | 发生位置 | 用户看到 |
|------|---------|---------|
| Token 过期/无效 | 阶段 1 JWT 验证 | HTTP 401 → 前端跳转登录页 |
| 消息为空 | 阶段 1 校验 | HTTP 400（前端已提前拦截） |
| 未配置模型 | 阶段 2 取客户端 | 对话页加载时已被 `/api/settings/check` 拦截并引导 |
| 工具执行失败 | 阶段 4 | 错误文本作为 tool 结果交回模型，模型可修正重试（最多 8 轮） |
| LLM 调用异常（断网/Key 失效/超时） | 阶段 3 | SSE `error` 事件 → 气泡显示错误，未渲染缓冲丢弃 |
| 请求超时（180 秒） | 前端兜底 | 气泡显示超时提示 |
| 知识库为空 / 检索无结果 | 阶段 4 检索 | 工具返回提示文本，模型按提示词如实告知「没有相关内容」 |

---

## 8. 文件索引

| 文件 | 在本链路中的职责 |
|------|-----------------|
| `web/static/chat.html` | 输入框、请求发起、SSE 解析、打字机渲染（接收/渲染解耦 + 自适应吐字） |
| `web/app.py` | 路由 `/api/chat/stream`、JWT 认证入口、Agent 缓存、SSE 序列化 |
| `auth/dependencies.py` / `auth/jwt_utils.py` | Bearer 解析、JWT 验签、用户加载 |
| `agent/core.py` | QA 短路、模型客户端热加载、系统提示组装、工具循环、流式事件产出 |
| `agent/llm_settings.py` | 启用模型配置读取（Key 解密）、多模型热切换 |
| `agent/memory.py` | 历史窗口读取（20 条）与最终问答落库 |
| `agent/tools.py` | `get_all_tools()` 动态工具清单、`execute_tool()` 分发、内置/HTTP/本地函数工具实现 |
| `agent/tool_store.py` / `agent/datasource.py` / `agent/formula_eval.py` | 自定义工具配置、MySQL 参数化取数、安全公式求值 |
| `knowledge/qa_store.py` | 固定问答对的语义匹配（短路层） |
| `knowledge/retriever.py` / `knowledge/bm25_store.py` | CAG/混合检索路由、RRF 融合、BM25 索引 |
| `config.py` | 全部阈值与开关（窗口大小、轮数、CAG/检索参数、上传限制等） |
