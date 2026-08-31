# 我的专属 Agent（多用户 Web 版 + RAG 知识库）

一个从零搭建的个人专属 Agent 项目：以任意 OpenAI 格式兼容模型（默认 DeepSeek）为大脑，支持工具调用（function calling）、流式对话、多用户隔离的 RAG 私有知识库，以及一套完整的 Web 管理系统（登录认证、知识库管理、模型管理、用户管理）。

项目同时保留命令行入口，代码精简、注释完整，适合边跑边学。

---

## 一、它能做什么

- **对话**：Web 端打字机流式回答；自动判断何时调用工具（查知识库、查时间、读文件）
- **知识库**：Web 端上传文档，自动切片、向量化、增量更新，上传后立即可被检索（无需重启）
- **多用户**：注册/登录（JWT），每个用户拥有独立的知识库、对话历史、文件目录
- **管理系统**：概览仪表盘、知识库管理、文档列表、向量片段查看、对话历史、系统信息、模型设置、用户管理
- **多模型**：可保存多套模型配置（名称 + Base URL + Key + 模型标识），一键切换启用，Key 加密存储
- **权限**：admin 拥有全部权限，可按页面给普通用户授权
- **CLI 模式**：`main.py` 命令行交互保留，与 Web 共用同一套核心代码

支持文档格式：`.md` / `.txt` / `.pdf` / `.doc` / `.docx` / `.xlsx` / `.xls`

---

## 二、整体架构

```
浏览器
 ├── /login     登录/注册页
 ├── /          对话页（默认首页，SSE 流式打字机）
 └── /console   管理系统（侧边栏多模块）
        │  Authorization: Bearer <JWT>
        ▼
┌──────────────────────── FastAPI (web/app.py, 单进程) ────────────────────────┐
│  认证依赖 get_current_user / require_permission                              │
│                                                                              │
│  /api/chat/stream ──▶ Agent.chat_stream() ──▶ LLM(流式) ──▶ 工具循环          │
│  /api/upload      ──▶ ingest_file() ──▶ 切片+向量化 ──▶ get_kb(user_id)       │
│                                                                              │
│  ┌────────────────────── 按 user_id 隔离 ──────────────────────┐             │
│  │  Chroma collection: user_{id}_docs   （向量知识库）          │             │
│  │  SQLite messages:   WHERE user_id=?  （对话记忆）            │             │
│  │  文件目录:          data/docs/{id}/   （原始文档）            │             │
│  └─────────────────────────────────────────────────────────────┘             │
└──────────────────────────────────────────────────────────────────────────────┘

知识库链路：
  上传文档 ─▶ 提取纯文本 ─▶ 切分(400字/片, 重叠50) ─▶ bge 向量化 ─▶ Chroma
  提问 ─▶ Agent 决策调用 search_knowledge_base ─▶ 余弦相似度 top3 ─▶ 拼入上下文
```

**实时性原理**：FastAPI 单进程内共享同一个 Chroma 客户端与 Agent 缓存，上传写入后同一进程的检索立即可见，无需重启。

---

## 三、目录结构

```
my_agent/
├── main.py                # 命令行入口（/ingest /reset /quit）
├── web_main.py            # Web 服务入口（uvicorn，端口默认 1129）
├── config.py              # 全局配置（路径、端口、JWT、切分参数等）
├── crypto.py              # Fernet 对称加密（API Key 落盘加密）
├── requirements.txt
├── .env / .env.example    # 环境变量（JWT_SECRET/ENCRYPTION_KEY 首次自动生成）
├── agent/
│   ├── core.py            # Agent 核心：思考-行动-观察循环 + chat_stream 流式
│   ├── memory.py          # SQLite 对话记忆（按 user_id 隔离，自动迁移旧表）
│   ├── tools.py           # 工具定义（schema）与执行分发（带 user_id）
│   └── llm_settings.py    # 多模型配置存储（model_configs 表，Key 加密，启用切换）
├── auth/
│   ├── jwt_utils.py       # JWT 签发/验证
│   ├── models.py          # 用户存储 + 密码哈希 + admin 预置 + 页面权限定义
│   └── dependencies.py    # FastAPI 依赖：get_current_user / require_permission
├── knowledge/
│   ├── extractors.py      # 多格式文本提取（PDF/Word/Excel/MD/TXT）
│   ├── ingest.py          # 单文件入库 ingest_file(path, user_id) + 删除
│   └── retriever.py       # get_kb(user_id) → per-user Chroma collection + 检索
├── web/
│   ├── app.py             # FastAPI 应用与全部 API 路由
│   └── static/
│       ├── login.html     # 登录/注册页
│       ├── chat.html      # 对话页（流式打字机、工具过程展示）
│       └── console.html   # 管理系统（多模块 SPA）
└── data/                  # 全部运行时数据（自动生成）
    ├── docs/{user_id}/    # 各用户上传的原始文档
    ├── chroma/            # 向量数据库（per-user collection）
    ├── memory.db          # 对话历史（含 user_id 列）
    ├── users.db           # 用户账号
    └── settings.db        # 模型配置（API Key 加密存储）
```

---

## 四、搭建与运行

### 第 1 步：准备环境

Python 3.10+，建议虚拟环境：

```bash
cd E:\my_project\腾讯buddy\my_agent
python -m venv .venv
.venv\Scripts\activate        # PowerShell
pip install -r requirements.txt
```

说明：`sentence-transformers` 会附带安装 PyTorch，体积较大，请耐心等待。

### 第 2 步：启动 Web 服务

```bash
python web_main.py
```

浏览器访问 `http://localhost:1129`（端口可在 `.env` 中用 `WEB_PORT` 修改）。

- 首次启动自动完成：生成 `JWT_SECRET`、`ENCRYPTION_KEY` 写入 `.env`；创建管理员账号
- **默认管理员：`admin` / `123456`**（登录后请在用户管理中为其他用户授权或修改密码）

### 第 3 步：配置模型（必须）

API Key 不写在任何配置文件里，唯一入口是 Web 页面：

1. 登录后进入 **控制台 → 模型设置**（旧配置会自动迁移为一条"默认模型"记录）
2. 点 **新增模型**：填写名称、API 地址（多数服务需 `/v1` 后缀）、API Key、模型标识；可用预设一键填充（OpenAI / DeepSeek / 通义 / 智谱 / Kimi / SiliconFlow / Ollama）
3. **测试连接** 通过后 **保存**，再点 **启用**，立即生效（无需重启）

未配置 Key 时，对话页会显示引导提示并禁用输入。

### 第 4 步：上传文档并对话

- **控制台 → 知识库管理**：拖拽上传文档，自动切片入库，列表实时刷新
- **对话页**：直接提问，Agent 自行决定是否检索知识库；回答逐字流式输出

### CLI 模式（可选）

```bash
python main.py
# /ingest 批量入库 data/docs/ 下文档；/reset 清空历史；/quit 退出
```

---

## 五、Web 端功能说明

### 页面

| 路径 | 页面 | 说明 |
|------|------|------|
| `/login` | 登录/注册 | 公开 |
| `/` | 对话页 | 默认可浏览，发消息需登录；流式打字机 + 工具调用过程展示 |
| `/console` | 管理系统 | 需登录，侧边栏按权限显示模块 |

### 管理系统模块

| 模块 | 权限 key | 内容 |
|------|----------|------|
| 概览 | dashboard | 文档数 / 向量片段数 / 对话轮数 / 工具数统计 |
| 知识库管理 | knowledge | 拖拽上传、入库进度 |
| 文档列表 | doclist | 每文档片段数、下载、删除 |
| 向量片段 | chunks | 卡片/列表双视图、按文件筛选、查看完整切片内容 |
| 对话历史 | history | 全部问答记录、一键清空 |
| 系统信息 | system | 运行配置与可用工具清单 |
| 模型设置 | llm | 多模型增删改、测试连接、启用切换 |
| 用户管理 | users | 新增用户、重置密码、页面授权、删除（仅 admin 可见可操作） |

### 权限模型

- `auth/models.py` 中 `ALL_PAGES` 定义 8 个页面权限 key
- admin（id=1）恒拥有全部权限
- 普通用户由 admin 通过 `PUT /api/users/{id}/permissions` 授予页面权限；无权限的模块在侧边栏隐藏、后端接口返回 403

---

## 六、API 一览

### 页面与认证

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/login` `/` `/console` | 三个页面 | 公开 |
| POST | `/api/auth/register` | 注册，返回 JWT | 公开 |
| POST | `/api/auth/login` | 登录，返回 JWT | 公开 |
| GET | `/api/auth/me` | 当前用户信息 | 登录 |

### 用户管理

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET/POST | `/api/users` | 列表 / 新增 | users |
| PUT | `/api/users/{id}/permissions` | 设置页面权限 | users |
| PUT | `/api/users/{id}/password` | 重置密码 | users |
| DELETE | `/api/users/{id}` | 删除用户 | users |

### 模型设置

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/api/settings/check` | 是否已配置 Key（轻量） | 登录 |
| GET/POST | `/api/models` | 列表 / 新增 | llm |
| GET/PUT/DELETE | `/api/models/{id}` | 详情 / 更新 / 删除 | llm |
| POST | `/api/models/{id}/enable` | 启用（自动禁用其他） | llm |
| POST | `/api/models/test` | 测试连接 | llm |

### 知识库

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| POST | `/api/upload` | 上传文件并增量入库 | 登录 |
| GET | `/api/docs` | 文档列表（含片段数） | 登录 |
| GET | `/api/docs/download/{filename}` | 下载原始文档 | 登录 |
| DELETE | `/api/docs/{filename}` | 删除文档及其向量 | 登录 |
| GET | `/api/chunks` | 全部向量片段 | 登录 |
| GET | `/api/chunks/{filename}` | 指定文档的片段 | 登录 |

### 对话与系统

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| POST | `/api/chat` | 对话（一次性返回） | 登录 |
| POST | `/api/chat/stream` | 对话（SSE 流式） | 登录 |
| GET | `/api/history` | 对话历史 | 登录 |
| POST | `/api/reset` | 清空当前用户历史 | 登录 |
| GET | `/api/status` | 文档/片段/历史统计 | 登录 |
| GET | `/api/system` | 系统配置与工具清单 | 登录 |

---

## 七、安全与数据隔离设计

| 项 | 设计 |
|----|------|
| 认证 | JWT（HS256），`JWT_SECRET` 首次启动随机生成写入 `.env`，默认 7 天过期 |
| 密码 | PBKDF2 哈希存储，不存明文 |
| API Key | Web 端填写，Fernet 加密后存 `settings.db`；接口只返回脱敏形式（`sk-***abcd`） |
| 知识库隔离 | 每用户独立 Chroma collection：`user_{id}_docs` |
| 记忆隔离 | `messages` 表按 `user_id` 过滤；旧库自动迁移 |
| 文件隔离 | 原始文档存 `data/docs/{user_id}/` |
| 并发安全 | Chroma 写操作与 SQLite 均加线程锁 |

---

## 八、核心原理讲解

### 1. Agent 循环（agent/core.py）

1. 组装上下文：系统提示词 + 最近 N 条历史（`HISTORY_WINDOW=20` 滑动窗口）+ 本轮输入；
2. 请求 LLM：同时携带工具清单（内置工具，OpenAI function-calling 格式）；
3. 模型决策：返回 `tool_calls` → 本地执行，结果以 `role="tool"` 追加后继续循环；无 `tool_calls` → 输出最终回答；
4. 保险丝：`MAX_TOOL_ROUNDS=8` 防死循环。

`chat_stream()` 使用 `stream=True`，将 delta 内容以 SSE 事件（`text` / `tool` / `done`）实时推给前端，实现打字机效果；工具调用阶段先累积完整参数再执行。

### 2. RAG 知识库（knowledge/）

- **切片**：按空行拆自然段 → 聚合到约 `CHUNK_SIZE=400` 字符/片 → 超长段落硬切并保留 `CHUNK_OVERLAP=50` 重叠；
- **向量化**：`BAAI/bge-small-zh-v1.5`（SentenceTransformer），余弦相似度；
- **存储**：Chroma `PersistentClient` 落盘 `data/chroma/`；
- **增量更新**：`ingest_file()` 先 `delete_by_source` 删同名旧片段再写入，重复上传=替换，新上传=追加；
- **检索即工具**：`search_knowledge_base` 作为工具由模型自主决定调用，top_k=3，带来源标注。

### 3. 记忆（agent/memory.py）

SQLite 存储 user/assistant 最终问答对（工具中间过程不落库），按 `user_id` 隔离；启动时自动检测旧表结构并迁移（补 `user_id` 列、重建表）。

### 4. 多模型（agent/llm_settings.py）

`model_configs` 表存多套配置，`enabled=1` 的唯一一条生效；切换启用时清空 Agent 缓存使新配置立即生效；旧 `settings` 表数据首次启动自动迁移。

---

## 九、配置项（.env）

| 变量 | 默认 | 说明 |
|------|------|------|
| `WEB_HOST` | `0.0.0.0` | 监听地址 |
| `WEB_PORT` | `1129` | 监听端口 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | 初始 Base URL，Web 保存后覆盖 |
| `LLM_MODEL` | `deepseek-chat` | 初始模型标识，Web 保存后覆盖 |
| `EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 向量模型 |
| `JWT_SECRET` | 自动生成 | 首次启动随机生成并写回 |
| `JWT_EXPIRE_HOURS` | `168` | Token 有效期（7 天） |
| `ENCRYPTION_KEY` | 自动生成 | Fernet 密钥，用于加密 API Key |

注意：`.env` 中**不存放任何 API Key**；Key 的唯一入口是 Web 模型设置页。

---

## 十、扩展路线（建议）

1. **接入 MCP**：新增 `agent/mcp_manager.py`，用官方 `mcp` SDK 连接 stdio/http MCP Server，`list_tools` 发现的工具加前缀并入工具清单，`execute_tool` 按前缀路由（方案已设计，待实现）；
2. **检索优化**：Rerank 重排序、相似度阈值过滤、混合检索（关键词 + 向量）；
3. **更好切分**：按 Markdown 标题层级或 token 数切分；
4. **加工具**：在 `agent/tools.py` 增加 schema 与实现即可；
5. **会话管理**：多会话/会话列表。

---

## 十一、常见问题

| 问题 | 解决办法 |
|------|---------|
| 对话页提示未配置 API Key | 控制台 → 模型设置 → 新增并启用一个模型 |
| 模型测试连接报 404/无效响应 | API 地址多数需要 `/v1` 后缀，如 `https://xxx/v1` |
| 登录无反应/页面闪烁 | `Ctrl+Shift+R` 硬刷新；必要时清除 localStorage 后重新登录 |
| embedding 模型下载慢 | 设置 `HF_ENDPOINT=https://hf-mirror.com` 后重试 |
| 知识库回答"没有相关内容" | 确认已在控制台上传文档且入库成功（文档列表可见片段数） |
| PDF 入库被跳过 | 扫描件无文字层，暂不支持 OCR |
| .doc 读取失败 | 用 Word 另存为 .docx，或本机装 Word + pywin32 |
| 想清空某用户知识库 | 控制台删除对应文档；或停服后删 `data/chroma/`（会清空所有用户） |
| 忘记 admin 密码 | 停服后删除 `data/users.db` 重启会重建 admin/123456（用户数据丢失，慎用） |
| 端口被占用 | `.env` 中改 `WEB_PORT` |
