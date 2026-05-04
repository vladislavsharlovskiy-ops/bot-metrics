"""Production entry point for bothost.tech.

Bothost runs a single command in a Docker container and exposes one external
port. We need three things alive at once:

  - the Telegram long-polling bot (bot.py)
  - the dashboard / API (web.py — Flask)
  - the Prodamus webhook receiver (webhook.py — Flask Blueprint)

We mount the webhook Blueprint into the dashboard Flask app, so a single
HTTP server serves everything on PORT (3000 by default):

  GET  /                  — dashboard HTML
  *    /api/*             — dashboard JSON API
  POST /webhook/prodamus  — Prodamus payments
  GET  /health            — healthcheck

Flask runs in a daemon thread; aiogram dispatcher runs in the main thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from bot import main as bot_main
# web.py теперь сам регистрирует webhook_bp (см. конец web.py), поэтому
# здесь мы просто импортируем готовое приложение.
from web import app as web_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("entrypoint")

PORT = int(os.environ.get("PORT", "3000"))


def run_http() -> None:
    log.info("Starting HTTP (dashboard + webhook) on 0.0.0.0:%d", PORT)
    web_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    threading.Thread(target=run_http, daemon=True).start()
    asyncio.run(bot_main())
