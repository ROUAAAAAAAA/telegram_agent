

import logging

from bot.handlers import build_application
from config import settings

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.DEBUG if settings.debug else logging.INFO,
)

for _noisy in ("httpcore", "httpx", "openai._base_client", "telegram.ext.ExtBot"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    logger.info("Starting Terrasol Telegram bot (polling mode)…")
    app = build_application()
    app.run_polling(drop_pending_updates=True)
