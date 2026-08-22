# 我的专属 Agent（DeepSeek + RAG 知识库）

一个从零搭建的个人专属 Agent 学习项目：以 DeepSeek 为大脑，支持工具调用（function calling）、对话记忆、以及基于 RAG 的私有知识库问答。全程命令行运行，代码精简、注释完整，适合边跑边学。

---

## 一、它能做什么

- 正常聊天对话（上下文记忆存 SQLite，重启自动恢复）
- 自动判断何时调用工具：查知识库、查当前时间、读项目内文件
- 把你自己的文档变成可问答的知识库（支持 `.md` / `.txt` / `.pdf` / `.doc` / `.docx` / `.xlsx` / `.xls`）
- 全程可观察：每次工具调用都会在终端打印出来

## 二、整体架构

```
用户输入
   │
   ▼
┌─────────────────────── Agent 循环 ───────────────────────┐
│  DeepSeek 思考 ──需要工具?──▶ 执行工具 ──▶ 观察结果 ──┐  │
│       ▲                                                │  │
│       └──────────────── 继续循环 ◀─────────────────────┘  │
│       │ 不需要工具                                        │
│       ▼                                                  │
│     输出最终回答                                          │
└───────────────────────────────────────────────────────────┘

工具之一：search_knowledge_base（RAG 知识库检索）
  离线入库：文档(.md/.txt) ─▶ 切分(chunk) ─▶ Embedding 向量化 ─▶ Chroma 向量库
  在线检索：用户问题 ─▶ 向量化 ─▶ 相似度检索 ─▶ 相关片段拼进上下文 ─▶ DeepSeek 回答
```

## 三、目录结构

```
my_agent/
├── main.py                # 命令行入口（对话 + /ingest /reset /quit 指令）
├── config.py              # 全局配置（API、路径、切分参数、循环上限）
├── requirements.txt       # 依赖清单
├── .env.example           # 环境变量模板（复制为 .env 后填 Key）
├── agent/
│   ├── core.py            # Agent 核心：思考-行动-观察循环
│   ├── memory.py          # SQLite 对话记忆持久化（重启不丢）
│   └── tools.py           # 工具定义（schema）与执行分发
├── knowledge/
│   ├── extractors.py      # 多格式文本提取（PDF/Word/Excel/MD/TXT）
│   ├── ingest.py          # 文档切分与入库
│   └── retriever.py       # Embedding + Chroma 向量检索
└── data/
    ├── docs/              # 把你的文档放这里（已附示例文档）
    ├── chroma/            # 向量数据库文件（首次入库后自动生成）
    └── memory.db          # 对话历史数据库（首次对话后自动生成）
```

## 四、搭建与运行步骤

### 第 1 步：准备 Python 环境

要求 Python 3.10 及以上。建议使用虚拟环境：

```bash
cd E:\my_project\腾讯buddy\my_agent
python -m venv .venv
# Windows Git Bash:
source .venv/Scripts/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
```

### 第 2 步：安装依赖

```bash
pip install -r requirements.txt
```

说明：`sentence-transformers` 会顺带安装 PyTorch，体积较大，请耐心等待。

### 第 3 步：配置 API Key

1. 到 https://platform.deepseek.com 注册并创建 API Key；
2. 复制 `.env.example` 为 `.env`；
3. 把 `DEEPSEEK_API_KEY=` 后面改成你的 Key。

```bash
cp .env.example .env
```

### 第 4 步：放入你的文档（可选）

把文档放进 `data/docs/` 目录即可，支持 **`.md` / `.txt` / `.pdf` / `.doc` / `.docx` / `.xlsx` / `.xls`**。项目已自带一篇示例文档，可先用它验证。

注意：
- **扫描件 PDF**（图片型，没有文字层）提取不到内容，需要 OCR，暂不支持；
- 老格式 **`.doc`** 依赖本机安装的 Word（通过 pywin32 调用），更方便的做法是另存为 `.docx`；
- 单个文件解析失败只会跳过该文件并提示原因，不影响其他文档入库。

### 第 5 步：启动并入库

```bash
python main.py
```

进入交互界面后：

```
你 > /ingest        # 文档入库（首次会下载约 100MB 的中文向量模型，只需一次）
你 > Agent 的核心循环是什么？   # 它会先检索知识库再回答
你 > 现在几点？      # 它会调用 get_current_time 工具
你 > /reset         # 清空对话历史
你 > /quit          # 退出
```

如果之前聊过，启动时会提示"已从 SQLite 恢复 N 条历史对话"。

如果 HuggingFace 下载 embedding 模型太慢，先执行（Git Bash）：

```bash
export HF_ENDPOINT=https://hf-mirror.com
```

## 五、核心原理讲解

### 1. Agent 循环（agent/core.py）

这是整个项目的灵魂，对应代码中 `Agent.chat()` 的 `for` 循环：

1. **组装上下文**：系统提示词 + 最近 N 条历史（滑动窗口）+ 本轮输入；
2. **请求 DeepSeek**：同时把工具清单 `TOOLS` 交给模型；
3. **模型决策**：
   - 返回 `tool_calls` → 本地执行对应工具，把结果以 `role="tool"` 追加进 messages，**继续循环**，让模型基于工具结果再思考；
   - 没有 `tool_calls` → 说明可以回答了，把答案写入历史并返回；
4. **保险丝**：`MAX_TOOL_ROUNDS = 8`，防止模型陷入无限工具调用。

### 2. RAG 知识库（knowledge/ 目录）

- **入库（ingest.py + extractors.py）**：先按格式提取纯文本（PDF 用 pypdf、Word 用 python-docx、Excel 按行拼接）→ 按空行拆自然段 → 拼成约 400 字的片段（重叠 50 字防语义切断）→ bge 中文模型向量化 → 写入 Chroma（落盘 `data/chroma/`，重启不丢）。重复入库会先删同名文档旧片段，不会重复。
- **检索（retriever.py）**：用户问题加 bge 推荐的查询指令前缀后向量化，用余弦相似度取最相关的 3 个片段，带来源标注拼成文本，作为工具结果返回给模型。
- **为什么检索本身也是一个工具**：这样 Agent 可以自己决定"这个问题要不要查知识库"，而不是每句话都无脑检索，更省 token 也更聪明。

### 3. 记忆（agent/memory.py）

对话历史存储在 SQLite（`data/memory.db`），重启程序后自动恢复，启动时会提示恢复了多少条历史。只存 user/assistant 的最终问答对，工具调用的中间过程不落库；通过 `HISTORY_WINDOW = 20` 控制发送给模型的最大消息数，避免上下文超长。`/reset` 会清空数据库中当前会话的全部历史。

## 六、如何扩展（建议的进阶路线）

1. **加工具**：在 `agent/tools.py` 的 `TOOLS` 加描述、在 `execute_tool()` 加实现，例如网页搜索、计算器、查天气；
2. **换界面**：用 Gradio / Streamlit 包一层 Web UI；
3. **检索优化**：加 Rerank 重排序（bge-reranker）、相似度阈值过滤、混合检索（关键词 + 向量）；
4. **多会话**：`MemoryStore` 已支持 session_id，可以加 `/new`、`/sessions` 指令管理多个对话；
5. **更好的切分**：引入 LangChain 的 `RecursiveCharacterTextSplitter`，或按 Markdown 标题切。

## 七、常见问题

| 问题 | 解决办法 |
|------|---------|
| 提示未配置 API Key | 确认 `.env` 存在且 Key 填对，文件名不是 `.env.txt` |
| embedding 模型下载失败/太慢 | 设置 `HF_ENDPOINT=https://hf-mirror.com` 后重试 |
| 知识库回答说"没有相关内容" | 确认执行过 `/ingest`，且文档在 `data/docs/` 下 |
| PDF 入库时被跳过 | 多半是扫描件（无文字层），本项目暂不支持 OCR |
| .doc 读取失败 | 用 Word 另存为 .docx 后放入，或安装 pywin32 且本机装有 Word |
| 想清空知识库重来 | 删除 `data/chroma/` 目录后重新 `/ingest` |
| 想清空对话历史 | 运行中输入 `/reset`，或直接删除 `data/memory.db` |
| DeepSeek 报余额不足 | 到 platform.deepseek.com 充值，deepseek-chat 价格很低 |
