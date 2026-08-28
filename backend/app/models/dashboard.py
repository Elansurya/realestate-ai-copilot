import enum


class ActivityType(str, enum.Enum):
    CUSTOMER_CREATED = "customer_created"
    LEAD_CREATED = "lead_created"
    LEAD_CONVERTED = "lead_converted"
    LEAD_LOST = "lead_lost"
    PROPERTY_LISTED = "property_listed"
    PROPERTY_SOLD = "property_sold"
    BOOKING_CREATED = "booking_created"
    BOOKING_CONFIRMED = "booking_confirmed"
    BOOKING_CANCELLED = "booking_cancelled"
    PAYMENT_RECEIVED = "payment_received"
    PAYMENT_PENDING = "payment_pending"


class TrendPeriod(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class LeadStatusEnum(str, enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    NEGOTIATION = "negotiation"
    CONVERTED = "converted"
    LOST = "lost"


class BookingStatusEnum(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class PaymentStatusEnum(str, enum.Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


class PropertyStatusEnum(str, enum.Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    SOLD = "sold"
    RENTED = "rented"
    INACTIVE = "inactive"