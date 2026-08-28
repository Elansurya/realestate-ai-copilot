"""
Report Module - Models
Enterprise Real Estate AI Copilot CRM

This module is strictly READ-ONLY.
It owns no persisted tables and performs no INSERT, UPDATE
or DELETE operations. It exposes only typed enumerations
used consistently across the Report schemas and repository
layer to classify report types, periods, export formats and
the domain status values read from Customer, Lead, Property,
Booking and Payment tables.
"""

from enum import Enum


class ReportType(str, Enum):
    REVENUE = "revenue"
    CUSTOMER = "customer"
    LEAD = "lead"
    PROPERTY = "property"
    BOOKING = "booking"
    BOOKINGS = "booking"  # compatibility alias used by API/export callers
    PAYMENT = "payment"
    PAYMENTS = "payment"  # compatibility alias
    LEADS = "lead"  # compatibility alias
    AGENT_PERFORMANCE = "agent_performance"
    BUSINESS_SUMMARY = "business_summary"


class ReportPeriod(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"
    CUSTOM = "custom"


class ExportFormat(str, Enum):
    PDF = "pdf"
    EXCEL = "excel"
    CSV = "csv"
    JSON = "json"


class ReportLeadSource(str, Enum):
    WEBSITE = "website"
    REFERRAL = "referral"
    SOCIAL_MEDIA = "social_media"
    WALK_IN = "walk_in"
    PHONE = "phone"
    EMAIL = "email"
    OTHER = "other"


class ReportLeadStatus(str, Enum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    CONVERTED = "converted"
    LOST = "lost"


class ReportBookingStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class ReportPaymentStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


class ReportPropertyStatus(str, Enum):
    AVAILABLE = "available"
    SOLD = "sold"
    RENTED = "rented"
    PENDING = "pending"