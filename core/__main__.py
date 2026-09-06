"""`python -m core` 直接启动 uvicorn（127.0.0.1 环回）。"""
import uvicorn

from core.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "core.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
