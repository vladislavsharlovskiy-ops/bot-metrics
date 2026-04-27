"""
Polling-клиент: каждые 60 секунд опрашивает webhook.site и забирает новые
платежи Prodamus. Использует ту же логику обработки, что был в webhook.py.
После обработки удаляет запрос из webhook.site, чтобы не повторно поднимать.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from sqlalchemy import or_, select

load_dotenv(Path(__file__).resolve().parent / ".env")

from db import get_session  # noqa: E402
from models import Client, Lead, Payment, RepeatSession  # noqa: E402
from prodamus import extract_payment, parse_form_to_dict, verify  # noqa: E402
from stages import REPEAT_PAID  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("poller")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_UUID = os.environ.get("WEBHOOK_SITE_UUID", "").strip()
POLL_INTERVAL = 30  # секунд

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
WSITE_API = f"https://webhook.site/token/{WEBHOOK_UUID}"


# ───────── HTTP helpers ─────────

def _http(method: str, url: str, data: Optional[bytes] = None,
          headers: Optional[Dict[str, str]] = None, timeout: int = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def tg_send(text: str, reply_markup: Optional[dict] = None) -> None:
    payload = {"chat_id": OWNER_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        _http("POST", f"{TG_API}/sendMessage",
              data=urllib.parse.urlencode(payload).encode(),
              headers={"Content-Type": "application/x-www-form-urlencoded"})
    except Exception as e:
        log.warning("tg_send failed: %s", e)


# ───────── webhook.site API ─────────

def fetch_requests() -> list[dict]:
    url = f"{WSITE_API}/requests?sorting=oldest&per_page=50"
    status, body = _http("GET", url)
    if status != 200:
        log.warning("webhook.site fetch failed %s: %s", status, body[:200])
        return []
    try:
        d = json.loads(body)
        return d.get("data", []) or []
    except Exception as e:
        log.warning("bad json: %s", e)
        return []


def delete_request(request_uuid: str) -> None:
    _http("DELETE", f"{WSITE_API}/request/{request_uuid}")


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


def _money(amount: float, currency: str) -> str:
    sym = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)
    return f"{amount:,.0f}".replace(",", " ") + " " + sym


def _payment_header(p: Payment) -> str:
    lines = [f"💰 <b>Платёж {_money(p.amount, p.currency)}</b>"]
    who = p.customer_name or p.customer_phone or p.customer_email or "—"
    lines.append(f"👤 {who}")
    if p.customer_phone: lines.append(f"📱 {p.customer_phone}")
    if p.customer_email: lines.append(f"✉️ {p.customer_email}")
    if p.product:        lines.append(f"🛍 {p.product}")
    if p.paid_at:        lines.append(f"🕒 {p.paid_at:%d.%m.%Y %H:%M}")
    return "\n".join(lines)


def _notify_auto_repeat(payment: Payment, client: Client) -> None:
    text = _payment_header(payment) + (
        f"\n\n✅ Авто-учёт как <b>повторка</b> от клиента «{client.name or client.phone}»."
    )
    kb = {"inline_keyboard": [[
        {"text": "↩︎ Это первичка", "callback_data": f"pay:flip:{payment.id}"},
        {"text": "🚫 Игнор",         "callback_data": f"pay:ignore:{payment.id}"},
    ]]}
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
    rows.append([{"text": "🚫 Игнорировать",
                   "callback_data": f"pay:ignore:{payment.id}"}])
    tg_send(text, {"inline_keyboard": rows})


def _parse_datetime(raw: str) -> datetime:
    if not raw: return datetime.now()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
    return datetime.now()


# ───────── процессор одного payload'а ─────────

def process_payload(form: Dict[str, str], request_uuid: str) -> bool:
    """Обрабатывает одни form-данные. Возвращает True, если можно удалять из webhook.site."""
    sig = form.pop("signature", "")
    nested = parse_form_to_dict(form)

    if not verify(nested, sig):
        log.warning("bad signature for request %s — discarding", request_uuid)
        return True

    p = extract_payment(nested)
    if p["payment_status"] not in ("success", "paid"):
        log.info("skip status=%s", p["payment_status"])
        return True

    if not p["prodamus_id"]:
        return True

    paid_at = _parse_datetime(p["paid_at"])

    with get_session() as session:
        existing = session.execute(
            select(Payment).where(Payment.prodamus_id == p["prodamus_id"])
        ).scalars().first()
        if existing:
            return True

        client = _match_client(session, p["customer_phone"], p["customer_email"])
        lead = None if client else _match_lead(session, p["customer_name"], p["customer_phone"])

        payment = Payment(
            prodamus_id=p["prodamus_id"],
            amount=p["amount"], currency=p["currency"], paid_at=paid_at,
            customer_name=p["customer_name"], customer_phone=p["customer_phone"],
            customer_email=p["customer_email"], product=p["product"],
            raw_json=json.dumps(nested, ensure_ascii=False)[:8000],
        )
        if client:
            payment.client_id = client.id
            payment.payment_type = "repeat"
            client.last_payment_at = paid_at
            session.add(payment); session.commit(); session.refresh(payment)
            _notify_auto_repeat(payment, client)
        else:
            payment.payment_type = "unclassified"
            if lead: payment.lead_id = lead.id
            session.add(payment); session.commit(); session.refresh(payment)
            _notify_classify(payment, lead)

    return True


# ───────── main loop ─────────

def main() -> None:
    if not WEBHOOK_UUID:
        log.error("WEBHOOK_SITE_UUID not set in .env, exiting")
        return
    log.info("Poller started, polling every %ss", POLL_INTERVAL)
    while True:
        try:
            requests = fetch_requests()
            if requests:
                log.info("got %d new request(s) from webhook.site", len(requests))
            for r in requests:
                request_uuid = r.get("uuid")
                content = r.get("content") or ""
                headers = r.get("headers") or {}
                # webhook.site headers: {'sign': ['value'], ...} (lowercase keys, list values)
                sign_header = ""
                for hk in ("sign", "signature", "x-signature"):
                    v = headers.get(hk)
                    if v:
                        sign_header = v[0] if isinstance(v, list) else str(v)
                        break
                try:
                    form = dict(urllib.parse.parse_qsl(content, keep_blank_values=True))
                except Exception as e:
                    log.warning("bad content for %s: %s", request_uuid, e)
                    delete_request(request_uuid)
                    continue
                if sign_header and "signature" not in form:
                    form["signature"] = sign_header
                if not sign_header and "signature" not in form:
                    log.warning("no signature found anywhere. headers=%s",
                                json.dumps(headers, ensure_ascii=False)[:500])
                if process_payload(form, request_uuid):
                    delete_request(request_uuid)
        except Exception as e:
            log.exception("poll cycle error: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
