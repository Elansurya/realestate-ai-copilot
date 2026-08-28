"""Pydantic schemas for AI-powered analytics and insight generation."""

import enum
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


class AnalyticsMetric(str, enum.Enum):
    """Enumeration of supported CRM analytics metrics."""

    LEAD_CONVERSION_RATE = "lead_conversion_rate"
    AVERAGE_DEAL_SIZE = "average_deal_size"
    LISTING_TIME_ON_MARKET = "listing_time_on_market"
    AGENT_RESPONSE_TIME = "agent_response_time"
    REVENUE = "revenue"


class TrendDirection(str, enum.Enum):
    """Enumeration of qualitative trend directions for an analyzed metric."""

    UP = "up"
    DOWN = "down"
    FLAT = "flat"


class AnalyticsQueryRequest(BaseModel):
    """Request payload for generating an AI-powered analytics summary.

    Attributes:
        metric: The analytics metric to analyze.
        start_date: Start of the analysis period, inclusive.
        end_date: End of the analysis period, inclusive.
        segment_by: Optional dimension to segment results by (e.g. agent, region).
    """

    model_config = ConfigDict(from_attributes=True)

    metric: AnalyticsMetric
    start_date: date
    end_date: date
    segment_by: Optional[str] = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_date_range(self) -> "AnalyticsQueryRequest":
        """Ensure the analysis period is chronologically valid.

        Returns:
            The validated AnalyticsQueryRequest instance.

        Raises:
            ValueError: If start_date is on or after end_date.
        """
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be strictly before end_date")
        return self


class TrendAnalysisRequest(BaseModel):
    """Request payload for analyzing the trend of a metric across two periods.

    Attributes:
        metric: The analytics metric to analyze.
        current_period_start: Start of the current comparison period.
        current_period_end: End of the current comparison period.
        previous_period_start: Start of the previous comparison period.
        previous_period_end: End of the previous comparison period.
    """

    model_config = ConfigDict(from_attributes=True)

    metric: AnalyticsMetric
    current_period_start: date
    current_period_end: date
    previous_period_start: date
    previous_period_end: date

    @model_validator(mode="after")
    def validate_periods(self) -> "TrendAnalysisRequest":
        """Ensure both comparison periods are chronologically valid and ordered.

        Returns:
            The validated TrendAnalysisRequest instance.

        Raises:
            ValueError: If either period is invalid or the previous period does
                not precede the current period.
        """
        if self.current_period_start >= self.current_period_end:
            raise ValueError(
                "current_period_start must be strictly before current_period_end"
            )
        if self.previous_period_start >= self.previous_period_end:
            raise ValueError(
                "previous_period_start must be strictly before previous_period_end"
            )
        if self.previous_period_end > self.current_period_start:
            raise ValueError("previous_period must end before current_period begins")
        return self


class AnalyticsInsight(BaseModel):
    """A single AI-generated insight derived from analytics data.

    Attributes:
        summary: Short human readable summary of the insight.
        detail: Extended natural language explanation of the insight.
        confidence: Model-reported confidence score for the insight.
    """

    model_config = ConfigDict(from_attributes=True)

    summary: str = Field(min_length=1, max_length=500)
    detail: str
    confidence: float = Field(ge=0.0, le=1.0)


class TrendAnalysisResult(BaseModel):
    """Response payload describing the trend of a metric between two periods.

    Attributes:
        metric: The analytics metric that was analyzed.
        current_value: Aggregated metric value for the current period.
        previous_value: Aggregated metric value for the previous period.
        percent_change: Percentage change from previous to current value.
        direction: Qualitative direction of the observed trend.
    """

    model_config = ConfigDict(from_attributes=True)

    metric: AnalyticsMetric
    current_value: float
    previous_value: float
    percent_change: float
    direction: TrendDirection

    @field_validator("direction")
    @classmethod
    def validate_direction_consistency(
        cls, value: TrendDirection, info: ValidationInfo
    ) -> TrendDirection:
        """Ensure the reported direction is consistent with percent_change sign.

        Args:
            value: The reported trend direction.
            info: Pydantic validation context containing previously validated fields.

        Returns:
            The validated trend direction.

        Raises:
            ValueError: If the direction is inconsistent with percent_change.
        """
        percent_change = info.data.get("percent_change")
        if percent_change is None:
            return value
        if percent_change > 0 and value != TrendDirection.UP:
            raise ValueError("direction must be 'up' when percent_change is positive")
        if percent_change < 0 and value != TrendDirection.DOWN:
            raise ValueError("direction must be 'down' when percent_change is negative")
        if percent_change == 0 and value != TrendDirection.FLAT:
            raise ValueError("direction must be 'flat' when percent_change is zero")
        return value


class AnalyticsResponse(BaseModel):
    """Response payload for an AI-powered analytics summary request.

    Attributes:
        metric: The analytics metric that was analyzed.
        start_date: Start of the analysis period, inclusive.
        end_date: End of the analysis period, inclusive.
        aggregate_value: Aggregated value of the metric over the period.
        insights: List of AI-generated insights derived from the data.
        generated_at: Timestamp when the analytics response was generated.
    """

    model_config = ConfigDict(from_attributes=True)

    metric: AnalyticsMetric
    start_date: date
    end_date: date
    aggregate_value: float
    insights: list[AnalyticsInsight] = Field(default_factory=list)
    generated_at: datetime