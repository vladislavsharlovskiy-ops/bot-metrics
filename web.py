from __future__ import annotations

import hmac
import logging
from datetime import datetime, time, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from sqlalchemy import func, or_, select

from config import LEADS_API_KEY
from db import get_session
from models import Client, Lead, Payment, StageHistory
from stages import (
    ACTIVE_CODES,
    BY_CODE,
    CLIENT_CODES,
    CONSULTED,
    FUNNEL,
    IGNORING,
    IGNORING_CODES,
    IGNORING_STAGE,
    LEAD_NEW,
    LOST,
    LOST_STAGE,
    PAID,
    PACKAGE_BOUGHT,
    QUALIFIED,
    SOURCES,
    SOURCE_TITLES,
    next_stage,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web")

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))


# ───────── helpers ─────────

def _lead_dict(lead: Lead) -> dict:
    stage = BY_CODE.get(lead.stage)
    nxt = next_stage(lead.stage)
    return {
        "id": lead.id,
        "name": lead.name or "",
        "username": lead.username or "",
        "source": lead.source,
        "source_title": SOURCE_TITLES.get(lead.source, lead.source),
        "request": lead.request or "",
        "notes": lead.notes or "",
        "stage": lead.stage,
        "stage_title": stage.title if stage else lead.stage,
        "stage_short": stage.short if stage else lead.stage,
        "lost_reason": lead.lost_reason or "",
        "next_stage": nxt.code if nxt else None,
        "next_stage_title": nxt.title if nxt else None,
        "created_at": lead.created_at.isoformat(timespec="minutes"),
        "updated_at": lead.updated_at.isoformat(timespec="minutes"),
        "is_active": lead.stage in ACTIVE_CODES,
        "is_lost": lead.stage == LOST,
        "is_ignoring": lead.stage == IGNORING,
    }


def _today_range():
    s = datetime.combine(datetime.now().date(), time.min)
    return s, s + timedelta(days=1)


def _week_range():
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    s = datetime.combine(monday, time.min)
    return s, s + timedelta(days=7)


def _month_range():
    today = datetime.now().date()
    s = datetime.combine(today.replace(day=1), time.min)
    if s.month == 12:
        e = s.replace(year=s.year + 1, month=1)
    else:
        e = s.replace(month=s.month + 1)
    return s, e


def _counts_in_period(start, end, source=None):
    """Distinct lead counts that reached each funnel stage in [start, end)."""
    out = {}
    with get_session() as session:
        for s in FUNNEL:
            q = (
                select(func.count(func.distinct(StageHistory.lead_id)))
                .where(StageHistory.stage == s.code)
                .where(StageHistory.changed_at >= start)
                .where(StageHistory.changed_at < end)
            )
            if source:
                q = q.join(Lead, Lead.id == StageHistory.lead_id).where(Lead.source == source)
            out[s.code] = session.execute(q).scalar_one()
    return out


# ───────── pages ─────────

@app.get("/")
def index():
    return render_template("dashboard.html")


# ───────── API ─────────

@app.get("/api/summary")
def api_summary():
    """Шапка дашборда: всё за текущий месяц + неделя/сегодня для совместимости."""
    today_s, today_e = _today_range()
    week_s, week_e = _week_range()
    month_s, month_e = _month_range()
    today = _counts_in_period(today_s, today_e)
    week = _counts_in_period(week_s, week_e)
    month = _counts_in_period(month_s, month_e)

    with get_session() as session:
        total_active = session.execute(
            select(func.count(Lead.id)).where(Lead.stage.in_(ACTIVE_CODES))
        ).scalar_one()
        total_clients = session.execute(
            select(func.count(Lead.id)).where(Lead.stage.in_(CLIENT_CODES))
        ).scalar_one()

        # Реальные платежи за месяц — игнорируем unclassified/ignored
        month_pay = session.execute(
            select(
                func.coalesce(func.sum(Payment.amount), 0),
                func.count(Payment.id),
            )
            .where(Payment.paid_at >= month_s)
            .where(Payment.paid_at < month_e)
            .where(Payment.payment_type.in_(["first", "repeat"]))
        ).one()
        month_revenue = float(month_pay[0] or 0)
        month_payments = int(month_pay[1] or 0)
        month_avg = (month_revenue / month_payments) if month_payments else 0

        # Уникальных клиентов, заплативших в этом месяце (а не общим итогом).
        # Считаем distinct по client_id и lead_id (на случай платежей без client).
        month_clients = session.execute(
            select(func.count(func.distinct(
                func.coalesce(Payment.client_id, -Payment.lead_id)
            )))
            .where(Payment.paid_at >= month_s)
            .where(Payment.paid_at < month_e)
            .where(Payment.payment_type.in_(["first", "repeat"]))
        ).scalar_one() or 0

    month_label = f"{RU_MONTHS[month_s.month - 1]} {month_s.year}"

    return jsonify({
        "today": today,
        "week": week,
        "month": month,
        "month_label": month_label,
        "total_active": total_active,
        "total_clients": total_clients,
        "month_revenue": month_revenue,
        "month_payments": month_payments,
        "month_clients": int(month_clients),
        "month_avg_check": month_avg,
    })


@app.get("/api/funnel")
def api_funnel():
    """Current snapshot: how many leads are at each stage RIGHT NOW."""
    with get_session() as session:
        rows = session.execute(
            select(Lead.stage, func.count(Lead.id)).group_by(Lead.stage)
        ).all()
    by_stage = {stage: cnt for stage, cnt in rows}
    return jsonify({
        "stages": [
            {"code": s.code, "title": s.title, "short": s.short, "count": by_stage.get(s.code, 0)}
            for s in FUNNEL
        ],
        "lost": by_stage.get(LOST, 0),
    })


@app.get("/api/channels")
def api_channels():
    """Per-channel breakdown for the current month."""
    start, end = _month_range()
    out = []
    for code, title in SOURCES:
        c = _counts_in_period(start, end, source=code)
        leads = c.get(LEAD_NEW, 0)
        paid = c.get(PAID, 0) + c.get("consulted", 0) + c.get(PACKAGE_BOUGHT, 0)
        out.append({
            "source": code,
            "title": title,
            "leads": leads,
            "qualified": c.get("qualified", 0),
            "paid": paid,
            "package": c.get(PACKAGE_BOUGHT, 0),
            "conv_paid": (paid / leads) if leads else 0,
        })
    return jsonify({
        "period": f"{start:%d.%m} – {(end - timedelta(seconds=1)):%d.%m.%Y}",
        "channels": out,
    })


RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def _month_metrics(year: int, month: int) -> dict:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    counts = _counts_in_period(start, end)
    with get_session() as session:
        rev_first = session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.paid_at >= start)
            .where(Payment.paid_at < end)
            .where(Payment.payment_type == "first")
        ).scalar_one()
        rev_repeat = session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.paid_at >= start)
            .where(Payment.paid_at < end)
            .where(Payment.payment_type == "repeat")
        ).scalar_one()
    leads = counts.get(LEAD_NEW, 0)
    paid = counts.get(PAID, 0) + counts.get(CONSULTED, 0) + counts.get(PACKAGE_BOUGHT, 0)
    return {
        "year": year,
        "month": month,
        "label": f"{RU_MONTHS[month - 1]} {year}",
        "leads": leads,
        "qualified": counts.get(QUALIFIED, 0),
        "paid": paid,
        "package_bought": counts.get(PACKAGE_BOUGHT, 0),
        "conv_paid": (paid / leads) if leads else 0,
        "revenue_first": float(rev_first or 0),
        "revenue_repeat": float(rev_repeat or 0),
        "revenue_total": float((rev_first or 0) + (rev_repeat or 0)),
    }


@app.get("/api/months")
def api_months():
    """Метрики за последние 12 месяцев. Только месяцы, где была активность."""
    today = datetime.now().date()
    months = []
    for i in range(12):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        months.append(_month_metrics(y, m))
    # фильтруем месяцы без событий и без выручки
    active = [m for m in months if m["leads"] or m["revenue_total"]]
    return jsonify({"months": active or months[:1]})  # хотя бы текущий


@app.get("/api/period/<scope>")
def api_period(scope: str):
    if scope == "today":
        s, e = _today_range()
        label = f"Сегодня ({s:%d.%m.%Y})"
    elif scope == "week":
        s, e = _week_range()
        label = f"Неделя ({s:%d.%m} – {(e - timedelta(seconds=1)):%d.%m})"
    elif scope == "month":
        s, e = _month_range()
        label = f"Месяц ({s:%B %Y})"
    else:
        return jsonify({"error": "bad scope"}), 400
    counts = _counts_in_period(s, e)
    return jsonify({
        "label": label,
        "stages": [
            {"code": st.code, "title": st.title, "short": st.short, "count": counts.get(st.code, 0)}
            for st in FUNNEL
        ],
    })


def _client_metrics(session, lead_ids: list[int]) -> dict[int, dict]:
    """
    Возвращает {lead_id: {revenue, first_paid_at, cycle_days}}.
    Цикл сделки = days между Lead.created_at и первой оплатой (first или repeat).
    """
    if not lead_ids:
        return {}
    out = {lid: {"revenue": 0.0, "first_paid_at": None, "cycle_days": None} for lid in lead_ids}

    # Map lead_id → all client_ids привязанных к нему
    client_ids_by_lead: dict[int, list[int]] = {lid: [] for lid in lead_ids}
    for cid, lid in session.execute(
        select(Client.id, Client.lead_id).where(Client.lead_id.in_(lead_ids))
    ).all():
        client_ids_by_lead.setdefault(lid, []).append(cid)

    # Берём все релевантные платежи: либо привязанные к лиду напрямую, либо к клиенту лида.
    # ВАЖНО: чтобы не считать дубли, через клиента берём только те, у кого lead_id IS NULL.
    direct_rows = session.execute(
        select(Payment.lead_id, Payment.amount, Payment.paid_at)
        .where(Payment.lead_id.in_(lead_ids))
        .where(Payment.payment_type.in_(["first", "repeat"]))
    ).all()
    for lid, amount, paid_at in direct_rows:
        rec = out[lid]
        rec["revenue"] += float(amount or 0)
        if paid_at and (rec["first_paid_at"] is None or paid_at < rec["first_paid_at"]):
            rec["first_paid_at"] = paid_at

    all_client_ids = [c for cids in client_ids_by_lead.values() for c in cids]
    if all_client_ids:
        via_client = session.execute(
            select(Payment.client_id, Payment.amount, Payment.paid_at)
            .where(Payment.client_id.in_(all_client_ids))
            .where(Payment.lead_id.is_(None))
            .where(Payment.payment_type.in_(["first", "repeat"]))
        ).all()
        client_to_lead = {c: lid for lid, cids in client_ids_by_lead.items() for c in cids}
        for cid, amount, paid_at in via_client:
            lid = client_to_lead.get(cid)
            if lid is None:
                continue
            rec = out[lid]
            rec["revenue"] += float(amount or 0)
            if paid_at and (rec["first_paid_at"] is None or paid_at < rec["first_paid_at"]):
                rec["first_paid_at"] = paid_at

    # Считаем цикл сделки: дни между created_at лида и первой оплатой
    leads_data = session.execute(
        select(Lead.id, Lead.created_at).where(Lead.id.in_(lead_ids))
    ).all()
    for lid, created_at in leads_data:
        first_paid = out[lid]["first_paid_at"]
        if first_paid and created_at:
            delta = (first_paid - created_at).total_seconds() / 86400
            out[lid]["cycle_days"] = max(0, round(delta, 1))

    return out


@app.get("/api/leads")
def api_leads():
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or "active"
    with get_session() as session:
        stmt = select(Lead)
        if status == "active":
            stmt = stmt.where(Lead.stage.in_(ACTIVE_CODES))
        elif status == "clients":
            stmt = stmt.where(Lead.stage.in_(CLIENT_CODES))
        elif status == "lost":
            stmt = stmt.where(Lead.stage == LOST)
        elif status == "won":
            stmt = stmt.where(Lead.stage == PACKAGE_BOUGHT)
        elif status == "ignoring":
            stmt = stmt.where(Lead.stage.in_(IGNORING_CODES))
        # status == 'all' — no filter
        if q:
            pat = f"%{q}%"
            stmt = stmt.where(or_(Lead.name.ilike(pat), Lead.username.ilike(pat), Lead.request.ilike(pat)))
        stmt = stmt.order_by(Lead.updated_at.desc())
        rows = session.execute(stmt).scalars().all()

        # Для клиентов — выручка и цикл сделки + общая агрегация
        metrics: dict[int, dict] = {}
        agg = None
        if status == "clients" and rows:
            metrics = _client_metrics(session, [l.id for l in rows])
            cycles = [m["cycle_days"] for m in metrics.values() if m["cycle_days"] is not None]
            total_rev = sum(m["revenue"] for m in metrics.values())
            avg_cycle = (sum(cycles) / len(cycles)) if cycles else None
            agg = {
                "count": len(rows),
                "revenue": total_rev,
                "avg_cycle_days": round(avg_cycle, 1) if avg_cycle is not None else None,
            }

    leads_out = []
    for l in rows:
        d = _lead_dict(l)
        m = metrics.get(l.id)
        if m:
            d["revenue"] = m["revenue"]
            d["cycle_days"] = m["cycle_days"]
        leads_out.append(d)
    return jsonify({"leads": leads_out, "clients_summary": agg})


@app.get("/api/stages")
def api_stages():
    return jsonify({
        "funnel": [{"code": s.code, "title": s.title, "short": s.short} for s in FUNNEL],
        "lost": {"code": LOST_STAGE.code, "title": LOST_STAGE.title, "short": LOST_STAGE.short},
        "ignoring": {"code": IGNORING_STAGE.code, "title": IGNORING_STAGE.title, "short": IGNORING_STAGE.short},
        "sources": [{"code": c, "title": t} for c, t in SOURCES],
    })


@app.post("/api/leads/<int:lead_id>/advance")
def api_advance(lead_id: int):
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "not found"}), 404
        nxt = next_stage(lead.stage)
        if not nxt:
            return jsonify({"error": "already final"}), 400
        lead.stage = nxt.code
        session.add(StageHistory(lead_id=lead.id, stage=nxt.code))
        session.commit()
        session.refresh(lead)
    _try_sheets_sync(lead_id)
    return jsonify({"lead": _lead_dict(lead)})


@app.post("/api/leads/<int:lead_id>/lost")
def api_lost(lead_id: int):
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip() or None
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "not found"}), 404
        lead.stage = LOST
        lead.lost_reason = reason
        session.add(StageHistory(lead_id=lead.id, stage=LOST))
        session.commit()
        session.refresh(lead)
    _try_sheets_sync(lead_id)
    return jsonify({"lead": _lead_dict(lead)})


@app.post("/api/leads/<int:lead_id>/note")
def api_note(lead_id: int):
    body = request.get_json(silent=True) or {}
    note = (body.get("notes") or "").strip()
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "not found"}), 404
        lead.notes = note or None
        session.commit()
        session.refresh(lead)
    _try_sheets_sync(lead_id)
    return jsonify({"lead": _lead_dict(lead)})


@app.delete("/api/leads/<int:lead_id>")
def api_delete(lead_id: int):
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "not found"}), 404
        session.delete(lead)
        session.commit()
    return jsonify({"ok": True, "id": lead_id})


@app.post("/api/leads/<int:lead_id>/revive")
def api_revive(lead_id: int):
    """Move a lost lead back to qualified."""
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "not found"}), 404
        lead.stage = "qualified"
        lead.lost_reason = None
        session.add(StageHistory(lead_id=lead.id, stage="qualified"))
        session.commit()
        session.refresh(lead)
    _try_sheets_sync(lead_id)
    return jsonify({"lead": _lead_dict(lead)})


@app.post("/api/leads/<int:lead_id>/ignore")
def api_ignore(lead_id: int):
    """Mark an active lead as ignoring."""
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "not found"}), 404
        if lead.stage == IGNORING:
            return jsonify({"lead": _lead_dict(lead)})
        lead.stage = IGNORING
        session.add(StageHistory(lead_id=lead.id, stage=IGNORING))
        session.commit()
        session.refresh(lead)
    _try_sheets_sync(lead_id)
    return jsonify({"lead": _lead_dict(lead)})


@app.post("/api/leads/<int:lead_id>/unignore")
def api_unignore(lead_id: int):
    """Return ignoring lead to its previous stage (or lead_new)."""
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "not found"}), 404
        if lead.stage != IGNORING:
            return jsonify({"lead": _lead_dict(lead)})
        prev = session.execute(
            select(StageHistory)
            .where(StageHistory.lead_id == lead_id)
            .where(StageHistory.stage != IGNORING)
            .order_by(StageHistory.changed_at.desc())
            .limit(1)
        ).scalars().first()
        target = prev.stage if prev else LEAD_NEW
        lead.stage = target
        session.add(StageHistory(lead_id=lead.id, stage=target))
        session.commit()
        session.refresh(lead)
    _try_sheets_sync(lead_id)
    return jsonify({"lead": _lead_dict(lead)})


def _create_lead_from_payload(body: dict):
    """Создаёт Lead + начальную запись в StageHistory. Возвращает (lead_dict, error)."""
    source = (body.get("source") or "").strip()
    if source not in SOURCE_TITLES:
        return None, ("bad source", 400)
    name = (body.get("name") or "").strip() or None
    username = (body.get("username") or "").strip() or None
    req = (body.get("request") or "").strip() or None

    with get_session() as session:
        lead = Lead(
            name=name,
            username=username,
            source=source,
            request=req,
            stage=LEAD_NEW,
        )
        session.add(lead)
        session.flush()
        session.add(StageHistory(lead_id=lead.id, stage=LEAD_NEW))
        session.commit()
        session.refresh(lead)
        result = _lead_dict(lead)
        lead_id = lead.id
    _try_sheets_sync(lead_id)
    return result, None


@app.post("/api/leads")
def api_create():
    body = request.get_json(silent=True) or {}
    lead, err = _create_lead_from_payload(body)
    if err:
        return jsonify({"error": err[0]}), err[1]
    return jsonify({"lead": lead})


@app.post("/api/external/leads")
def api_external_create():
    """
    Защищённый эндпоинт для внешних ботов (например, отдельный лид-бот).
    Принимает то же тело, что и /api/leads, но требует заголовок X-API-Key.
    Если LEADS_API_KEY не задан в env — эндпоинт отключён (403).

    После успешного создания — пушит в Telegram владельцу карточку лида
    с пометкой «Автоматически из лид-бота» и кнопками управления. Ошибка
    отправки не валит ответ — лид всё равно сохранён.
    """
    if not LEADS_API_KEY:
        return jsonify({"error": "external api disabled"}), 403
    provided = request.headers.get("X-API-Key", "").strip()
    if not provided or not hmac.compare_digest(LEADS_API_KEY, provided):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    # Дефолтный source для лид-бота — telegram, если не передали ничего валидного
    if not body.get("source") or body.get("source") not in SOURCE_TITLES:
        body["source"] = "telegram"
    lead, err = _create_lead_from_payload(body)
    if err:
        return jsonify({"error": err[0]}), err[1]

    # Push владельцу — после сохранения, перед возвратом ответа
    try:
        from tg_notify import notify_external_lead
        notify_external_lead(lead)
    except Exception as e:
        log.warning("external lead notify failed: %s", e)

    return jsonify({"ok": True, "lead": lead})


def _try_sheets_sync(lead_id: int) -> None:
    try:
        from sheets import sync_lead
        sync_lead(lead_id)
    except Exception as e:
        log.warning("sheets sync skipped: %s", e)


if __name__ == "__main__":
    # 0.0.0.0 — доступ с телефона в той же Wi-Fi сети по IP Mac.
    app.run(host="0.0.0.0", port=8765, debug=False)
