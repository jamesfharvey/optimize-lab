"""Paired Monte-Carlo driver: identical per-day draws across every variant."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import Scenario
from .levers import build_run_plan, make_params, optimize_breaks, selected_levers
from .simulate import run_day
from .world import ArrivalDist, gen_day_draws

METRIC_FIELDS = [
    "served", "turned_away", "abandoned", "resolved", "incompletes",
    "incomplete_min", "appts_shown", "pct_on_time", "pct_acceptable",
    "p50_late", "p90_late", "max_late", "mean_wait", "p90_wait",
    "mean_csat", "makespan",
]


@dataclass
class MCResult:
    plan: dict                      # label -> frozenset
    arrays: dict                    # frozenset -> {field: np.ndarray over days}
    break_schedule: dict | None
    csv_rows: list = field(default_factory=list)

    def of(self, label: str) -> dict:
        return self.arrays[self.plan[label]]


def run_scenario_mc(sc: Scenario, collect_csv: bool = False,
                    progress=None) -> MCResult:
    dist = ArrivalDist(sc)
    sel = selected_levers(sc)
    break_schedule = None
    if "break_scheduling" in sel:
        break_schedule = optimize_breaks(sc, dist)

    plan = build_run_plan(sc)
    unique_sets = sorted(set(plan.values()), key=lambda fs: (len(fs), sorted(fs)))
    params_by_set = {fs: make_params(sc, fs, break_schedule) for fs in unique_sets}
    csv_sets = {plan["baseline"], plan["combined"]} if collect_csv else set()
    set_labels = {plan["baseline"]: "baseline", plan["combined"]: "combined"}

    per_day = {fs: [] for fs in unique_sets}
    csv_rows = []
    for d in range(sc.mc_days):
        draws = gen_day_draws(sc, d, dist)
        for fs in unique_sets:
            collect = fs in csv_sets
            metrics, rows = run_day(sc, dist, draws, params_by_set[fs], collect)
            per_day[fs].append(metrics)
            if collect and rows:
                label = set_labels.get(fs, "run")
                csv_rows.extend((label, d) + r for r in rows)
        if progress and (d + 1) % 50 == 0:
            progress(d + 1, sc.mc_days)

    arrays = {}
    for fs, metric_list in per_day.items():
        arrays[fs] = {f: np.array([getattr(m, f) for m in metric_list])
                      for f in METRIC_FIELDS}
    return MCResult(plan=plan, arrays=arrays, break_schedule=break_schedule,
                    csv_rows=csv_rows)
