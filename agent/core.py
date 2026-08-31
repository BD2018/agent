"""Agent 核心：OpenAI 格式兼容 API + function calling 的「思考 -> 行动 -> 观察」循环。

工作流程：
1. 组装 messages（系统提示 + 滑动窗口历史 + 本轮用户输入）；
2. 调用 LLM，模型要么直接给出最终回答，要么返回 tool_calls；
3. 若有 tool_calls：本地执行工具，把结果以 role=tool 追加回 messages，继续循环；
4. 若无 tool_calls：得到最终回答，写入历史并返回。

按用户隔离：Agent(user_id) 使用该用户专属的 MemoryStore 和 Chroma collection。
LLM 连接（地址/Key/模型）每轮对话前从 llm_settings 读取，
Web 管理页修改后立即生效，无需重启。
"""
from openai import OpenAI

import config
from agent.llm_settings import get_llm_settings
from agent.memory import MemoryStore
from agent.tools import TOOLS, execute_tool

SYSTEM_PROMPT = """你是用户的专属 AI 助手，请遵守以下规则：
1. 回答涉及用户私有资料（文档、笔记、项目信息）的问题前，必须先调用 search_knowledge_base 检索知识库；
2. 如果知识库检索结果与问题无关，请如实说明「知识库中没有相关内容」，不要编造；
3. 回答使用简体中文，简洁清晰；
4. 需要知道当前时间时，调用 get_current_time。"""


class Agent:
    def __init__(self, user_id: int = 1):
        self.user_id = user_id
        self._client = None
        self._client_config = None  # 构建 _client 时使用的 (api_key, base_url)
        self.memory = MemoryStore(user_id)

    def _get_client(self):
        """按最新设置返回 (client, model)；地址或 Key 变化时自动重建 client。"""
        cfg = get_llm_settings()
        if not cfg["api_key"]:
            raise RuntimeError(
                "未配置 LLM API Key，请前往「控制台 → 系统设置」页面填写 API Key 后再使用对话功能。"
            )
        sig = (cfg["api_key"], cfg["base_url"])
        if self._client is None or self._client_config != sig:
            self._client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
            self._client_config = sig
        return self._client, cfg["model"]

    def reset(self):
        self.memory.clear()

    def chat(self, user_input, on_event=None):
        """处理一轮用户输入，返回最终回答。on_event 用于打印中间过程。"""
        client, model = self._get_client()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self.memory.load_recent(config.HISTORY_WINDOW))
        messages.append({"role": "user", "content": user_input})

        for _ in range(config.MAX_TOOL_ROUNDS):
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:
                answer = msg.content or ""
                self.memory.append("user", user_input)
                self.memory.append("assistant", answer)
                return answer

            messages.append(msg.model_dump(exclude_none=True))
            for call in msg.tool_calls:
                if on_event:
                    on_event(
                        f"调用工具 {call.function.name}({call.function.arguments})"
                    )
                result = execute_tool(call.function.name, call.function.arguments, self.user_id)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.function.name,
                        "content": result,
                    }
                )

        return "已达到最大工具调用轮数仍未完成，请换个方式提问或缩小问题范围。"

    def chat_stream(self, user_input):
        """流式处理：生成器逐 token yield (event_type, content)。

        event_type:
          "text"  — 文本片段，前端逐字追加渲染
          "tool"  — 工具调用事件，前端展示为提示
          "done"  — 全部结束
        """
        client, model = self._get_client()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self.memory.load_recent(config.HISTORY_WINDOW))
        messages.append({"role": "user", "content": user_input})

        full_answer = ""

        for _ in range(config.MAX_TOOL_ROUNDS):
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                stream=True,
            )

            content_parts = []
            tool_calls = {}
            has_tool_calls = False

            for chunk in stream:
                delta = chunk.choices[0].delta

                if delta.content:
                    content_parts.append(delta.content)
                    yield ("text", delta.content)

                if delta.tool_calls:
                    has_tool_calls = True
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls:
                            tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.id:
                            tool_calls[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls[idx]["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            tool_calls[idx]["arguments"] += tc.function.arguments

            if not has_tool_calls:
                full_answer = "".join(content_parts)
                if not full_answer:
                    full_answer = "（API 返回了空内容，请检查 API 地址是否需要加 /v1 后缀，或模型名称是否正确）"
                    yield ("text", full_answer)
                self.memory.append("user", user_input)
                self.memory.append("assistant", full_answer)
                yield ("done", "")
                return

            assistant_msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tool_calls[i]["id"],
                        "type": "function",
                        "function": {
                            "name": tool_calls[i]["name"],
                            "arguments": tool_calls[i]["arguments"],
                        },
                    }
                    for i in sorted(tool_calls)
                ],
            }
            messages.append(assistant_msg)

            for i in sorted(tool_calls):
                tc = tool_calls[i]
                yield ("tool", f"调用工具 {tc['name']}({tc['arguments']})")
                result = execute_tool(tc["name"], tc["arguments"], self.user_id)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                        "content": result,
                    }
                )

        fallback = "已达到最大工具调用轮数仍未完成，请换个方式提问或缩小问题范围。"
        yield ("text", fallback)
        self.memory.append("user", user_input)
        self.memory.append("assistant", fallback)
        yield ("done", "")
