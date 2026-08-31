"""全局配置：集中管理路径、API、模型等常量。"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（config.py 所在目录）
BASE_DIR = Path(__file__).resolve().parent

# 加载 .env 中的环境变量
load_dotenv(BASE_DIR / ".env")

# ---------- LLM（任意 OpenAI 格式兼容 API）----------
# API Key 不再从 .env 读取，唯一来源是 Web 管理页写入的 settings.db（加密存储）。
# base_url / model 仍保留 .env 默认值作为初始值，Web 页面保存后覆盖。
LLM_API_KEY = ""  # 始终为空，仅作 fallback 占位
LLM_BASE_URL = (
    os.getenv("LLM_BASE_URL")
    or os.getenv("DEEPSEEK_BASE_URL")
    or "https://api.deepseek.com"
)
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("MODEL") or "deepseek-chat"

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
def _ensure_jwt_secret() -> str:
    """从 .env 读取 JWT_SECRET，不存在则生成随机值并写回。"""
    existing = os.getenv("JWT_SECRET")
    if existing:
        return existing
    import secrets as _s
    secret = _s.token_urlsafe(32)
    env_path = BASE_DIR / ".env"
    line = f"JWT_SECRET={secret}\n"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if "JWT_SECRET" not in content:
            env_path.write_text(content.rstrip("\n") + "\n" + line, encoding="utf-8")
    else:
        env_path.write_text(line, encoding="utf-8")
    os.environ["JWT_SECRET"] = secret
    return secret


JWT_SECRET = _ensure_jwt_secret()
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "168"))  # 7 天
USERS_DB = str(BASE_DIR / "data" / "users.db")
SETTINGS_DB = str(BASE_DIR / "data" / "settings.db")  # Web 页面保存的 LLM 设置
