"""Web 入口：python web_main.py

启动 FastAPI + Uvicorn 服务，提供文件上传和对话的 Web 界面。
启动后浏览器访问 http://localhost:1129

与 CLI（python main.py）并存，共用同一套 agent / 知识库代码。
"""
import uvicorn

import config
from web.app import app

if __name__ == "__main__":
    print("=" * 48)
    print("  我的专属 Agent Web 服务")
    print(f"  访问 http://localhost:{config.WEB_PORT}")
    print("=" * 48)
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT)
