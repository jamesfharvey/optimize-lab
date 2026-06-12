"""Predicted-CSAT model.

WALK-INS (v1.4):  Base(e,s) x W_wait x W_accuracy x W_duration.
The check-in promise is a RANGE around the dispatch-forward quote center c:
  band = max(2 min, range_k x c);  low = max(0, c - band);  high = c + band
(range_k is ASSUMPTION-FLAGGED until VE.12.01 + forecast logs allow fitting).

  W_wait     = 1 - alpha * min(1, max(0, wait - high) / 60)
               ("later than promised" = beyond the communicated upper bound;
                a wait inside the promised range is never wait-penalized —
                judgment call, documented in spec section 6)
  W_accuracy = 1.0                                   if low <= wait <= high
             = 1 - beta_late  * min(1, (wait - high) / max(c, eps))  if late
             = 1 - beta_early * min(1, (low - wait) / max(c, eps))   if early
               Asymmetric by design: the mild early side (beta_early default
               0.1, ASSUMPTION-FLAGGED) represents the product's re-forecast
               updates (VE.02.01.US.09 pre-summon notification), >x% change
               alerts, range communication, and visitor-initiated push-back
               when summoned early — none of which are simulated as message
               traffic because sim visitors have no leave-and-return
               behavior. beta_late keeps the legacy beta_accuracy strength.
  W_duration = 1 - gamma * min(1, max(0, actual/target - 1))
               (penalty only when the visit ran longer than the service target)
  W_time     = max(time_floor,
                   1 - delta_wait * max(0, wait - wait_free_min) / wait_ref_min)
               (v1.5: ABSOLUTE-wait disutility - expectation management
                softens waiting's cost, it does not erase time actually
                consumed. A perfectly-quoted long wait is no longer free:
                at defaults an accurately-promised 60-min wait costs ~21%
                vs a free one, floored at 0.5 (~130 min). Walk-ins only -
                an appointment's scheduled time is not "waiting"; the v1.1
                lateness curve continues to govern appointments. All four
                constants are ASSUMPTION-FLAGGED until VE.12.01 ratings
                allow fitting.)

Factors are bounded to [0, 1] before multiplying so CSAT stays in [0, 100].
Three-mechanism wait decomposition (spec v1.5 section 6): (1) expectation /
promise honesty (W_wait + asymmetric W_accuracy), (2) absolute time consumed
(W_time), (3) match quality and duration (Base, W_duration).

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


def promise_range(center: float, range_k: float) -> tuple:
    """The communicated promise interval around the dispatch-forward quote."""
    band = max(2.0, range_k * center)
    return max(0.0, center - band), center + band


def visit_csat(base: float, wait: float, promised_center: float,
               actual_duration: float, target_duration: float,
               alpha: float, beta_early: float, beta_late: float,
               gamma: float, range_k: float, wait_free: float,
               wait_ref: float, delta_wait: float,
               time_floor: float) -> float:
    low, high = promise_range(promised_center, range_k)
    w_wait = _clamp01(1.0 - alpha * min(1.0, max(0.0, wait - high) / 60.0))
    denom = max(promised_center, 1e-9)
    if wait > high:
        w_acc = _clamp01(1.0 - beta_late * min(1.0, (wait - high) / denom))
    elif wait < low:
        w_acc = _clamp01(1.0 - beta_early * min(1.0, (low - wait) / denom))
    else:
        w_acc = 1.0
    over_dur = max(0.0, actual_duration / target_duration - 1.0)
    w_dur = _clamp01(1.0 - gamma * min(1.0, over_dur))
    w_time = max(time_floor,
                 1.0 - delta_wait * max(0.0, wait - wait_free) / wait_ref)
    return base * w_wait * w_acc * w_dur * w_time


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
