"""Golden vectors, validation anchors, and the on-time guardrail.

Goldens: canonical ResultsReports for all three presets at seed 42. Regenerate
with  OPTLAB_REGEN_GOLDEN=1 pytest tests/test_golden.py  — a normal run then
asserts byte-identical regeneration.

VALIDATION STATUS: the session-validated anchor band (build prompt acceptance
criterion 2) is encoded below as a strict xfail. The engine implements the
normative effective-duration rule (target / (efficiency x daily_form)) in BOTH
policies, which lands baseline mean wait inside the band but overshoots
baseline served/day (~206 vs 185+-4): with employees serving at their true
efficiency-scaled speeds, baseline FIFO already captures most of the
efficiency upside through utilization. The 185 figure equals total capacity /
mean TARGET duration (3240 / 17.51 = 185.0), i.e. a baseline running at target
speed. Per the build prompt the engine was NOT tuned to force the band; the
discrepancy is reported, with the decision (which baseline the lab should
model) left to the spec owner. Goldens are not committed until that call is
made; until then the golden tests skip.
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

GOLDEN_DIR = Path(__file__).parent / "golden"
REGEN = os.environ.get("OPTLAB_REGEN_GOLDEN") == "1"


def validation_config():
    """University preset modified per the build prompt: appointment_share 0,
    abandonment disabled, no breaks/incompletes (the preset has none),
    matching lever only, 200 days, seed 42."""
    with open(REPO_ROOT / "scenarios" / "preset-university-onestop.json") as f:
        data = json.load(f)
    data["policy"]["baseline"]["appointment_share"] = 0.0
    data["policy"]["optimized"] = {
        "matching": {"enabled": True, "aging_cap_min": 45,
                     "weight_preset": "wait_dominant"}}
    data["simulation"]["monte_carlo_days"] = 200
    data["simulation"]["random_seed"] = 42
    data["simulation"]["abandonment_model"] = {"enabled": False}
    return data


@pytest.mark.xfail(
    strict=True,
    reason="Session-validated band assumes a target-speed baseline; engine "
           "implements the normative efficiency-scaled durations in both "
           "policies. STOP-and-report per acceptance criterion 2 — awaiting "
           "spec-owner decision. See module docstring and README.")
def test_validation_anchor_band():
    sc = load_scenario(validation_config())
    mc = run_scenario_mc(sc)
    base, opt = mc.of("baseline"), mc.of("combined")
    b_served = float(np.mean(base["served"]))
    b_wait = float(np.mean(base["mean_wait"]))
    o_served = float(np.mean(opt["served"]))
    o_wait = float(np.mean(opt["mean_wait"]))
    assert 181 <= b_served <= 189, f"baseline served {b_served:.1f}"
    assert 40.8 <= b_wait <= 45.8, f"baseline wait {b_wait:.2f}"
    assert 198 <= o_served <= 206, f"optimized served {o_served:.1f}"
    assert 23.7 <= o_wait <= 28.7, f"optimized wait {o_wait:.2f}"
    gain = (o_served - b_served) / b_served * 100
    assert 8.0 <= gain <= 10.0, f"served gain {gain:.1f}%"


def test_validation_run_direction_sanity():
    """Engine-behavior floor that must hold regardless of the anchor decision:
    matching must not worsen wait or served, and must visibly cut the wait."""
    cfg = validation_config()
    cfg["simulation"]["monte_carlo_days"] = 40
    sc = load_scenario(cfg)
    mc = run_scenario_mc(sc)
    base, opt = mc.of("baseline"), mc.of("combined")
    assert float(np.mean(opt["served"])) >= float(np.mean(base["served"]))
    assert float(np.mean(opt["mean_wait"])) < float(np.mean(base["mean_wait"])) * 0.9


@pytest.fixture(scope="module")
def clinic_mc():
    sc = load_scenario(REPO_ROOT / "scenarios" / "preset-clinic.json")
    return sc, run_scenario_mc(sc)


def test_clinic_on_time_guardrail_combined(clinic_mc):
    """Acceptance criterion 4: the optimized (combined) run must not degrade
    the appointment on-time rate vs baseline in the Clinic preset."""
    sc, mc = clinic_mc
    base = float(np.mean(mc.of("baseline")["on_time_rate"]))
    combined = float(np.mean(mc.of("combined")["on_time_rate"]))
    assert combined >= base - 1e-9, f"combined degrades on-time: {combined} < {base}"


@pytest.mark.xfail(
    strict=True,
    reason="Strict 'under any lever set' reading: appointment_smoothing SOLO "
           "degrades clinic on-time (0.799 -> 0.737). Pushing share 0.60 -> "
           "0.80 under employee_specific pinning with a +-5 min grace adds "
           "~30 shown appointments/day without the capacity relief of the "
           "other levers. Real dynamic, reported to the spec owner alongside "
           "the validation-anchor decision; combined stack passes.")
def test_clinic_on_time_guardrail_all_lever_sets(clinic_mc):
    sc, mc = clinic_mc
    base = float(np.mean(mc.of("baseline")["on_time_rate"]))
    for label, fs in mc.plan.items():
        if label == "baseline":
            continue
        rate = float(np.mean(mc.arrays[fs]["on_time_rate"]))
        assert rate >= base - 0.005, f"{label} degrades on-time: {rate} < {base}"


@pytest.mark.parametrize("path", PRESETS, ids=lambda p: p.stem)
def test_golden_regeneration(path):
    golden_path = GOLDEN_DIR / f"{path.stem}.golden.json"
    sc = load_scenario(path)
    if not REGEN and not golden_path.exists():
        pytest.skip("goldens not yet generated — pending validation anchor "
                    "sign-off (see module docstring)")
    mc = run_scenario_mc(sc)
    report = build_report(sc, mc, deterministic=True)
    if REGEN:
        GOLDEN_DIR.mkdir(exist_ok=True)
        with open(golden_path, "w") as f:
            json.dump(report, f, indent=2, sort_keys=True)
            f.write("\n")
    with open(golden_path) as f:
        golden = json.load(f)
    assert report == golden, f"golden mismatch for {path.stem}"
