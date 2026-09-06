"""AI Agent Trading core 常驻进程。

Web 部署版后端：FastAPI + WebSocket，默认监听 127.0.0.1 环回端口，
经 Nginx 反代 /api、/ws 对外提供同源 HTTPS/WSS 访问（总纲 §12.1/spec-06 §3）。
"""

__version__ = "0.1.0"
