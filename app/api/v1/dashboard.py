from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.dashboard import (
    AgentPerformance,
    BookingSummary,
    CustomerSummary,
    DashboardResponse,
    DashboardSummary,
    LeadSummary,
    MonthlyTrend,
    PropertySummary,
    RecentActivity,
    RevenueSummary,
    WeeklyTrend,
)
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


def get_dashboard_service(db: AsyncSession = Depends(get_db)) -> DashboardService:
    return DashboardService(db)


@router.get(
    "",
    response_model=DashboardResponse,
    status_code=status.HTTP_200_OK,
    summary="Get the full aggregated dashboard",
    description="Returns the complete read-only dashboard: summary, top agents, "
    "recent activities, and charts for revenue, bookings, leads, properties and payments.",
)
async def get_dashboard(
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> DashboardResponse:
    return await service.get_full_dashboard(current_user)


@router.get(
    "/summary",
    response_model=DashboardSummary,
    status_code=status.HTTP_200_OK,
    summary="Get the dashboard summary",
    description="Returns aggregated revenue, lead, booking, property and customer summaries.",
)
async def get_dashboard_summary(
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> DashboardSummary:
    return await service.get_dashboard_summary(current_user)


@router.get(
    "/revenue",
    response_model=RevenueSummary,
    status_code=status.HTTP_200_OK,
    summary="Get revenue overview",
    description="Returns total, collected, pending and refunded revenue figures.",
)
async def get_revenue_overview(
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> RevenueSummary:
    return await service.get_revenue_overview(current_user)


@router.get(
    "/revenue/monthly",
    response_model=List[MonthlyTrend],
    status_code=status.HTTP_200_OK,
    summary="Get monthly revenue trend",
    description="Returns collected revenue grouped by month for the requested lookback window.",
)
async def get_monthly_revenue(
    months: int = Query(default=12, ge=1, le=36, description="Number of months to include."),
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> List[MonthlyTrend]:
    return await service.get_monthly_revenue_trend(current_user, months=months)


@router.get(
    "/revenue/weekly",
    response_model=List[WeeklyTrend],
    status_code=status.HTTP_200_OK,
    summary="Get weekly revenue trend",
    description="Returns collected revenue grouped by ISO week for the requested lookback window.",
)
async def get_weekly_revenue(
    weeks: int = Query(default=8, ge=1, le=52, description="Number of weeks to include."),
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> List[WeeklyTrend]:
    return await service.get_weekly_revenue_trend(current_user, weeks=weeks)


@router.get(
    "/revenue/daily",
    response_model=Dict[str, Any],
    status_code=status.HTTP_200_OK,
    summary="Get daily revenue overview and derived metrics",
    description="Returns today's and yesterday's revenue, week-to-date and month-to-date "
    "revenue, daily/weekly/monthly growth percentages, booking conversion rate, "
    "property availability rate and outstanding revenue.",
)
async def get_daily_revenue(
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> Dict[str, Any]:
    return await service.get_daily_revenue_overview(current_user)


@router.get(
    "/leads",
    response_model=LeadSummary,
    status_code=status.HTTP_200_OK,
    summary="Get lead statistics",
    description="Returns lead counts by status and the overall lead conversion rate.",
)
async def get_lead_summary(
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> LeadSummary:
    return await service.get_lead_overview(current_user)


@router.get(
    "/bookings",
    response_model=BookingSummary,
    status_code=status.HTTP_200_OK,
    summary="Get booking statistics",
    description="Returns booking counts by status and total booking value.",
)
async def get_booking_summary(
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> BookingSummary:
    return await service.get_booking_overview(current_user)


@router.get(
    "/payments",
    response_model=RevenueSummary,
    status_code=status.HTTP_200_OK,
    summary="Get payment statistics",
    description="Returns collected, pending and refunded payment totals with transaction counts.",
)
async def get_payment_summary(
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> RevenueSummary:
    return await service.get_payment_overview(current_user)


@router.get(
    "/properties",
    response_model=PropertySummary,
    status_code=status.HTTP_200_OK,
    summary="Get property statistics",
    description="Returns property counts by status and average property price.",
)
async def get_property_summary(
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> PropertySummary:
    return await service.get_property_overview(current_user)


@router.get(
    "/customers",
    response_model=CustomerSummary,
    status_code=status.HTTP_200_OK,
    summary="Get customer statistics",
    description="Returns total, new-this-month and distinct-city customer counts.",
)
async def get_customer_summary(
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> CustomerSummary:
    return await service.get_customer_overview(current_user)


@router.get(
    "/agents",
    response_model=List[AgentPerformance],
    status_code=status.HTTP_200_OK,
    summary="Get agent performance list",
    description="Returns paginated agent performance metrics. Sales agents only see their own record.",
)
async def get_agents_performance(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> List[AgentPerformance]:
    return await service.get_agents_performance(current_user, limit=limit, offset=offset)


@router.get(
    "/recent-activities",
    response_model=List[RecentActivity],
    status_code=status.HTTP_200_OK,
    summary="Get recent activities",
    description="Returns the most recent customer, lead, booking and payment activities merged "
    "and sorted by recency.",
)
async def get_recent_activities(
    limit: int = Query(default=15, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> List[RecentActivity]:
    return await service.get_recent_activities(current_user, limit=limit)


@router.get(
    "/top-agents",
    response_model=List[AgentPerformance],
    status_code=status.HTTP_200_OK,
    summary="Get top performing agents",
    description="Returns the top agents ranked by collected revenue. Sales agents only see "
    "their own record.",
)
async def get_top_agents(
    limit: int = Query(default=5, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> List[AgentPerformance]:
    return await service.get_top_agents(current_user, limit=limit)


@router.get(
    "/top-properties",
    response_model=List[Dict[str, Any]],
    status_code=status.HTTP_200_OK,
    summary="Get top performing properties",
    description="Returns the top properties ranked by collected revenue.",
)
async def get_top_properties(
    limit: int = Query(default=5, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> List[Dict[str, Any]]:
    return await service.get_top_properties(current_user, limit=limit)


@router.get(
    "/top-cities",
    response_model=List[Dict[str, Any]],
    status_code=status.HTTP_200_OK,
    summary="Get top cities",
    description="Returns the top cities ranked by number of bookings.",
)
async def get_top_cities(
    limit: int = Query(default=5, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> List[Dict[str, Any]]:
    return await service.get_top_cities(current_user, limit=limit)


@router.get(
    "/upcoming-followups",
    response_model=List[Dict[str, Any]],
    status_code=status.HTTP_200_OK,
    summary="Get upcoming lead follow-ups",
    description="Returns leads with an upcoming follow-up date that are not yet converted or lost. "
    "Sales agents only see their own assigned leads.",
)
async def get_upcoming_followups(
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> List[Dict[str, Any]]:
    return await service.get_upcoming_followups(current_user, limit=limit, offset=offset)