#!/usr/bin/env python
"""Precompute workbench data bundles from the Python reference engine.

The workbench is a VIEWER over engine output, not a second engine: this
script runs the paired Monte-Carlo plan per preset and emits, for each
precomputed lever set, the aggregate numbers the workbench displays. The
workbench never computes queue physics — combinations not precomputed here
render as "not precomputed" with the exact command to generate them.

Default scope (per preset): baseline, each enabled lever SOLO, the
cumulative PROGRESSION prefixes in canonical order, and COMBINED — the same
sets as the appendix tables. Runtime is roughly 5-7 minutes per preset
(the v1.4 dispatch-forward quoting dominates; ~6 min per extra variant when
adding subsets).

Outputs under workbench/data/<preset>/:
  manifest.json      scenario meta, lever config, punctuality inputs,
                     variant list, engine_version, golden sha256
  report.json        the full ResultsReport — byte-identical to the
                     committed golden (verified; the script ABORTS if the
                     engine no longer reproduces it)
  variants/<key>.json one file per precomputed lever set
  bundle.js          everything above wrapped for file:// loading
Plus workbench/data/index.js listing available presets.

Integrity: variant values for baseline/combined are asserted equal to the
corresponding golden ResultsReport fields before anything is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

import numpy as np  # noqa: E402

from optimize_lab.config import LEVER_ORDER, load_scenario  # noqa: E402
from optimize_lab.csat import to_five_point  # noqa: E402
from optimize_lab.levers import (LEVER_LABELS, make_params,  # noqa: E402
                                 selected_levers)
from optimize_lab.montecarlo import METRIC_FIELDS, run_scenario_mc  # noqa: E402
from optimize_lab.report import (_comparison, _punctuality_stats,  # noqa: E402
                                 _round_floats, build_report)
from optimize_lab.simulate import run_day  # noqa: E402
from optimize_lab.world import ArrivalDist, gen_day_draws  # noqa: E402

PRESETS = {
    "preset-university-onestop": "University One-Stop",
    "preset-dmv": "County DMV",
    "preset-clinic": "Community Clinic",
}
DATA_DIR = REPO / "workbench" / "data"
GOLDEN_DIR = REPO / "engine" / "tests" / "golden"

CORE_COMPARED = ["mean_wait", "p90_wait", "served", "turned_away",
                 "abandoned", "mean_csat"]


def engine_version() -> str:
    out = subprocess.run(["git", "describe", "--always", "--tags", "--dirty"],
                         cwd=REPO, capture_output=True, text=True)
    return out.stdout.strip() or "unknown"


def set_key(levers) -> str:
    ordered = [lv for lv in LEVER_ORDER if lv in levers]
    return "+".join(ordered) if ordered else "none"


def set_label(levers) -> str:
    if not levers:
        return "Baseline (FIFO, as configured today)"
    ordered = [lv for lv in LEVER_ORDER if lv in levers]
    return " + ".join(LEVER_LABELS[lv].lstrip("+ ") for lv in ordered)


def variant_record(sc, key, levers, arrays, base_arrays):
    base_csat = float(np.mean(base_arrays["mean_csat"]))
    metrics = {f: float(np.mean(arrays[f])) for f in METRIC_FIELDS}
    rec = {
        "key": key,
        "label": set_label(levers),
        "levers": [lv for lv in LEVER_ORDER if lv in levers],
        "metrics": {
            "mean_wait_min": metrics["mean_wait"],
            "p90_wait_min": metrics["p90_wait"],
            "served_per_day": metrics["served"],
            "resolved_digitally_per_day": metrics["resolved"],
            "turned_away_per_day": metrics["turned_away"],
            "abandoned_per_day": metrics["abandoned"],
            "mean_csat": metrics["mean_csat"],
            "csat_5pt": to_five_point(metrics["mean_csat"], base_csat),
            "incomplete_time_cost_min_per_day": metrics["incomplete_min"],
            "makespan_min": metrics["makespan"],
        },
        "punctuality": _punctuality_stats(arrays),
        "vs_baseline": {},
    }
    for f in CORE_COMPARED:
        cmp_ = _comparison(base_arrays[f], arrays[f])
        rec["vs_baseline"][f] = {k: v for k, v in cmp_.items()
                                 if k in ("delta_pct", "ci95_low", "ci95_high")}
    return _round_floats(rec)


def run_extra_sets(sc, sets):
    """Paired runs for lever sets outside the default plan (same per-day
    draws — gen_day_draws is deterministic in (seed, day))."""
    dist = ArrivalDist(sc)
    per_day = {fs: [] for fs in sets}
    params = {fs: make_params(sc, fs) for fs in sets}
    for d in range(sc.mc_days):
        draws = gen_day_draws(sc, d, dist)
        for fs in sets:
            m, _ = run_day(sc, dist, draws, params[fs])
            per_day[fs].append(m)
    return {fs: {f: np.array([getattr(m, f) for m in ms])
                 for f in METRIC_FIELDS}
            for fs, ms in per_day.items()}


def assert_overlap(rec, report, side):
    """Variant values must equal the golden ResultsReport's fields."""
    checks = [
        ("mean_wait_min", report["metrics"]["mean_wait_min"][side]),
        ("p90_wait_min", report["metrics"]["p90_wait_min"][side]),
        ("served_per_day", report["metrics"]["served_per_day"][side]),
        ("turned_away_per_day", report["metrics"]["turned_away_per_day"][side]),
        ("abandoned_per_day", report["metrics"]["abandoned_per_day"][side]),
        ("mean_csat", report["metrics"]["mean_csat"][side]),
        ("csat_5pt", report["metrics"]["csat_5pt"][side]),
    ]
    for field, expected in checks:
        got = rec["metrics"][field]
        if got != expected:
            raise SystemExit(
                f"ABORT: variant '{rec['key']}' field {field} = {got} does "
                f"not match golden {side} value {expected}")
    punct = report["metrics"]["appointment_punctuality"][side]
    if rec["punctuality"] != punct:
        raise SystemExit(
            f"ABORT: variant '{rec['key']}' punctuality does not match "
            f"golden {side} block")


def build_manifest(sc, preset, raw, variants, golden_sha):
    opt = raw["policy"]["optimized"]
    return _round_floats({
        "preset": preset,
        "scenario_name": sc.name,
        "customer": sc.customer,
        "notes": raw["meta"].get("notes", ""),
        "engine_version": engine_version(),
        "schema_version": raw["meta"]["schema_version"],
        "monte_carlo_days": sc.mc_days,
        "random_seed": sc.seed,
        "golden_sha256": golden_sha,
        "levers_enabled": selected_levers(sc),
        "lever_config": {
            "matching": sc.levers["matching"],
            "appointment_smoothing": sc.levers["appointment_smoothing"],
            "prep_in_queue": sc.levers["prep_in_queue"],
            "deflection": sc.levers["deflection"],
            "running_late": sc.levers["running_late"],
            "break_scheduling": sc.levers["break_scheduling"],
        },
        "weights": {"throughput": sc.weights[0], "wait": sc.weights[1],
                    "csat": sc.weights[2],
                    "preset": sc.levers["matching"].get("weight_preset")},
        "baseline_policy": raw["policy"]["baseline"],
        "punctuality_inputs": {
            "early_summon_max_min": sc.baseline.early_summon_max,
            "late_ok_min": sc.baseline.late_ok,
            "late_acceptable_min": sc.baseline.late_acceptable,
        },
        "no_show_rate": sc.no_show_rate,
        "variants": [{"key": v["key"], "label": v["label"],
                      "levers": v["levers"],
                      "path": f"variants/{v['key']}.json"}
                     for v in variants],
        "report_path": "report.json",
        "_": "matching/smoothing/prep/deflection/running_late values above "
             "are the configuration the bundle was RUN with; the workbench "
             "displays them read-only and never re-runs weights.",
    })


def write_bundle_js(preset_dir, preset, manifest, report, variants):
    payload = {
        "manifest": manifest,
        "report": report,
        "variants": {v["key"]: v for v in variants},
    }
    js = ("window.OPTLAB_DATA = window.OPTLAB_DATA || {};\n"
          f"window.OPTLAB_DATA[{json.dumps(preset)}] = "
          f"{json.dumps(payload, sort_keys=True)};\n")
    (preset_dir / "bundle.js").write_text(js)


def precompute(preset, full_grid=False, extra_levers=None):
    print(f"== {preset}")
    sc = load_scenario(REPO / "scenarios" / f"{preset}.json")
    raw = json.loads((REPO / "scenarios" / f"{preset}.json").read_text())
    golden_path = GOLDEN_DIR / f"{preset}.golden.json"
    golden_bytes = golden_path.read_bytes()
    golden = json.loads(golden_bytes)

    mc = run_scenario_mc(sc)
    report = build_report(sc, mc, deterministic=True)
    if report != golden:
        raise SystemExit(f"ABORT: regenerated report for {preset} is not "
                         f"byte-identical to the committed golden "
                         f"({golden_path}). Engine and goldens have "
                         f"diverged — resolve before bundling.")
    print("   report == committed golden: verified")

    plan_sets = sorted(set(mc.plan.values()), key=lambda fs: (len(fs), sorted(fs)))
    wanted = list(plan_sets)
    sel = selected_levers(sc)
    if full_grid:
        from itertools import combinations
        for k in range(len(sel) + 1):
            for combo in combinations(sel, k):
                fs = frozenset(combo)
                if fs not in wanted:
                    wanted.append(fs)
    if extra_levers is not None:
        fs = frozenset(extra_levers)
        unknown = fs - set(sel)
        if unknown:
            raise SystemExit(f"ABORT: levers not enabled in {preset}: "
                             f"{sorted(unknown)}")
        if fs not in wanted:
            wanted.append(fs)

    extra = [fs for fs in wanted if fs not in mc.arrays]
    arrays = dict(mc.arrays)
    if extra:
        print(f"   running {len(extra)} extra lever set(s) "
              f"(~6 min each at v1.4 quoting cost)...")
        arrays.update(run_extra_sets(sc, extra))

    base_arrays = arrays[frozenset()]
    variants = []
    for fs in wanted:
        key = set_key(fs)
        variants.append(variant_record(sc, key, fs, arrays[fs], base_arrays))
    by_key = {v["key"]: v for v in variants}
    assert_overlap(by_key["none"], golden, "baseline")
    assert_overlap(by_key[set_key(frozenset(sel))], golden, "optimized")
    print("   baseline/combined variants match golden fields: verified")

    preset_dir = DATA_DIR / preset
    (preset_dir / "variants").mkdir(parents=True, exist_ok=True)
    golden_sha = hashlib.sha256(golden_bytes).hexdigest()
    manifest = build_manifest(sc, preset, raw, variants, golden_sha)
    (preset_dir / "report.json").write_bytes(golden_bytes)  # byte-identical
    (preset_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    for v in variants:
        (preset_dir / "variants" / f"{v['key']}.json").write_text(
            json.dumps(v, indent=2, sort_keys=True) + "\n")
    write_bundle_js(preset_dir, preset, manifest, golden, variants)
    sizes = sum(p.stat().st_size for p in preset_dir.rglob("*") if p.is_file())
    print(f"   wrote {len(variants)} variants, bundle total "
          f"{sizes / 1024:.0f} KB")


def write_index():
    entries = []
    for preset, name in PRESETS.items():
        if (DATA_DIR / preset / "bundle.js").exists():
            entries.append({"key": preset, "name": name,
                            "bundle": f"data/{preset}/bundle.js"})
    js = f"window.OPTLAB_INDEX = {json.dumps(entries, indent=2)};\n"
    (DATA_DIR / "index.js").write_text(js)
    print(f"== data/index.js: {len(entries)} preset(s)")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", choices=sorted(PRESETS), default=None,
                    help="single preset (default: all three)")
    ap.add_argument("--levers", default=None,
                    help="comma-separated lever subset to add to the bundle, "
                         "e.g. --levers matching,prep_in_queue "
                         "(~6 min: one extra paired 200-day run)")
    ap.add_argument("--full-grid", action="store_true",
                    help="precompute EVERY lever subset (2^k per preset). "
                         "HONEST RUNTIME WARNING: ~6 minutes per variant at "
                         "v1.4 quoting cost; the University/DMV grids are "
                         "2^5 = 32 sets each, so the full grid is an "
                         "overnight batch, not a coffee break. Default OFF.")
    args = ap.parse_args()
    targets = [args.preset] if args.preset else sorted(PRESETS)
    extra = args.levers.split(",") if args.levers else None
    if extra and not args.preset:
        ap.error("--levers requires --preset")
    for preset in targets:
        precompute(preset, full_grid=args.full_grid, extra_levers=extra)
    write_index()


if __name__ == "__main__":
    main()
