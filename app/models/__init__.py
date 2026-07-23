"""
Models package.

This package is the single home for every SQLAlchemy ORM model in
the application (e.g. `User`, `Customer`, `Lead`, `Property`,
`Booking`, `Payment`, etc. -- introduced in later phases).

Why this file exists (even though it currently declares no models):
  - `app.db.base.Base` is re-exported here so that every model
    module can consistently do `from app.models import Base` instead
    of reaching back into `app.db.base` directly, keeping model
    imports self-contained within this package.
  - As each model is added in a future phase, it MUST be imported
    here so that its table is registered on `Base.metadata`. This is
    what allows Alembic's autogenerate to discover the full schema:
    Alembic only "sees" models that have actually been imported
    somewhere before it inspects `Base.metadata`.
  - Centralizing these imports in one place avoids scattered,
    inconsistent import paths across the codebase and prevents
    circular-import issues between model modules that reference
    one another via relationships/foreign keys.

Usage (once models exist, added in a future phase):

    # app/models/__init__.py
    from app.db.base import Base

    from app.models.user import User
    from app.models.customer import Customer
    from app.models.lead import Lead
    from app.models.property import Property
    from app.models.booking import Booking
    from app.models.payment import Payment

    __all__ = [
        "Base",
        "User",
        "Customer",
        "Lead",
        "Property",
        "Booking",
        "Payment",
    ]

NOTE (scope of this phase):
    No model classes are declared or imported yet. This file only
    establishes the package structure and re-exports `Base` so that
    future model modules have a single, stable import target.
"""

from app.db.base import Base

# Public API of this package. Extended in future phases as model
# modules are added (e.g. "User", "Customer", "Lead", ...).
__all__ = [
    "Base",
]