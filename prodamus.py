"""
Утилиты для работы с Prodamus webhook'ами.

Prodamus присылает POST с form-данными платежа + поле `signature`. Мы должны:
1. Достать все поля КРОМЕ `signature`
2. Отсортировать рекурсивно по ключам
3. JSON-encoded строка (UTF-8, без эскейпинга юникода) → HMAC-SHA256 → hex
4. Сравнить с тем, что прислал Prodamus
"""

from __future__ import annotations

import hmac
import hashlib
import json
import os
from typing import Any, Dict


PRODAMUS_SECRET = os.environ.get("PRODAMUS_SECRET_KEY", "")


def _sort_recursive(obj: Any) -> Any:
    """Рекурсивно сортирует dict-ключи, как делает PHP-эталон Prodamus."""
    if isinstance(obj, dict):
        return {k: _sort_recursive(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_sort_recursive(x) for x in obj]
    return obj


def _build_signature(data: Dict[str, Any], secret: str) -> str:
    sorted_data = _sort_recursive(data)
    # PHP json_encode по умолчанию экранирует / в \/. ensure_ascii=False сохраняет UTF-8.
    payload = json.dumps(sorted_data, ensure_ascii=False, separators=(",", ":")).replace("/", "\\/")
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(data: Dict[str, Any], provided_signature: str, secret: str | None = None) -> bool:
    """Проверка подписи incoming webhook от Prodamus."""
    secret = secret or PRODAMUS_SECRET
    if not secret or not provided_signature:
        return False
    expected = _build_signature(data, secret)
    return hmac.compare_digest(expected.lower(), provided_signature.lower())


def parse_form_to_dict(form: Dict[str, str]) -> Dict[str, Any]:
    """
    Prodamus присылает hierarchy в плоских ключах: 'products[0][name]=X'.
    Парсим обратно в nested dict.
    """
    out: Dict[str, Any] = {}
    for raw_key, value in form.items():
        # парсим 'a[b][c]' → ['a', 'b', 'c']
        if "[" not in raw_key:
            out[raw_key] = value
            continue
        parts: list[str] = []
        head, _, rest = raw_key.partition("[")
        parts.append(head)
        # rest = 'b][c]'
        for chunk in rest.split("["):
            parts.append(chunk.rstrip("]"))
        # теперь записываем по пути
        cur = out
        for p in parts[:-1]:
            key = int(p) if p.isdigit() else p
            if isinstance(cur, list):
                while len(cur) <= key:
                    cur.append({})
                if not isinstance(cur[key], (dict, list)):
                    cur[key] = {}
                cur = cur[key]
            else:
                if p not in cur or not isinstance(cur[p], (dict, list)):
                    # peek next part — если число, делаем list, иначе dict
                    next_idx = parts[parts.index(p) + 1]
                    cur[p] = [] if next_idx.isdigit() else {}
                cur = cur[p]
        last = parts[-1]
        key = int(last) if last.isdigit() else last
        if isinstance(cur, list):
            while len(cur) <= key:
                cur.append(None)
            cur[key] = value
        else:
            cur[last] = value
    return out


def extract_payment(data: Dict[str, Any]) -> Dict[str, Any]:
    """Достаём из webhook поля, которые нас интересуют."""
    products = data.get("products") or []
    if isinstance(products, dict):
        products = list(products.values())
    product_names = []
    for p in products:
        if isinstance(p, dict) and p.get("name"):
            product_names.append(str(p["name"]))
    product_str = " · ".join(product_names) if product_names else (data.get("order_num") or "")

    # Prodamus в демо-форме пишет имя клиента в order_num. Используем как fallback,
    # но только если значение не выглядит как чисто цифровой номер заказа.
    order_num = str(data.get("order_num") or "").strip()
    order_num_as_name = order_num if order_num and not order_num.isdigit() else ""
    name = (
        data.get("customer_name")
        or data.get("name")
        or data.get("client_name")
        or data.get("payer_name")
        or order_num_as_name
        or ""
    )
    phone = (
        data.get("customer_phone")
        or data.get("phone")
        or data.get("client_phone")
        or ""
    )
    email = (
        data.get("customer_email")
        or data.get("email")
        or data.get("client_email")
        or ""
    )
    return {
        "prodamus_id": str(data.get("order_id") or data.get("order_num") or ""),
        "amount": float(data.get("sum") or 0),
        "currency": str(data.get("currency") or "rub").upper(),
        "paid_at": data.get("date") or "",
        "customer_name": (name or "").strip() or None,
        "customer_phone": _normalize_phone(phone),
        "customer_email": (email or "").strip() or None,
        "product": product_str.strip() or None,
        "payment_status": str(data.get("payment_status") or "").lower(),
    }


def _normalize_phone(raw: Any) -> str | None:
    if not raw:
        return None
    digits = "".join(c for c in str(raw) if c.isdigit())
    if not digits:
        return None
    # 8 → 7
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits if not digits.startswith("+") else digits
