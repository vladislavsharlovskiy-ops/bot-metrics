"""
Приёмник webhook'ов от Prodamus.

Отдельный Flask-сервер на порту 8766. Только этот порт пробрасываем наружу
через Cloudflare Tunnel — дашборд (8765) остаётся локальным.

Поток:
1. Prodamus шлёт POST с form-данными платежа + signature.
2. Проверяем подпись.
3. Сохраняем Payment (если ещё не сохранён — Prodamus умеет ретраить).
4. Пытаемся сматчить клиента по телефону, лида по имени/телефону.
5. Шлём владельцу в Telegram карточку платежа с кнопками классификации
   (auto-классифицируется, если уверенный матч с существующим клиентом).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from sqlalchemy import or_, select

load_dotenv(Path(__file__).resolve().parent / ".env")

from db import get_session  # noqa: E402  — после load_dotenv
from models import Client, Lead, Payment  # noqa: E402
from prodamus import extract_payment, parse_form_to_dict, verify  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("webhook")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)


# ───────── Telegram helper ─────────

def tg_send(text: str, reply_markup: Optional[dict] = None) -> None:
    payload = {"chat_id": OWNER_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    data = urllib.parse.urlencode(payload).encode()
    try:
        req = urllib.request.Request(f"{TG_API}/sendMessage", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:
        log.warning("tg_send failed: %s", e)


# ───────── matching ─────────

def _match_client(session, phone: Optional[str], email: Optional[str]) -> Optional[Client]:
    if not phone and not email:
        return None
    conds = []
    if phone:
        conds.append(Client.phone == phone)
    if email:
        conds.append(Client.email == email)
    return session.execute(select(Client).where(or_(*conds))).scalars().first()


def _match_lead(session, name: Optional[str], phone: Optional[str]) -> Optional[Lead]:
    if not name and not phone:
        return None
    conds = []
    if name:
        conds.append(Lead.name.ilike(f"%{name}%"))
    if phone:
        conds.append(Lead.username.ilike(f"%{phone}%"))
    return session.execute(
        select(Lead).where(or_(*conds)).order_by(Lead.updated_at.desc())
    ).scalars().first()


# ───────── route ─────────

@app.post("/webhook/prodamus")
def prodamus_webhook():
    form = request.form.to_dict(flat=True)
    log.info("Webhook received: order_id=%s", form.get("order_id"))

    provided_sig = form.pop("signature", "") or request.headers.get("Sign", "")
    nested = parse_form_to_dict(form)

    if not verify(nested, provided_sig):
        log.warning("Signature mismatch for order_id=%s", form.get("order_id"))
        return jsonify({"error": "bad signature"}), 403

    p = extract_payment(nested)

    # Игнорируем не-success
    if p["payment_status"] not in ("success", "paid"):
        log.info("Skipped status=%s", p["payment_status"])
        return jsonify({"ok": True, "skipped": True})

    if not p["prodamus_id"]:
        return jsonify({"error": "no order_id"}), 400

    paid_at = _parse_datetime(p["paid_at"])

    with get_session() as session:
        # дубликат?
        existing = session.execute(
            select(Payment).where(Payment.prodamus_id == p["prodamus_id"])
        ).scalars().first()
        if existing:
            log.info("Duplicate payment %s — ignored", p["prodamus_id"])
            return jsonify({"ok": True, "duplicate": True})

        # ищем клиента
        client = _match_client(session, p["customer_phone"], p["customer_email"])
        lead = None if client else _match_lead(session, p["customer_name"], p["customer_phone"])

        payment = Payment(
            prodamus_id=p["prodamus_id"],
            amount=p["amount"],
            currency=p["currency"],
            paid_at=paid_at,
            customer_name=p["customer_name"],
            customer_phone=p["customer_phone"],
            customer_email=p["customer_email"],
            product=p["product"],
            raw_json=json.dumps(nested, ensure_ascii=False)[:8000],
        )

        if client:
            # Совпадение с существующим клиентом → авто-повторка
            payment.client_id = client.id
            payment.payment_type = "repeat"
            client.last_payment_at = paid_at
            session.add(payment)
            session.commit()
            session.refresh(payment)
            _notify_auto_repeat(payment, client)
        else:
            # Не нашли клиента — спрашиваем у владельца
            payment.payment_type = "unclassified"
            if lead:
                payment.lead_id = lead.id
            session.add(payment)
            session.commit()
            session.refresh(payment)
            _notify_classify(payment, lead)

    return jsonify({"ok": True})


# ───────── notifications ─────────

def _money(amount: float, currency: str) -> str:
    sym = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)
    return f"{amount:,.0f}".replace(",", " ") + " " + sym


def _payment_header(p: Payment) -> str:
    lines = [f"💰 <b>Платёж {_money(p.amount, p.currency)}</b>"]
    who = p.customer_name or p.customer_phone or p.customer_email or "—"
    lines.append(f"👤 {who}")
    if p.customer_phone:
        lines.append(f"📱 {p.customer_phone}")
    if p.customer_email:
        lines.append(f"✉️ {p.customer_email}")
    if p.product:
        lines.append(f"🛍 {p.product}")
    if p.paid_at:
        lines.append(f"🕒 {p.paid_at:%d.%m.%Y %H:%M}")
    return "\n".join(lines)


def _notify_auto_repeat(payment: Payment, client: Client) -> None:
    text = _payment_header(payment) + (
        f"\n\n✅ Авто-учёт как <b>повторка</b> от клиента «{client.name or client.phone}»."
    )
    kb = {
        "inline_keyboard": [[
            {"text": "↩︎ Это первичка", "callback_data": f"pay:flip:{payment.id}"},
            {"text": "🚫 Игнор",         "callback_data": f"pay:ignore:{payment.id}"},
        ]]
    }
    tg_send(text, kb)


def _notify_classify(payment: Payment, lead: Optional[Lead]) -> None:
    text = _payment_header(payment) + "\n\n❓ <b>Не нашли клиента в базе.</b>"
    rows = []
    if lead:
        text += f"\nПохоже на лид <b>#{lead.id}</b> «{lead.name or lead.username}»."
        rows.append([{"text": f"✅ Первичка от лида #{lead.id}",
                       "callback_data": f"pay:first_lead:{payment.id}:{lead.id}"}])
    rows.append([{"text": "✅ Первичка (новый клиент)",
                   "callback_data": f"pay:first_new:{payment.id}"}])
    rows.append([{"text": "🔁 Повторка (новый клиент)",
                   "callback_data": f"pay:repeat_new:{payment.id}"}])
    rows.append([{"text": "🚫 Игнорировать платёж",
                   "callback_data": f"pay:ignore:{payment.id}"}])
    tg_send(text, {"inline_keyboard": rows})


def _parse_datetime(raw: str) -> datetime:
    if not raw:
        return datetime.now()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
    return datetime.now()


# ───────── healthcheck ─────────

@app.get("/")
def index():
    return "Prodamus webhook receiver. POST /webhook/prodamus"


@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8766, debug=False)
