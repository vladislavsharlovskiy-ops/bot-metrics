import logging

from sqlalchemy import create_engine, delete as sql_delete, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from config import DB_PATH
from models import Base, Client, Payment

log = logging.getLogger("db")

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """
    Прагмы для безопасной параллельной работы нескольких ботов через
    одну SQLite-базу на общем томе (/app/shared/bot.db на bothost).

    - journal_mode=WAL — несколько читателей и один писатель работают
      одновременно, без полной блокировки файла. Сохраняется в самом
      файле БД, ставим при каждом коннекте на всякий случай.
    - busy_timeout=5000 — если другой процесс держит блокировку,
      ждём до 5 сек вместо мгновенной ошибки «database is locked».
    - synchronous=NORMAL — баланс между скоростью и сохранностью;
      безопасно для WAL.
    - foreign_keys=ON — SQLite по умолчанию игнорирует FK; включаем,
      чтобы каскадные удаления и связи работали как описано в моделях.
    """
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    _run_migrations()
    _purge_orphans_on_startup()


def _purge_orphans_on_startup() -> None:
    """Идемпотентная чистка «осиротевших» Clients/Payments на каждом старте.

    До cascade-delete фикса (lead_ops.delete_lead_cascade) удаление лида
    оставляло в БД связанные Client (с lead_id=NULL через FK SET NULL)
    и Payment (с lead_id=NULL). Дашборд продолжал учитывать такие
    записи в карточке «Первичных оплат», расходясь с нижним блоком
    «Клиенты». После фикса новые удаления уже чистят всё, но накопленный
    мусор остаётся — поэтому чистим его при каждом старте.

    Идемпотентно: если осиротевших нет — это no-op. Платежи в состоянии
    'unclassified' не трогаем, это нормальные свежие webhook'и.
    """
    with SessionLocal() as session:
        orphan_clients = session.execute(
            select(Client).where(Client.lead_id.is_(None))
        ).scalars().all()
        for c in orphan_clients:
            session.delete(c)

        result = session.execute(
            sql_delete(Payment)
            .where(Payment.lead_id.is_(None))
            .where(Payment.client_id.is_(None))
            .where(Payment.payment_type.in_(("first", "repeat")))
        )
        bare_payments = result.rowcount or 0

        if orphan_clients or bare_payments:
            session.commit()
            log.warning(
                "purge_orphans_on_startup: removed %d orphan Clients (with cascade payments/sessions) and %d bare orphan Payments",
                len(orphan_clients),
                bare_payments,
            )


def _run_migrations() -> None:
    """Лёгкие in-place миграции для SQLite. Только ADD COLUMN: безопасно, обратимо."""
    insp = inspect(engine)
    existing = {c["name"] for c in insp.get_columns("leads")}
    with engine.begin() as conn:
        if "telegram_user_id" not in existing:
            conn.execute(text("ALTER TABLE leads ADD COLUMN telegram_user_id INTEGER"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_leads_telegram_user_id ON leads(telegram_user_id)"))


def get_session() -> Session:
    return SessionLocal()
