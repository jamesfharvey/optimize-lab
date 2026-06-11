import math
import sys
from pathlib import Path

import numpy as np
import pytest

ENGINE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_DIR.parent
sys.path.insert(0, str(ENGINE_DIR))

from optimize_lab.config import load_scenario  # noqa: E402
from optimize_lab.world import DayDraws  # noqa: E402

SCENARIOS = REPO_ROOT / "scenarios"
PRESETS = sorted(SCENARIOS.glob("preset-*.json"))


@pytest.fixture
def repo_root():
    return REPO_ROOT


def mini_config(**over):
    """Small, fully-deterministic scenario for surgical rule tests.

    09:00-12:00, last join 30 min before close, two services, jitter 0
    (daily form == 1.0 exactly), abandonment off unless a test opts in.
    """
    cfg = {
        "meta": {"scenario_name": "mini", "schema_version": "1.0.0"},
        "location": {"name": "Mini", "open": "09:00", "close": "12:00",
                     "last_join_minutes_before_close": 30},
        "services": [
            {"id": "A", "name": "Alpha", "target_duration_min": 30,
             "demand_share": 0.5},
            {"id": "B", "name": "Beta", "target_duration_min": 10,
             "demand_share": 0.5},
        ],
        "employees": [
            {"id": "E1", "name": "One", "profile": [
                {"service_id": "A", "efficiency": 1.0, "csat": 80},
                {"service_id": "B", "efficiency": 1.0, "csat": 80}]},
        ],
        "demand": {"visitors_per_day": 4,
                   "arrival_pattern": {"shape": "uniform"}},
        "policy": {
            "baseline": {"appointment_share": 0.0,
                         "scheduling_method": "round_robin",
                         "appointment_punctuality": {
                             "early_summon_max_min": 10,
                             "late_ok_min": 5,
                             "late_acceptable_min": 15},
                         "appointment_distribution": "even"},
            "optimized": {"matching": {"enabled": True, "aging_cap_min": 45,
                                       "weight_preset": "wait_dominant"}},
            "no_show_rate": 0.0,
        },
        "simulation": {"monte_carlo_days": 1, "daily_form_jitter": 0.0,
                       "random_seed": 7,
                       "abandonment_model": {"enabled": False}},
    }
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(val)
        else:
            cfg[key] = val
    return cfg


def hand_draws(sc, arrivals, services, **over):
    """Build a DayDraws with explicit arrival times (minutes) and service ids;
    all stochastic inputs default to inert values."""
    n = len(arrivals)
    sidx = np.array([sc.service_index[s] for s in services], dtype=np.int64)
    d = dict(
        day=0,
        n=n,
        u_appt=np.full(n, 0.99),
        arrival=np.array(arrivals, dtype=float),
        u_slot=np.linspace(0.1, 0.9, n),
        service=sidx,
        lang=np.full(n, -1, dtype=np.int64),
        patience=np.full(n, math.inf),
        u_noshow=np.full(n, 0.99),
        u_deflect=np.full(n, 0.99),
        u_inc=np.full(n, 0.99),
        u_inc_cost=np.full(n, 0.5),
        form=np.ones((sc.n_employees, sc.n_services)),
        brk_shift=np.zeros(sc.n_employees),
    )
    d.update(over)
    return DayDraws(**d)


def load_mini(**over):
    return load_scenario(mini_config(**over))
