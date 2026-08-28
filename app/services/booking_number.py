"""
backend/app/services/booking_number.py

Generates unique, human-readable booking reference numbers in the
format `BOOK-YYYY-000001`.

Concurrency-safety:
    Numbers are drawn from a dedicated PostgreSQL sequence
    (`bookings_booking_number_seq`, created in migration
    `20260813_0002_add_booking_number_seq`) via `nextval()`, which
    PostgreSQL guarantees is atomic and race-free across concurrent
    transactions/connections. This avoids the classic
    `SELECT count(*) + 1` race, where two concurrent inserts can
    compute the same "next" number.

    The sequence itself is monotonically increasing and is NOT reset
    per calendar year; the year segment of the formatted string simply
    reflects the current year at generation time. This keeps number
    generation a single atomic DB round-trip with no read-then-write
    gap. If a hard per-year reset (000001 every January) is required
    later, that needs a different, lock-based strategy and should be a
    deliberate follow-up rather than bundled into this fix.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SEQUENCE_NAME = "bookings_booking_number_seq"


async def generate_booking_number(db: AsyncSession) -> str:
    """
    Atomically draw the next value from `bookings_booking_number_seq`
    and format it as a booking number.

    Args:
        db: An active SQLAlchemy AsyncSession — must be the same
            session/transaction the booking row will be inserted in,
            so the sequence draw and the insert share atomicity
            guarantees with the rest of the unit of work.

    Returns:
        A booking number string, e.g. "BOOK-2026-000001".
    """
    result = await db.execute(text(f"SELECT nextval('{_SEQUENCE_NAME}')"))
    next_val = result.scalar_one()
    year = date.today().year
    return f"BOOK-{year}-{next_val:06d}"


__all__ = ["generate_booking_number"]