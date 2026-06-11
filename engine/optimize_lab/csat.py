"""Predicted-CSAT model: Base(e,s) x W_wait x W_accuracy x W_duration.

The spec fixes the multiplicative shape and the alpha/beta/gamma parameters;
the exact W functions are engine-defined and documented here and in README:

  W_wait     = 1 - alpha * min(1, max(0, wait - promised) / 60)
               (penalty only when the visitor waited LONGER than promised;
                promised wait is the grace concept of the spec)
  W_accuracy = 1 - beta  * min(1, |wait - promised| / 60)
               (promise accuracy: off in either direction erodes trust)
  W_duration = 1 - gamma * min(1, max(0, actual/target - 1))
               (penalty only when the visit ran longer than the service target)

All horizons are 60-minute saturations; factors are bounded to [0, 1] before
multiplying so CSAT stays within [0, 100].

5-point translation (documented formula):
  five_pt(c) = 5 - ((100 - c) / (100 - c_baseline)) ** 1.5
anchored so the baseline mean lands exactly at 4.0 and gains flatten near the
100-point ceiling (diminishing returns). Clamped to [1, 5].
"""
from __future__ import annotations


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def visit_csat(base: float, wait: float, promised: float,
               actual_duration: float, target_duration: float,
               alpha: float, beta: float, gamma: float) -> float:
    over = max(0.0, wait - promised)
    w_wait = _clamp01(1.0 - alpha * min(1.0, over / 60.0))
    w_acc = _clamp01(1.0 - beta * min(1.0, abs(wait - promised) / 60.0))
    over_dur = max(0.0, actual_duration / target_duration - 1.0)
    w_dur = _clamp01(1.0 - gamma * min(1.0, over_dur))
    return base * w_wait * w_acc * w_dur


def predicted_csat(base: float, eff_duration: float, target_duration: float,
                   gamma: float) -> float:
    """Routing-time estimate: base CSAT discounted by expected over-duration."""
    over_dur = max(0.0, eff_duration / target_duration - 1.0)
    return base * _clamp01(1.0 - gamma * min(1.0, over_dur))


def to_five_point(csat: float, baseline_csat: float) -> float:
    if baseline_csat >= 100.0 or csat >= 100.0:
        return 5.0
    ratio = (100.0 - csat) / (100.0 - baseline_csat)
    val = 5.0 - ratio ** 1.5
    return max(1.0, min(5.0, val))
