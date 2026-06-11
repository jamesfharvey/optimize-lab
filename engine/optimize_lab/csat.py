"""Predicted-CSAT model.

WALK-INS:  Base(e,s) x W_wait x W_accuracy x W_duration.
The spec fixes the multiplicative shape and the alpha/beta/gamma parameters;
the exact W functions are engine-defined and documented here and in README:

  W_wait     = 1 - alpha * min(1, max(0, wait - promised) / 60)
               (penalty only when the visitor waited LONGER than promised)
  W_accuracy = 1 - beta  * min(1, |wait - promised| / 60)
               (promise accuracy: off in either direction erodes trust)
  W_duration = 1 - gamma * min(1, max(0, actual/target - 1))
               (penalty only when the visit ran longer than the service target)

All horizons are 60-minute saturations; factors are bounded to [0, 1] before
multiplying so CSAT stays within [0, 100].

APPOINTMENTS:  Base(e,s) x W_punctuality x W_duration.
The promise-accuracy penalty is replaced by a kinked-convex curve driven by
the scenario's punctuality inputs (lateness L = max(0, summon - scheduled);
early summons carry no penalty and no bonus):

  L <= late_ok_min                          -> 1.0 (promise kept)
  late_ok_min < L <= late_acceptable_min    -> mild linear ramp down to
                                               1 - RAMP_DEPTH at the kink
  L > late_acceptable_min                   -> penalty grows quadratically:
       1 - RAMP_DEPTH - (1 - RAMP_DEPTH) * ((L - late_acceptable)/CONVEX_HORIZON)^2
  floor-capped at 0 (as all factors are).

The curve SHAPE (flat -> mild ramp -> convex blow-up) is operationally
grounded; the STEEPNESS constants below are assumptions until VE.12.01
produces real lateness-vs-rating pairs, at which point the curve becomes
fittable per customer. Because the factor is multiplicative on Base(e,s) and
the duration term, a high-CSAT, faster-than-target employee partially
recovers a late start — intentional.

5-point translation (documented formula):
  five_pt(c) = 5 - ((100 - c) / (100 - c_baseline)) ** 1.5
anchored so the baseline mean lands exactly at 4.0 and gains flatten near the
100-point ceiling (diminishing returns). Clamped to [1, 5].
"""
from __future__ import annotations

# Assumption-flagged steepness constants (shape is grounded; steepness is not).
RAMP_DEPTH = 0.10        # factor lost across the late_ok -> late_acceptable ramp
CONVEX_HORIZON = 60.0    # minutes past late_acceptable at which the factor hits 0


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


def punctuality_factor(lateness: float, late_ok: float,
                       late_acceptable: float) -> float:
    """Kinked-convex appointment lateness penalty factor in [0, 1]."""
    if lateness <= late_ok:
        return 1.0
    if lateness <= late_acceptable:
        span = late_acceptable - late_ok
        if span <= 0:
            return 1.0 - RAMP_DEPTH
        return 1.0 - RAMP_DEPTH * (lateness - late_ok) / span
    over = (lateness - late_acceptable) / CONVEX_HORIZON
    return _clamp01(1.0 - RAMP_DEPTH - (1.0 - RAMP_DEPTH) * over * over)


def appointment_csat(base: float, lateness: float, late_ok: float,
                     late_acceptable: float, actual_duration: float,
                     target_duration: float, gamma: float) -> float:
    w_punct = punctuality_factor(lateness, late_ok, late_acceptable)
    over_dur = max(0.0, actual_duration / target_duration - 1.0)
    w_dur = _clamp01(1.0 - gamma * min(1.0, over_dur))
    return base * w_punct * w_dur


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
