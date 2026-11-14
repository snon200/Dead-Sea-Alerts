"""Interface between the zones API and the processing pipeline."""

from typing import Literal, TypedDict


class Polygon(TypedDict):
    type: Literal["Polygon"]
    coordinates: list[list[list[float]]]


class MultiPolygon(TypedDict):
    type: Literal["MultiPolygon"]
    coordinates: list[list[list[list[float]]]]


class ZoneSummary(TypedDict):
    los_velocity_mm_per_year: float | None
    cumulative_shift_mm: float | None
    acceleration_mm_per_year2: float | None
    latest_pair_mean_coherence: float | None
    latest_pair_reference_time: str | None
    latest_pair_secondary_time: str | None


class CumulativePoint(TypedDict):
    time: str
    displacement_mm: float


class Zone(TypedDict):
    id: str
    geometry: Polygon | MultiPolygon
    risk_level: Literal["high", "elevated", "watch", "low"] | None
    summary: ZoneSummary
    cumulative_series: list[CumulativePoint] | None


class ZoneCalculationUnavailable(Exception):
    """Zone calculations have not been connected to the API."""


def get_zones() -> list[Zone]:
    """Return calculated zones using the contract in openapi.json.

    Implement this adapter using the processing pipeline. Do not return an
    empty list to indicate that calculations are unavailable.
    """
    raise ZoneCalculationUnavailable("Zone calculations are not connected yet")
