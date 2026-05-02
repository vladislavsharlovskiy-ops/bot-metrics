from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from config import DB_PATH
from models import Base

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


def get_session() -> Session:
    return SessionLocal()
