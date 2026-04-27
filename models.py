from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now()


# ───────── Funnel #1: входящие лиды ─────────

class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(200))
    username: Mapped[Optional[str]] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(40))
    request: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(String(40), index=True)
    lost_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    history: Mapped[List["StageHistory"]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
        order_by="StageHistory.changed_at",
    )


class StageHistory(Base):
    __tablename__ = "stage_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(40), index=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)

    lead: Mapped[Lead] = relationship(back_populates="history")


# ───────── Клиенты, платежи, повторная воронка ─────────

class Client(Base):
    """Человек, который заплатил хотя бы один раз. Идентификатор — телефон."""
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(200))
    phone: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    email: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    lead_id: Mapped[Optional[int]] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    first_payment_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_payment_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    payments: Mapped[List["Payment"]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
        order_by="Payment.paid_at",
    )
    sessions: Mapped[List["RepeatSession"]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
        order_by="RepeatSession.created_at",
    )


class Payment(Base):
    """Каждый платёж из Prodamus."""
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prodamus_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    paid_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    customer_name: Mapped[Optional[str]] = mapped_column(String(200))
    customer_phone: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    customer_email: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    product: Mapped[Optional[str]] = mapped_column(String(400))

    # 'first' = первичка, 'repeat' = повторка, 'unclassified' = ждёт ручного выбора
    payment_type: Mapped[str] = mapped_column(String(20), default="unclassified", index=True)
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"))
    lead_id: Mapped[Optional[int]] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))

    raw_json: Mapped[Optional[str]] = mapped_column(Text)  # сырой ответ Prodamus для отладки
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    client: Mapped[Optional[Client]] = relationship(back_populates="payments")


class RepeatSession(Base):
    """Цикл повторной сессии: запрос → согласовано → оплата → проведена."""
    __tablename__ = "repeat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(40), index=True)  # см. stages.REPEAT_FUNNEL
    payment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    client: Mapped[Client] = relationship(back_populates="sessions")
