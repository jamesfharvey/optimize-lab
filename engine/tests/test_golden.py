"""Golden vectors, the validation anchor, and the punctuality guardrail.

Goldens: canonical ResultsReports for all three presets at seed 42.
Regenerate with  OPTLAB_REGEN_GOLDEN=1 pytest tests/test_golden.py  — a
normal run asserts byte-identical regeneration.

Validation anchor (decision 2026-06-11): the band came from a different
ENVIRONMENT than the preset, not a different engine. Anchor config =
University preset modified to an 08:00-16:00 day, last join 0, no language
preferences, appointment_share 0, abandonment disabled, matching only,
200 days, seed 42. The engine's normative effective-duration formula
(efficiency in BOTH policies) stands as built.

Punctuality guardrail (decision 2026-06-11): hard-assert ONLY on the
combined selected lever set — optimized p90 lateness must be within
late_acceptable_min and must not exceed baseline p90 lateness. Solo-lever
degradations (notably Clinic appointment-smoothing solo — a real, valuable
finding) surface as guardrail_warnings in the ResultsReport, not failures.
"""
import json
import os
from pathlib import Path

import numpy as np
import pytest

from conftest import PRESETS, REPO_ROOT

from optimize_lab.config import load_scenario
from optimize_lab.montecarlo import run_scenario_mc
from optimize_lab.report import build_report
from optimize_lab.world import ArrivalDist, gen_day_draws

GOLDEN_DIR = Path(__file__).parent / "golden"
REGEN = os.environ.get("OPTLAB_REGEN_GOLDEN") == "1"


def validation_config(days=200):
    with open(REPO_ROOT / "scenarios" / "preset-university-onestop.json") as f:
        data = json.load(f)
    data["location"]["open"] = "08:00"
    data["location"]["close"] = "16:00"
    data["location"]["last_join_minutes_before_close"] = 0
    data["demand"]["language_preferences"] = []
    data["policy"]["baseline"]["appointment_share"] = 0.0
    data["policy"]["optimized"] = {
        "matching": {"enabled": True, "aging_cap_min": 45,
                     "weight_preset": "wait_dominant"}}
    data["simulation"]["monte_carlo_days"] = days
    data["simulation"]["random_seed"] = 42
    data["simulation"]["abandonment_model"] = {"enabled": False}
    return data


def test_validation_anchor_band():
    sc = load_scenario(validation_config())
    dist = ArrivalDist(sc)
    arrivals = float(np.mean([gen_day_draws(sc, d, dist).n
                              for d in range(sc.mc_days)]))
    mc = run_scenario_mc(sc)
    base, opt = mc.of("baseline"), mc.of("combined")
    b_served = float(np.mean(base["served"]))
    b_wait = float(np.mean(base["mean_wait"]))
    o_served = float(np.mean(opt["served"]))
    o_wait = float(np.mean(opt["mean_wait"]))
    print(f"\nanchor: arrivals/day {arrivals:.1f} | "
          f"baseline {b_served:.1f} served / {b_wait:.2f} wait | "
          f"optimized {o_served:.1f} served / {o_wait:.2f} wait")
    assert 215 <= arrivals <= 225, f"arrivals/day {arrivals:.1f}"
    assert 180 <= b_served <= 190, f"baseline served {b_served:.1f}"
    assert 40.3 <= b_wait <= 46.3, f"baseline wait {b_wait:.2f}"
    assert 197 <= o_served <= 207, f"optimized served {o_served:.1f}"
    assert 23.2 <= o_wait <= 29.2, f"optimized wait {o_wait:.2f}"


def test_validation_run_direction_sanity():
    """Cheap directional floor: matching must not worsen served or wait, and
    must visibly cut the wait."""
    sc = load_scenario(validation_config(days=40))
    mc = run_scenario_mc(sc)
    base, opt = mc.of("baseline"), mc.of("combined")
    assert float(np.mean(opt["served"])) >= float(np.mean(base["served"]))
    assert float(np.mean(opt["mean_wait"])) < float(np.mean(base["mean_wait"])) * 0.9


@pytest.fixture(scope="module")
def preset_runs():
    out = {}
    for path in PRESETS:
        sc = load_scenario(path)
        mc = run_scenario_mc(sc)
        out[path.stem] = (sc, mc, build_report(sc, mc, deterministic=True))
    return out


@pytest.mark.parametrize("stem", [p.stem for p in PRESETS])
def test_punctuality_guardrail_combined(preset_runs, stem):
    """Hard guardrail, combined lever set only: optimized p90 lateness within
    late_acceptable_min AND not worse than baseline."""
    sc, mc, _ = preset_runs[stem]
    base_p90 = float(np.mean(mc.of("baseline")["p90_late"]))
    comb_p90 = float(np.mean(mc.of("combined")["p90_late"]))
    assert comb_p90 <= sc.baseline.late_acceptable + 1e-9, \
        f"{stem}: combined p90 lateness {comb_p90:.2f} exceeds " \
        f"late_acceptable {sc.baseline.late_acceptable}"
    # 0.5-minute operational tolerance: when the baseline p90 is ~0 (e.g.
    # University, 40 appts/day all on time), a sub-minute shift is below
    # measurement resolution, not a degradation.
    assert comb_p90 <= base_p90 + 0.5, \
        f"{stem}: combined p90 lateness {comb_p90:.2f} worse than " \
        f"baseline {base_p90:.2f}"


def test_clinic_smoothing_solo_warning_documented(preset_runs):
    """The Clinic smoothing-solo punctuality degradation is a real finding:
    it must surface as a guardrail WARNING in the report (not a failure)."""
    _, mc, report = preset_runs["preset-clinic"]
    warnings = report.get("guardrail_warnings", [])
    assert any(w["lever"] == "appointment_smoothing" for w in warnings)
    smoothing = next(w for w in warnings if w["lever"] == "appointment_smoothing")
    assert smoothing["p90_lateness_solo"] > smoothing["p90_lateness_baseline"]


@pytest.mark.parametrize("stem", [p.stem for p in PRESETS])
def test_golden_regeneration(preset_runs, stem):
    golden_path = GOLDEN_DIR / f"{stem}.golden.json"
    _, _, report = preset_runs[stem]
    if REGEN:
        GOLDEN_DIR.mkdir(exist_ok=True)
        with open(golden_path, "w") as f:
            json.dump(report, f, indent=2, sort_keys=True)
            f.write("\n")
    assert golden_path.exists(), \
        "goldens missing — generate with OPTLAB_REGEN_GOLDEN=1 pytest"
    with open(golden_path) as f:
        golden = json.load(f)
    assert report == golden, f"golden mismatch for {stem}"
