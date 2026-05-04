"""
Приёмник webhook'ов от Prodamus.

Регистрируется как Blueprint в общем Flask-приложении (см. main.py).
В проде раздаётся через тот же домен, что и дашборд:
  POST /webhook/prodamus  — приём платежей
  GET  /health            — healthcheck

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
from flask import Blueprint, jsonify, request
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

bp = Blueprint("webhook", __name__)


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


def _phone_digits(phone: Optional[str]) -> Optional[str]:
    """Только цифры из телефона; берём последние 10 — это надёжное ядро номера в РФ."""
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else (digits or None)


def _match_leads(
    session,
    name: Optional[str],
    phone: Optional[str],
    email: Optional[str],
    limit: int = 5,
) -> list[Lead]:
    """
    Ищет кандидатов-лидов по имени (по словам), телефону и email.
    Возвращает до `limit` лидов, отсортированных по свежести.
    """
    conds = []
    if name:
        # Бьём имя на слова, ищем каждое слово >=3 букв в Lead.name и Lead.username.
        # Это ловит «Зарема» → «Зарема Цеева», и наоборот.
        for word in name.split():
            if len(word) >= 3:
                conds.append(Lead.name.ilike(f"%{word}%"))
                conds.append(Lead.username.ilike(f"%{word}%"))
    if phone:
        conds.append(Lead.username.ilike(f"%{phone}%"))
        digits = _phone_digits(phone)
        if digits and len(digits) >= 7:
            # Ищем по последним 7+ цифрам — поймает форматы +7..., 8..., с пробелами и т.п.
            conds.append(Lead.username.ilike(f"%{digits[-7:]}%"))
    if email:
        conds.append(Lead.username.ilike(f"%{email}%"))
    if not conds:
        return []
    rows = session.execute(
        select(Lead).where(or_(*conds))
        .order_by(Lead.updated_at.desc())
        .limit(limit)
    ).scalars().all()
    return list(rows)


# ───────── route ─────────

@bp.post("/webhook/prodamus")
def prodamus_webhook():
    form = request.form.to_dict(flat=True)
    log.info("Webhook received: order_id=%s", form.get("order_id"))

    provided_sig = form.pop("signature", "") or request.headers.get("Sign", "")
    nested = parse_form_to_dict(form)

    if not verify(nested, provided_sig):
        log.warning("Signature mismatch for order_id=%s", form.get("order_id"))
        # Чтобы оплата не потерялась тихо — шлём владельцу уведомление со всеми
        # полями платежа. Дальше он либо поправит PRODAMUS_SECRET_KEY (через
        # /setprodamuskey в боте), либо дозапишет вручную (/addpayment).
        # Возвращаем 403, чтобы Prodamus продолжал ретраить — после починки
        # ключа повторный запрос пройдёт нормально.
        try:
            _notify_owner_bad_signature(nested, form)
        except Exception as e:
            log.warning("Failed to notify owner about bad signature: %s", e)
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
        lead_candidates = []
        if not client:
            lead_candidates = _match_leads(
                session,
                p["customer_name"],
                p["customer_phone"],
                p["customer_email"],
            )

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
            # Не нашли клиента — спрашиваем у владельца, какой лид (или новый клиент)
            payment.payment_type = "unclassified"
            if lead_candidates:
                payment.lead_id = lead_candidates[0].id
            session.add(payment)
            session.commit()
            session.refresh(payment)
            _notify_classify(payment, lead_candidates)

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


def _notify_owner_bad_signature(nested: dict, raw_form: dict) -> None:
    """
    Webhook от Prodamus пришёл, но signature не сошлась → платёж в БД НЕ
    записан, в дашборд НЕ попал. Чтобы оплата не потерялась — отдаём
    владельцу карточку со всеми данными, которые нашли в payload, и
    подсказку что делать.
    """
    p = extract_payment(nested)
    name = p.get("customer_name") or "—"
    phone = p.get("customer_phone") or "—"
    email = p.get("customer_email") or "—"
    amount = p.get("amount") or 0
    currency = p.get("currency") or "RUB"
    order_id = p.get("prodamus_id") or raw_form.get("order_id") or "—"
    product = p.get("product") or "—"

    # JSON-блок, готовый к копированию в /addpayment (если оплата реальная,
    # но signature пока не проходит — пользователь дозапишет одной командой).
    addpayment_arg = json.dumps(nested, ensure_ascii=False)
    # Telegram не любит >4096 символов в одном сообщении — обрезаем.
    if len(addpayment_arg) > 3500:
        addpayment_arg = addpayment_arg[:3500] + "…"

    text = (
        "⚠ <b>Webhook от Prodamus с неверной подписью</b>\n\n"
        f"💰 <b>{amount:.0f} {currency}</b>\n"
        f"👤 {name}\n"
        f"📧 {email}\n"
        f"📱 {phone}\n"
        f"🛍 {product}\n"
        f"🆔 order_id: <code>{order_id}</code>\n\n"
        "Оплата <b>НЕ записана в БД</b>. Возможные причины:\n"
        "• PRODAMUS_SECRET_KEY в .env пустой/неправильный — "
        "поправь через <code>/setprodamuskey &lt;key&gt;</code>\n"
        "• Запрос не от Prodamus (попытка взлома)\n\n"
        "Если оплата реальная — дозапиши вручную:\n"
        f"<code>/addpayment {addpayment_arg}</code>\n\n"
        "Дальше /fixpay для классификации (привязать к лиду / создать клиента)."
    )
    tg_send(text)


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


def _lead_button_label(lead: Lead) -> str:
    """Подпись кнопки для лида: имя + источник, обрезаем до ~40 символов."""
    name = lead.name or lead.username or f"#{lead.id}"
    src = lead.source or ""
    label = f"🎯 #{lead.id} · {name}"
    if src:
        label += f" · {src}"
    return label[:48]


def _notify_classify(payment: Payment, lead_candidates: list[Lead]) -> None:
    text = _payment_header(payment)
    rows = []
    if lead_candidates:
        text += "\n\n🔍 <b>Возможные совпадения с лидами:</b>"
        for lead in lead_candidates:
            text += f"\n• #{lead.id} «{lead.name or lead.username}» — {lead.source}"
            rows.append([{
                "text": _lead_button_label(lead),
                "callback_data": f"pay:first_lead:{payment.id}:{lead.id}",
            }])
        text += "\n\nЕсли это новый клиент — нажми «Новый клиент»."
    else:
        text += "\n\n❓ <b>Не нашли клиента в базе.</b>"
    rows.append([{"text": "➕ Новый клиент (первичка)",
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

@bp.get("/health")
def health():
    return jsonify({"ok": True})
