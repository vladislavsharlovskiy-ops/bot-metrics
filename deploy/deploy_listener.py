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
from urllib.parse import unquote

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deploy-listener")

ENV_FILE = "/opt/bot-metrics/.env"
DEPLOY_SCRIPT = "/opt/bot-metrics/bin/deploy.sh"
PORT = int(os.environ.get("DEPLOY_LISTENER_PORT", "9876"))


def _read_deploy_secret() -> str:
    """Читает DEPLOY_SECRET из .env на каждом запросе.

    Раньше брали один раз на старте через os.environ — но systemd подгружает
    EnvironmentFile только при старте сервиса. Если кто-то менял .env
    (или install.sh регенерировал секрет) и не рестартанул listener — в памяти
    оставался старый секрет, бот через /deployurl показывал новый, и POST
    через GitHub Actions ловил HTTP 403 forbidden навечно.

    Re-read дёшев (одна-две сотни байт на запрос), зато развязывает
    рестарт-зависимость и /deployurl всегда показывает то, что listener
    реально проверяет.

    Если в .env вдруг несколько строк DEPLOY_SECRET= (приклеилось install.sh'ем
    второй раз) — берём ПОСЛЕДНЮЮ. Это совпадает с поведением systemd
    EnvironmentFile, который для дублей берёт last-wins. Иначе бот через
    os.environ возвращал бы одно значение, а listener — другое.
    """
    secret = ""
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                if line.startswith("DEPLOY_SECRET="):
                    secret = line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return secret or os.environ.get("DEPLOY_SECRET", "").strip()


if not _read_deploy_secret():
    log.error("DEPLOY_SECRET не задан в %s — listener не стартует", ENV_FILE)
    sys.exit(1)


def _path_matches(path: str, secret: str) -> bool:
    """Толерантное сравнение пути с ожидаемым /__deploy/<secret>.

    Учитывает частые источники рассинхрона:
    - trailing slash (если в DEPLOY_URL secret кто-то приклеил `/` в конце)
    - URL-encoded символы (CR/LF превращается в %0A — частый артефакт
      копи-пасты из веб-форм)
    - leading/trailing whitespace в самом self.path

    Раньше был строгий != сравнение, и если в GitHub-секрете оказывался
    trailing slash или невидимый перевод строки — listener возвращал 403
    forbidden навсегда, а из логов не было понятно почему.
    """
    cleaned = unquote(path or "").strip().rstrip("/")
    expected = f"/__deploy/{secret}".rstrip("/")
    return cleaned == expected


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, msg: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(msg.encode())

    def do_GET(self) -> None:
        # GitHub при создании вебхука сначала шлёт ping (с GET для health).
        # Возвращаем 200, чтобы зелёная галочка появилась.
        if _path_matches(self.path, _read_deploy_secret()):
            self._reply(200, "deploy-listener ok\n")
        else:
            self._reply(404, "not found\n")

    def do_POST(self) -> None:
        secret = _read_deploy_secret()
        if not _path_matches(self.path, secret):
            # Логируем мисматч (без полного секрета — только длину
            # ожидаемого, чтобы из journalctl можно было понять КАК именно
            # клиент промахнулся: trailing slash, левый префикс, опечатка).
            cleaned = unquote(self.path or "").strip().rstrip("/")
            log.warning(
                "POST 403: cleaned-path=%r expected-prefix=%r expected-secret-len=%d",
                cleaned, "/__deploy/", len(secret),
            )
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
