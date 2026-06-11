"""conservative_v1 abandonment curve.

P(abandon by wait w):
    0                          for w <= 15
    0.12 * (w - 15) / 45       for 15 < w <= 60   (linear to 12% at 60)
    0.12 + 0.03 * (w - 60)/30  for 60 < w <= 90   (linear to the 15% cap)
    0.15                       for w > 90

Each visitor draws a personal patience threshold at arrival by inverting this
CDF with one uniform u: with probability 0.85 the visitor never abandons.
"""
from __future__ import annotations

import math

CEILING = 0.15
KNEE_PROB = 0.12


def prob_abandon_by(wait_min: float) -> float:
    if wait_min <= 15.0:
        return 0.0
    if wait_min <= 60.0:
        return KNEE_PROB * (wait_min - 15.0) / 45.0
    if wait_min <= 90.0:
        return KNEE_PROB + (CEILING - KNEE_PROB) * (wait_min - 60.0) / 30.0
    return CEILING


def threshold_from_u(u: float) -> float:
    """Inverse CDF: uniform u -> patience threshold in minutes (inf = never)."""
    if u >= CEILING:
        return math.inf
    if u <= KNEE_PROB:
        return 15.0 + 45.0 * u / KNEE_PROB
    return 60.0 + 30.0 * (u - KNEE_PROB) / (CEILING - KNEE_PROB)
