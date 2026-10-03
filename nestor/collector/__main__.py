import asyncio

from ..config import load_settings
from ..logs import setup_logging
from .app import run


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
