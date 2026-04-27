"""Production entry point for bothost.tech.

Bothost runs a single command in a Docker container, but this bot needs two
processes: the Telegram long-polling bot (bot.py) and the Prodamus webhook
receiver (webhook.py, Flask). We start Flask in a daemon thread and run the
aiogram dispatcher in the main thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

import webhook as webhook_module
from bot import main as bot_main

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("entrypoint")

PORT = int(os.environ.get("PORT", "3000"))


def run_webhook() -> None:
    log.info("Starting Prodamus webhook on 0.0.0.0:%d", PORT)
    webhook_module.app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    threading.Thread(target=run_webhook, daemon=True).start()
    asyncio.run(bot_main())
