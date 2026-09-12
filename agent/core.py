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
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    APIStatusError,
    RateLimitError,
)

import config
from agent.llm_settings import get_llm_settings
from agent.memory import MemoryStore
from agent.tools import execute_tool, get_all_tools

SYSTEM_PROMPT = """你是用户的专属 AI 助手，请遵守以下规则：
1. 回答涉及用户私有资料（文档、笔记、项目信息）的问题前，必须先调用 search_knowledge_base 检索知识库；
2. 知识库检索结果与问题相关时，优先依据知识库内容回答；
3. 知识库内容与问题无关或未检索到相关内容时，直接用你自身知识回答用户的问题本身，回答里不要出现任何与知识库、检索、片段相关的字眼；
4. 回答必须是直接呈现给用户的最终答复：不要输出你的思考过程、行动计划、内心独白或自我对话；
5. 回答语言与提问语言一致：用户用中文提问就用简体中文回答，用户用其他语言提问就用相同语言回答，无法判断时用简体中文；简洁清晰，直接给出答案本身，不要复述工具返回的原文或检索日志；
6. 需要知道当前时间时，调用 get_current_time；
7. 涉及数值计算时，若有匹配的计算类工具，必须调用工具获得精确结果，禁止自行心算或估算；
8. 没有合适的工具或能力完成请求时，用简体中文直接说明无法完成及原因，并主动提供你能力范围内的替代帮助；
9. 工具返回错误时，根据错误信息修正参数后重试，仍失败则如实告知用户原因。"""


def _has_cjk(s: str) -> bool:
    """是否包含中文字符（用于「中文提问却得到外文回答」的检测）。"""
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def _friendly_llm_error(e: Exception) -> str:
    """把模型调用异常翻译成用户可读的中文提示（网络/鉴权/限流/服务/空内容）。"""
    if isinstance(e, (APIConnectionError, APITimeoutError)):
        return "网络异常：无法连接模型服务，请检查服务器网络后重试。"
    if isinstance(e, AuthenticationError):
        return "模型鉴权失败：API Key 无效或已过期，请在控制台检查模型配置。"
    if isinstance(e, RateLimitError):
        return "模型服务限流：请求过于频繁或额度不足，请稍后重试。"
    if isinstance(e, APIStatusError):
        return f"模型服务异常（HTTP {getattr(e, 'status_code', '?')}），请稍后重试。"
    return f"模型调用异常：{e}"


class Agent:
    def __init__(self, user_id: int = 1):
        self.user_id = user_id
        self._client = None
        self._client_config = None  # 构建 _client 时使用的 (api_key, base_url)
        self._role_reminder = ""    # 用户角色定义（每轮对话追加为收尾提醒）
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

    def _reply_lang_policy(self, user_input: str):
        """回答语言策略：'zh' | 'latin' | None（不强制）。

        角色定义明确固定语言时以角色为准；否则跟随提问语言。
        """
        role = (self._role_reminder or "")
        role_low = role.lower()
        # 角色定义要求语言跟随提问时，不固定语言
        if not ("一致" in role or "跟随" in role or "提问语言" in role):
            if "中文" in role or "chinese" in role_low:
                return "zh"
            if "英文" in role or "english" in role_low:
                return "latin"
        # 默认：跟随提问语言
        if _has_cjk(user_input):
            return "zh"
        letters = sum(1 for c in user_input if c.isascii() and c.isalpha())
        if letters >= 4 and letters >= len(user_input.replace(" ", "")) * 0.6:
            return "latin"
        return None

    def _lang_violation(self, user_input: str, answer: str):
        """判断回答是否违反语言策略，返回 (是否违反, 纠偏提示语)。"""
        target = self._reply_lang_policy(user_input)
        if target == "zh" and not _has_cjk(answer):
            letters = sum(1 for c in answer if c.isascii() and c.isalpha())
            if letters >= 3:
                return True, ("你上一次的回答没有使用简体中文，这是错误的。"
                              "请立即用简体中文重新给出面向用户的最终回答，不要输出思考过程。")
            return False, ""
        if target == "latin" and _has_cjk(answer):
            return True, ("用户使用非中文语言提问，回答语言必须与提问语言一致。"
                          "请立即用与提问相同的语言重新给出面向用户的最终回答。")
        return False, ""

    def reset(self):
        self.memory.clear()

    def _build_system_prompt(self) -> str:
        """用户自定义角色定义拥有最高优先级，置于提示词最前；默认规则在其后。"""
        from auth.models import get_role_prompt
        self._role_reminder = (get_role_prompt(self.user_id) or "").strip()
        if self._role_reminder:
            return (
                "【用户自定义要求（最高优先级，必须逐条严格遵守，与任何默认规则冲突时以本节为准）】\n"
                + self._role_reminder + "\n\n" + SYSTEM_PROMPT
            )
        return SYSTEM_PROMPT

    def chat(self, user_input, on_event=None):
        """处理一轮用户输入，返回最终回答。on_event 用于打印中间过程。"""
        # 固定问答优先拦截：语义相似度达标则直接返回预设回答
        from knowledge.qa_store import search_qa
        from knowledge.retriever import QUERY_INSTRUCTION
        qa_answer = search_qa(self.user_id, user_input, QUERY_INSTRUCTION)
        if qa_answer:
            self.memory.append("user", user_input)
            self.memory.append("assistant", qa_answer)
            return qa_answer

        client, model = self._get_client()
        messages = [{"role": "system", "content": self._build_system_prompt()}]
        messages.extend(self.memory.load_recent(config.HISTORY_WINDOW))
        messages.append({"role": "user", "content": user_input})

        lang_retried = False

        for _ in range(config.MAX_TOOL_ROUNDS):
            if self._role_reminder:
                messages.append({"role": "system", "content": "【再次提醒，必须严格遵守】" + self._role_reminder})
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=get_all_tools(),
                )
            except Exception as e:
                raise RuntimeError(_friendly_llm_error(e)) from e
            msg = resp.choices[0].message

            if not msg.tool_calls:
                answer = (msg.content or "").strip()
                if not answer:
                    raise RuntimeError("模型异常：模型返回了空内容，请重试或换个问法。")
                # 语言守卫：回答语言必须符合语言策略（跟随提问/角色定义），纠偏重试一次
                bad, fix_msg = self._lang_violation(user_input, answer)
                if bad:
                    if not lang_retried:
                        lang_retried = True
                        messages.append({"role": "assistant", "content": answer})
                        messages.append({"role": "system", "content": fix_msg})
                        continue
                    answer = "【模型异常：模型未能按要求的语言回答，建议重试或更换模型渠道】\n\n" + answer
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
        # 固定问答优先拦截
        from knowledge.qa_store import search_qa
        from knowledge.retriever import QUERY_INSTRUCTION
        qa_answer = search_qa(self.user_id, user_input, QUERY_INSTRUCTION)
        if qa_answer:
            self.memory.append("user", user_input)
            self.memory.append("assistant", qa_answer)
            yield ("text", qa_answer)
            yield ("done", "")
            return

        client, model = self._get_client()
        messages = [{"role": "system", "content": self._build_system_prompt()}]
        messages.extend(self.memory.load_recent(config.HISTORY_WINDOW))
        messages.append({"role": "user", "content": user_input})

        target_lang = self._reply_lang_policy(user_input)
        enforce_lang = target_lang is not None
        lang_retried = False

        for _ in range(config.MAX_TOOL_ROUNDS):
            content_parts = []
            tool_calls = {}
            has_tool_calls = False
            lang_settled = not enforce_lang  # 有语言守卫时先缓冲首段，确认输出语言后再流式放行
            held = ""  # 放行前缓冲的文本

            if self._role_reminder:
                messages.append({"role": "system", "content": "【再次提醒，必须严格遵守】" + self._role_reminder})
            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=get_all_tools(),
                    stream=True,
                )

                for chunk in stream:
                    delta = chunk.choices[0].delta

                    if delta.content:
                        content_parts.append(delta.content)
                        if lang_settled:
                            yield ("text", delta.content)
                        else:
                            held += delta.content
                            hit_zh = _has_cjk(held)
                            hit_lat = sum(1 for c in held if c.isascii() and c.isalpha()) >= 12
                            if (target_lang == "zh" and hit_zh) or (target_lang == "latin" and hit_lat and not hit_zh):
                                # 输出语言符合预期，放行缓冲并恢复实时流式
                                lang_settled = True
                                yield ("text", held)
                                held = ""
                            elif (target_lang == "zh" and hit_lat and not hit_zh) or (target_lang == "latin" and hit_zh):
                                if not lang_retried:
                                    # 输出语言与预期不符：中断本轮，纠偏重试
                                    lang_retried = True
                                    full = "".join(content_parts).strip()
                                    _, fix_msg = self._lang_violation(user_input, full)
                                    messages.append({"role": "assistant", "content": full})
                                    messages.append({"role": "system", "content": fix_msg})
                                    try:
                                        stream.close()
                                    except Exception:
                                        pass
                                    break
                                # 已重试过：不再中断，静默缓冲完整内容，结束后标注模型异常
                            # 其余情况（如全数字、语言特征未明）：继续缓冲

                    if delta.tool_calls:
                        has_tool_calls = True
                        if held:  # 罕见：先有文本再有工具调用，原样放行
                            lang_settled = True
                            yield ("text", held)
                            held = ""
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
            except Exception as e:
                raise RuntimeError(_friendly_llm_error(e)) from e

            if not has_tool_calls:
                full_answer = "".join(content_parts).strip()
                if not full_answer:
                    raise RuntimeError("模型异常：模型返回了空内容，请重试或换个问法。")
                bad, fix_msg = self._lang_violation(user_input, full_answer)
                if bad and enforce_lang:
                    if not lang_retried:
                        # 流自然结束仍不符合语言策略：纠偏重试
                        lang_retried = True
                        messages.append({"role": "assistant", "content": full_answer})
                        messages.append({"role": "system", "content": fix_msg})
                        continue
                    # 已重试过仍不符：只显示简短中文提示，避免向用户倾倒外文内容
                    yield ("text", "【模型异常：模型未能按要求的语言回答，请重试；若持续出现请更换模型渠道】")
                elif held:
                    yield ("text", held)
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
