from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GeneralToleranceRule(Base):
    """'Teach once' general-tolerance rule: applies to matching open dimensions in its scope."""

    __tablename__ = "general_tolerance_rules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True, default="default")
    scope: Mapped[str] = mapped_column(String(16), default="drawing")        # drawing | family | customer
    scope_key: Mapped[str] = mapped_column(String(255), index=True, default="")  # drawing key / family key / org id
    process: Mapped[str | None] = mapped_column(String(32), nullable=True)
    category: Mapped[str] = mapped_column(String(32))
    hole_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    size_min: Mapped[float | None] = mapped_column(Float, nullable=True)      # exclusive
    size_max: Mapped[float | None] = mapped_column(Float, nullable=True)      # inclusive
    unit: Mapped[str] = mapped_column(String(8), default="mm")                # mm | deg | %
    upper: Mapped[float] = mapped_column(Float)
    lower: Mapped[float] = mapped_column(Float)
    name: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class GeneralToleranceRuleAudit(Base):
    """Every create / update / disable / delete of a rule (before and after values)."""

    __tablename__ = "general_tolerance_rule_audit"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(16))                           # create | update | disable | enable | delete
    changed_by: Mapped[str] = mapped_column(String(120), default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    before_json: Mapped[str] = mapped_column(Text, default="null")
    after_json: Mapped[str] = mapped_column(Text, default="null")
