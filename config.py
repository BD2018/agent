"""全局配置：集中管理路径、API、模型等常量。"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（config.py 所在目录）
BASE_DIR = Path(__file__).resolve().parent

# 加载 .env 中的环境变量
load_dotenv(BASE_DIR / ".env")

# ---------- DeepSeek ----------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = os.getenv("MODEL", "deepseek-chat")

# ---------- 知识库 ----------
DOCS_DIR = BASE_DIR / "data" / "docs"          # 存放原始文档（.md / .txt）
CHROMA_DIR = str(BASE_DIR / "data" / "chroma")  # 向量数据库持久化目录
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

# ---------- Agent 行为 ----------
MAX_TOOL_ROUNDS = 8    # 单轮对话最多允许的工具调用次数，防止死循环
HISTORY_WINDOW = 20    # 发送给模型的最大历史消息条数（滑动窗口）
CHUNK_SIZE = 400       # 文档切分片段的目标长度（字符）
CHUNK_OVERLAP = 50     # 相邻片段的重叠长度，避免语义被切断

# ---------- Web 服务 ----------
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "1129"))

# ---------- 认证 ----------
JWT_SECRET = os.getenv("JWT_SECRET", "agent-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "168"))  # 7 天
USERS_DB = str(BASE_DIR / "data" / "users.db")
