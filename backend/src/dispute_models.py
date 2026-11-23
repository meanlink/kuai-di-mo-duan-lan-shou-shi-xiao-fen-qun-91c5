from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column as col, relationship

from .db import Base
from .models import PickupBatch, utcnow


class DuplicateDispute(Base):
    """离线恢复同步后，两个网点报来同一面单时留待主管裁定的记录。

    首报批次的面单照常入账；晚到批次的面单数据快照在本表，裁定前不进入
    pickup_scans。裁定为改判时，扫描在同一事务内从原批次转记到晚到批次。
    """

    __tablename__ = 'duplicate_disputes'
    __table_args__ = (
        UniqueConstraint('parcel_code', 'later_batch_code'),
        CheckConstraint('first_station_code <> later_station_code'),
        CheckConstraint('first_elapsed_minutes BETWEEN 0 AND 1440'),
        CheckConstraint('later_elapsed_minutes BETWEEN 0 AND 1440'),
    )
    id: Mapped[int] = col(primary_key=True)
    parcel_code: Mapped[str] = col(String(36), index=True)
    first_batch_code: Mapped[str] = col(ForeignKey('pickup_batches.code'))
    first_station_code: Mapped[str] = col(String(16))
    first_planned_at: Mapped[datetime] = col(DateTime, nullable=False)
    first_scanned_at: Mapped[datetime] = col(DateTime, nullable=False)
    first_elapsed_minutes: Mapped[int] = col(Integer)
    later_batch_code: Mapped[str] = col(ForeignKey('pickup_batches.code'))
    later_station_code: Mapped[str] = col(String(16))
    later_planned_at: Mapped[datetime] = col(DateTime, nullable=False)
    later_scanned_at: Mapped[datetime] = col(DateTime, nullable=False)
    later_elapsed_minutes: Mapped[int] = col(Integer)
    status: Mapped[str] = col(Enum('pending', 'upheld', 'reassigned'), default='pending', index=True)
    decision_note: Mapped[str | None] = col(String(400))
    decided_by: Mapped[str | None] = col(ForeignKey('users.username'))
    decided_at: Mapped[datetime | None] = col(DateTime)
    created_at: Mapped[datetime] = col(DateTime, default=utcnow)
    first_batch: Mapped[PickupBatch] = relationship(foreign_keys=[first_batch_code])
    later_batch: Mapped[PickupBatch] = relationship(foreign_keys=[later_batch_code])
