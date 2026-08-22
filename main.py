"""命令行入口：python main.py

支持的指令：
  /ingest   把 data/docs/ 下的文档入库到知识库
  /reset    清空对话历史
  /quit     退出
直接输入文字即可与 Agent 对话。
"""
import sys

from agent.core import Agent
from knowledge.ingest import ingest_docs

HELP = """可用指令：
  /ingest   文档入库（把 data/docs/ 下的 .md/.txt 写入知识库）
  /reset    清空对话历史
  /quit     退出
"""


def main():
    print("=" * 48)
    print("  我的专属 Agent（DeepSeek + RAG 知识库）")
    print("=" * 48)
    print(HELP)

    try:
        agent = Agent()
    except RuntimeError as e:
        print(e)
        sys.exit(1)

    restored = agent.memory.count()
    if restored:
        print(f"已从 SQLite 恢复 {restored} 条历史对话（/reset 可清空）。")

    while True:
        try:
            user = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user:
            continue
        if user in ("/quit", "/exit"):
            print("再见！")
            break
        if user == "/reset":
            agent.reset()
            print("对话历史已清空。")
            continue
        if user == "/ingest":
            print("开始入库（首次运行需下载 embedding 模型，请耐心等待）...")
            try:
                ingest_docs()
            except Exception as e:
                print(f"入库失败：{e}")
            continue

        try:
            answer = agent.chat(user, on_event=lambda e: print(f"  [agent] {e}"))
        except Exception as e:
            print(f"出错了：{e}")
            continue
        print(f"\n助手 > {answer}")


if __name__ == "__main__":
    main()
