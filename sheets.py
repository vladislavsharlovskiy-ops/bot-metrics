"""
Push lead changes to Google Sheets via an Apps Script Web App webhook.
If SHEETS_WEBHOOK_URL is empty, all sync calls are no-ops — the bot still works.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from sqlalchemy import select

from db import get_session
from models import Lead
from stages import BY_CODE, SOURCE_TITLES

load_dotenv()

WEBHOOK_URL = (os.environ.get("SHEETS_WEBHOOK_URL") or "").strip()
log = logging.getLogger("sheets")


def _lead_to_dict(lead: Lead) -> Dict[str, Any]:
    stage = BY_CODE.get(lead.stage)
    return {
        "id": lead.id,
        "created_at": lead.created_at.isoformat(timespec="seconds"),
        "updated_at": lead.updated_at.isoformat(timespec="seconds"),
        "source": lead.source,
        "source_title": SOURCE_TITLES.get(lead.source, lead.source),
        "name": lead.name or "",
        "username": lead.username or "",
        "request": lead.request or "",
        "notes": lead.notes or "",
        "stage": lead.stage,
        "stage_title": stage.title if stage else lead.stage,
        "lost_reason": lead.lost_reason or "",
    }


def _post(payload: Dict[str, Any]) -> None:
    if not WEBHOOK_URL:
        return
    try:
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("Sheets sync failed: %s", e)
    except Exception as e:
        log.warning("Sheets sync error: %s", e)


def sync_lead(lead_id: int) -> None:
    """Fire-and-forget upsert of a single lead."""
    if not WEBHOOK_URL:
        return
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return
        payload = {"action": "upsert", "lead": _lead_to_dict(lead)}
    threading.Thread(target=_post, args=(payload,), daemon=True).start()


def sync_all() -> int:
    """Push every lead to the sheet. Used by /sync. Returns row count."""
    if not WEBHOOK_URL:
        return 0
    with get_session() as session:
        rows = session.execute(select(Lead).order_by(Lead.id)).scalars().all()
        leads: List[Dict[str, Any]] = [_lead_to_dict(l) for l in rows]
    _post({"action": "replace_all", "leads": leads})
    return len(leads)


def is_enabled() -> bool:
    return bool(WEBHOOK_URL)
