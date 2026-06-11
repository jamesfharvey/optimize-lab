"""Lever set -> runtime variant parameters, and the break-scheduling search.

Run plan per scenario: baseline once, each SELECTED lever solo, the cumulative
prefixes in canonical order (for the waterfall), and the combined set. Runs
with identical active-lever sets are deduplicated and simulated once.
"""
from __future__ import annotations

from .config import LEVER_ORDER, Scenario
from .simulate import VariantParams, run_day
from .world import ArrivalDist, gen_day_draws

LEVER_LABELS = {
    "matching": "+ Focus matching & routing",
    "appointment_smoothing": "+ Appointment smoothing",
    "prep_in_queue": "+ Prep-in-queue",
    "deflection": "+ Deflection",
    "running_late": "+ Running Late",
    "break_scheduling": "+ Break scheduling",
}

BREAK_SEARCH_EVAL_DAYS = 20
BREAK_SEARCH_STEP = 15


def selected_levers(sc: Scenario) -> list:
    return [name for name in LEVER_ORDER if sc.levers[name]["enabled"]]


def make_params(sc: Scenario, active: frozenset,
                break_schedule: dict | None = None) -> VariantParams:
    share = sc.baseline.appointment_share
    dist = sc.baseline.distribution
    if "appointment_smoothing" in active:
        share = sc.levers["appointment_smoothing"]["target_appointment_share"]
        dist = "even"  # the lever spreads appointments evenly by definition
    prep_factor, inc_factor = 1.0, 1.0
    if "prep_in_queue" in active:
        prep_factor = 1.0 - sc.levers["prep_in_queue"]["duration_reduction"]
        inc_factor = 1.0 - sc.levers["prep_in_queue"]["incomplete_reduction"]
    deflect_rate = sc.levers["deflection"]["rate"] if "deflection" in active else 0.0
    noshow_red = (sc.levers["running_late"]["no_show_reduction"]
                  if "running_late" in active else 0.0)
    breaks = break_schedule if "break_scheduling" in active else None
    return VariantParams(
        appt_share=share,
        distribution=dist,
        matching="matching" in active,
        prep_factor=prep_factor,
        inc_factor=inc_factor,
        deflect_rate=deflect_rate,
        noshow_reduction=noshow_red,
        breaks=breaks,
    )


def build_run_plan(sc: Scenario) -> dict:
    """Ordered {label: frozenset(active levers)}. Labels: baseline,
    solo:<lever>, cum:<k>, combined."""
    sel = selected_levers(sc)
    plan = {"baseline": frozenset()}
    for lv in sel:
        plan[f"solo:{lv}"] = frozenset([lv])
    for k in range(2, len(sel)):
        plan[f"cum:{k}"] = frozenset(sel[:k])
    plan["combined"] = frozenset(sel)
    return plan


def optimize_breaks(sc: Scenario, dist: ArrivalDist,
                    eval_days: int = BREAK_SEARCH_EVAL_DAYS) -> dict:
    """Greedy per-employee grid search (15-minute steps within the shift,
    duration preserved) minimizing predicted mean wait over a fixed pool of
    paired evaluation days, simulated under the baseline policy plus the
    candidate schedule. Returns {emp_idx: (start, end)} for employees that
    declare a break window; the engine never invents breaks."""
    with_breaks = [emp for emp in sc.employees if emp.brk is not None]
    if not with_breaks:
        return {}
    days = [gen_day_draws(sc, d, dist) for d in range(min(eval_days, sc.mc_days))]
    current = {emp.idx: (float(emp.brk.start), float(emp.brk.end))
               for emp in with_breaks}

    def mean_wait(schedule):
        params = make_params(sc, frozenset(["break_scheduling"]), schedule)
        total = 0.0
        for draws in days:
            m, _ = run_day(sc, dist, draws, params)
            total += m.mean_wait
        return total / len(days)

    for emp in with_breaks:
        duration = emp.brk.end - emp.brk.start
        best_start, best_score = None, None
        start = emp.work_start
        while start + duration <= emp.work_end:
            cand = dict(current)
            cand[emp.idx] = (float(start), float(start + duration))
            score = mean_wait(cand)
            if best_score is None or score < best_score - 1e-9:
                best_start, best_score = start, score
            start += BREAK_SEARCH_STEP
        current[emp.idx] = (float(best_start), float(best_start + duration))
    return current
