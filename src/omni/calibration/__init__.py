from __future__ import annotations

import enum
from dataclasses import dataclass


class Direction(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"


class Outcome(str, enum.Enum):
    PENDING = "pending"
    UPPER = "upper"
    LOWER = "lower"
    EXPIRY = "expiry"


@dataclass(frozen=True)
class Benchmark:
    market_probability: float
