"""Risk assessment: rules over evidence, never an LLM judgement."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

METHOD_VERSION = "risk-v1"


class RiskDimension(StrEnum):
    LEGAL = "LEGAL"
    OCCUPANCY = "OCCUPANCY"
    DEVELOPER = "DEVELOPER"
    CONSTRUCTION = "CONSTRUCTION"
    LIQUIDITY = "LIQUIDITY"
    MARKET = "MARKET"
    VALUATION_UNCERTAINTY = "VALUATION_UNCERTAINTY"
    DATA_QUALITY = "DATA_QUALITY"
    AUCTION = "AUCTION"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def penalty(self) -> float:
        return {"LOW": 10.0, "MEDIUM": 40.0, "HIGH": 80.0}[self.value]


DIMENSION_WEIGHTS: dict[RiskDimension, float] = {
    RiskDimension.LEGAL: 0.20,
    RiskDimension.OCCUPANCY: 0.10,
    RiskDimension.DEVELOPER: 0.12,
    RiskDimension.CONSTRUCTION: 0.10,
    RiskDimension.LIQUIDITY: 0.12,
    RiskDimension.MARKET: 0.10,
    RiskDimension.VALUATION_UNCERTAINTY: 0.13,
    RiskDimension.DATA_QUALITY: 0.08,
    RiskDimension.AUCTION: 0.05,
}


@dataclass(frozen=True, slots=True)
class RedFlag:
    dimension: RiskDimension
    message: str
    level: RiskLevel


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    levels: dict[RiskDimension, RiskLevel]
    red_flags: tuple[RedFlag, ...] = field(default_factory=tuple)
    method_version: str = METHOD_VERSION

    @property
    def score(self) -> float:
        """0-100 where 100 is lowest risk."""
        penalty = sum(
            DIMENSION_WEIGHTS[dim] * self.levels.get(dim, RiskLevel.MEDIUM).penalty
            for dim in RiskDimension
        )
        return max(0.0, min(100.0, 100.0 - penalty))

    @property
    def overall(self) -> RiskLevel:
        s = self.score
        if s >= 75:
            return RiskLevel.LOW
        return RiskLevel.MEDIUM if s >= 50 else RiskLevel.HIGH
