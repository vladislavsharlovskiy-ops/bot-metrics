#!/usr/bin/env python3
"""
Маленький http-сервис на стандартной библиотеке: слушает 127.0.0.1:9876
и при POST на /__deploy/<DEPLOY_SECRET> запускает deploy.sh.
nginx прокcирует /__deploy/* сюда.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deploy-listener")

DEPLOY_SECRET = os.environ.get("DEPLOY_SECRET", "").strip()
DEPLOY_SCRIPT = "/opt/bot-metrics/bin/deploy.sh"
PORT = int(os.environ.get("DEPLOY_LISTENER_PORT", "9876"))

if not DEPLOY_SECRET:
    log.error("DEPLOY_SECRET не задан — listener не стартует")
    sys.exit(1)


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, msg: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(msg.encode())

    def do_GET(self) -> None:
        # GitHub при создании вебхука сначала шлёт ping (с GET для health).
        # Возвращаем 200, чтобы зелёная галочка появилась.
        if self.path == f"/__deploy/{DEPLOY_SECRET}":
            self._reply(200, "deploy-listener ok\n")
        else:
            self._reply(404, "not found\n")

    def do_POST(self) -> None:
        if self.path != f"/__deploy/{DEPLOY_SECRET}":
            self._reply(403, "forbidden\n")
            return
        # Читаем тело и выбрасываем — нам важен только факт пуша
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length:
            self.rfile.read(length)

        log.info("deploy triggered")
        try:
            # sudo разрешён в /etc/sudoers.d/bot-metrics
            r = subprocess.run(
                ["sudo", DEPLOY_SCRIPT],
                capture_output=True, text=True, timeout=300,
            )
            log.info("deploy.sh exit=%d", r.returncode)
            if r.stdout:
                log.info("stdout:\n%s", r.stdout)
            if r.stderr:
                log.warning("stderr:\n%s", r.stderr)
            if r.returncode == 0:
                self._reply(200, "deployed\n")
            else:
                self._reply(500, f"deploy failed (exit {r.returncode})\n")
        except subprocess.TimeoutExpired:
            log.error("deploy timeout")
            self._reply(504, "deploy timeout\n")
        except Exception as e:
            log.exception("deploy error")
            self._reply(500, f"error: {e}\n")

    # Шумные access-логи в журнал не пишем — у нас и так systemd
    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.client_address[0], fmt % args)


def main() -> None:
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    log.info("deploy-listener on 127.0.0.1:%d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
