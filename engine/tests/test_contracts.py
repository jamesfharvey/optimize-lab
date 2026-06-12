"""Schema round-trips: presets load and validate; reports validate."""
import json

import jsonschema
import pytest

from conftest import PRESETS, load_mini, mini_config

from optimize_lab.config import load_scenario, validate_report_dict
from optimize_lab.montecarlo import run_scenario_mc
from optimize_lab.report import build_report


def test_presets_exist():
    assert len(PRESETS) == 3


@pytest.mark.parametrize("path", PRESETS, ids=lambda p: p.stem)
def test_preset_loads_and_validates(path):
    sc = load_scenario(path)
    assert sc.n_services >= 1
    assert sc.n_employees >= 1
    assert abs(sum(s.share for s in sc.services) - 1.0) < 1e-6
    assert sc.close_min > sc.open_min
    # every service reachable by at least one employee
    for s in sc.services:
        assert sc.eligible_emps[s.idx]


def test_schema_violation_rejected():
    cfg = mini_config()
    del cfg["employees"]
    with pytest.raises(jsonschema.ValidationError):
        load_scenario(cfg)


def test_semantic_violation_rejected():
    cfg = mini_config()
    cfg["services"][0]["demand_share"] = 0.9  # sums to 1.4
    with pytest.raises(ValueError):
        load_scenario(cfg)

    cfg = mini_config()
    cfg["employees"][0]["profile"][0]["service_id"] = "ZZ"
    with pytest.raises(ValueError):
        load_scenario(cfg)


def test_defaults_applied():
    cfg = mini_config()
    del cfg["location"]["last_join_minutes_before_close"]
    cfg["policy"]["baseline"] = {"appointment_share": 0.2}
    cfg["policy"]["optimized"] = {}
    del cfg["policy"]["no_show_rate"]
    cfg["simulation"] = {}
    sc = load_scenario(cfg)
    assert sc.last_join == 30
    assert sc.baseline.scheduling_method == "round_robin"
    assert sc.baseline.early_summon_max == 10     # punctuality defaults
    assert sc.baseline.late_ok == 5
    assert sc.baseline.late_acceptable == 15
    assert sc.baseline.distribution == "even"
    assert sc.no_show_rate == 0.08
    assert sc.mc_days == 200
    assert sc.jitter == 0.12
    assert sc.seed == 42
    assert sc.abandonment_enabled is True
    assert sc.levers["matching"]["enabled"] is True   # schema default
    assert sc.weights == (0.6, 0.4, 0.0)              # wait_dominant preset
    assert sc.aging_cap == 45
    assert sc.alpha_wait == 0.6                       # v1.4 csat_model defaults
    assert sc.beta_early == 0.1
    assert sc.beta_late == 0.4
    assert sc.range_k == 0.15


def test_beta_late_falls_back_to_legacy_beta_accuracy():
    cfg = mini_config()
    cfg["simulation"]["csat_model"] = {"beta_accuracy": 0.3}
    sc = load_scenario(cfg)
    assert sc.beta_late == 0.3
    cfg["simulation"]["csat_model"] = {"beta_accuracy": 0.3, "beta_late": 0.25}
    assert load_scenario(cfg).beta_late == 0.25       # explicit wins


def test_weight_preset_resolution():
    sc = load_mini(policy={
        "baseline": {"appointment_share": 0.0},
        "optimized": {"matching": {"enabled": True,
                                   "weight_preset": "fairness_first"}},
    })
    assert sc.weights == (0.0, 0.9, 0.1)
    # explicit weights override the preset
    sc = load_mini(policy={
        "baseline": {"appointment_share": 0.0},
        "optimized": {"matching": {
            "enabled": True, "weight_preset": "fairness_first",
            "weights": {"throughput": 0.5, "wait": 0.5, "csat": 0.0}}},
    })
    assert sc.weights == (0.5, 0.5, 0.0)


def test_report_round_trip(tmp_path):
    cfg = mini_config()
    cfg["demand"]["visitors_per_day"] = 12
    cfg["simulation"]["monte_carlo_days"] = 5
    cfg["policy"]["baseline"]["appointment_share"] = 0.3
    sc = load_scenario(cfg)
    mc = run_scenario_mc(sc, collect_csv=True)
    report = build_report(sc, mc, deterministic=True, csv_path="x.csv")
    validate_report_dict(report)  # also validated inside build_report
    # required blocks present and coherent
    assert report["meta"]["random_seed"] == 7
    assert report["meta"]["monte_carlo_days"] == 5
    m = report["metrics"]
    for key in ("mean_wait_min", "p90_wait_min", "served_per_day",
                "turned_away_per_day", "mean_csat"):
        assert "baseline" in m[key] and "optimized" in m[key]
        assert "ci95_low" in m[key] or m[key]["baseline"] == 0
    punct = m["appointment_punctuality"]
    for side in ("baseline", "optimized"):
        for stat in ("pct_on_time", "pct_acceptable", "p50_lateness_min",
                     "p90_lateness_min", "max_lateness_min"):
            assert stat in punct[side]
    steps = [w["step"] for w in report["waterfall"]]
    assert steps == sorted(steps) and steps[0] == 0
    assert report["waterfall"][0]["levers_active"] == []
    assert {a["lever"] for a in report["attribution"]} == {"matching"}
    assert json.loads(json.dumps(report)) == report  # JSON-stable


def test_report_deterministic_repeatable():
    cfg = mini_config()
    cfg["demand"]["visitors_per_day"] = 10
    cfg["simulation"]["monte_carlo_days"] = 3
    sc1, sc2 = load_scenario(cfg), load_scenario(json.loads(json.dumps(cfg)))
    r1 = build_report(sc1, run_scenario_mc(sc1), deterministic=True)
    r2 = build_report(sc2, run_scenario_mc(sc2), deterministic=True)
    assert r1 == r2
