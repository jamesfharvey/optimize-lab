"""ResultsReport assembly. Output validates against schemas/results-report.v1.json.

Documented v1 reporting choices (see README for the full list):
  - Daily metrics are day-level means averaged across Monte-Carlo days; p90 is
    the within-day 90th percentile averaged across days.
  - delta_pct = (optimized - baseline) / baseline * 100; the 95% CI comes from
    the per-day PAIRED differences (normal approximation), expressed as a
    percentage of the baseline mean.
  - attribution: each selected lever's solo mean-wait reduction, proportionally
    rescaled so shares sum to ~100% of the combined gain (deliberately simple
    for v1). Equal split if the solo reductions cancel to ~zero.
  - resolved_digitally_per_day and incomplete_time_cost_min_per_day are scalar
    in the schema; both report the combined-optimized run.
  - csat_5pt and break_schedule_recommendation ride along as additional
    properties (the schema permits them).
"""
from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import __version__
from .config import REPO_ROOT, Scenario, fmt_hhmm, validate_report_dict
from .csat import to_five_point
from .levers import LEVER_LABELS, selected_levers
from .montecarlo import MCResult
from . import policies

SCHEMA_VERSION = "1.0.0"


def _engine_version() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return f"{__version__}+{out.stdout.strip()}"
    except Exception:
        pass
    return __version__


def _comparison(base: np.ndarray, opt: np.ndarray) -> dict:
    b, o = float(np.mean(base)), float(np.mean(opt))
    out = {"baseline": b, "optimized": o}
    if abs(b) > 1e-12:
        out["delta_pct"] = (o - b) / b * 100.0
        n = len(base)
        if n > 1:
            diff = opt - base
            half = 1.96 * float(np.std(diff, ddof=1)) / math.sqrt(n)
            mean_d = float(np.mean(diff))
            out["ci95_low"] = (mean_d - half) / b * 100.0
            out["ci95_high"] = (mean_d + half) / b * 100.0
    else:
        out["delta_pct"] = 0.0
    return out


def _punctuality_stats(arr: dict) -> dict:
    """Day-level punctuality stats averaged across MC days; max is the true
    maximum across all days."""
    return {
        "pct_on_time": float(np.mean(arr["pct_on_time"])),
        "pct_acceptable": float(np.mean(arr["pct_acceptable"])),
        "p50_lateness_min": float(np.mean(arr["p50_late"])),
        "p90_lateness_min": float(np.mean(arr["p90_late"])),
        "max_lateness_min": float(np.max(arr["max_late"])),
    }


def _round_floats(obj, ndigits=4):
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def _focus_recommendation(sc: Scenario) -> list:
    focus = policies.compute_focus(sc, form=None)
    out = []
    for emp in sc.employees:
        qualified = sorted(emp.profile, key=lambda s: s)
        kept = sorted(focus[emp.idx])
        parts = []
        for s_idx in kept:
            eff = emp.profile[s_idx][0]
            parts.append(f"{sc.services[s_idx].id} ({eff:.2f}x vs team avg "
                         f"{sc.mean_eff[s_idx]:.2f}x)")
        dropped = [s for s in qualified if s not in kept]
        rationale = "Focus on " + ", ".join(parts)
        if dropped:
            dparts = [f"{sc.services[s].id} ({emp.profile[s][0]:.2f}x vs "
                      f"{sc.mean_eff[s]:.2f}x)" for s in dropped]
            rationale += "; colleagues cover " + ", ".join(dparts) + " faster on average"
        rationale += "."
        out.append({
            "employee_id": emp.id,
            "qualified_services": [sc.services[s].id for s in qualified],
            "focus_services": [sc.services[s].id for s in kept],
            "rationale": rationale,
        })
    return out


def _assumption_flags(sc: Scenario, sel: list) -> list:
    flags = []
    if "prep_in_queue" in sel:
        flags.append({
            "lever": "prep_in_queue.duration_reduction",
            "assumed_value": sc.levers["prep_in_queue"]["duration_reduction"],
            "caveat": "Assumed fraction of service duration removed by pre-arrival "
                      "intake; validate against real visit anatomy per customer "
                      "before quoting externally.",
        })
        flags.append({
            "lever": "prep_in_queue.incomplete_reduction",
            "assumed_value": sc.levers["prep_in_queue"]["incomplete_reduction"],
            "caveat": "Assumed fraction of would-be incompletes caught by prep "
                      "(default midpoint of the 40-60% range); validate per customer.",
        })
    for name, value in (("wait_free_min", sc.wait_free),
                        ("wait_ref_min", sc.wait_ref),
                        ("delta_wait", sc.delta_wait),
                        ("time_floor", sc.time_floor)):
        flags.append({
            "lever": f"csat_model.{name}",
            "assumed_value": value,
            "caveat": "v1.5 absolute-wait disutility (W_time, walk-ins only): "
                      "constant is an assumption until VE.12.01 ratings allow "
                      "fitting the wait-vs-satisfaction curve per customer.",
        })
    if "deflection" in sel:
        flags.append({
            "lever": "deflection.rate",
            "assumed_value": sc.levers["deflection"]["rate"],
            "caveat": "Assumed fraction of visits resolved digitally before "
                      "arrival; reported separately, never blended into served. "
                      "Validate per customer.",
        })
    return flags


def build_report(sc: Scenario, mc: MCResult, deterministic: bool = False,
                 csv_path: str | None = None) -> dict:
    base = mc.of("baseline")
    comb = mc.of("combined")
    sel = selected_levers(sc)

    metrics = {
        "mean_wait_min": _comparison(base["mean_wait"], comb["mean_wait"]),
        "p90_wait_min": _comparison(base["p90_wait"], comb["p90_wait"]),
        "served_per_day": _comparison(base["served"], comb["served"]),
        "turned_away_per_day": _comparison(base["turned_away"], comb["turned_away"]),
        "mean_csat": _comparison(base["mean_csat"], comb["mean_csat"]),
        "makespan_min": _comparison(base["makespan"], comb["makespan"]),
        "appointment_punctuality": {
            "baseline": _punctuality_stats(base),
            "optimized": _punctuality_stats(comb),
        },
        "abandoned_per_day": _comparison(base["abandoned"], comb["abandoned"]),
        "resolved_digitally_per_day": float(np.mean(comb["resolved"])),
        "incomplete_time_cost_min_per_day": float(np.mean(comb["incomplete_min"])),
    }
    base_csat = metrics["mean_csat"]["baseline"]
    metrics["csat_5pt"] = {
        "baseline": to_five_point(base_csat, base_csat),
        "optimized": to_five_point(metrics["mean_csat"]["optimized"], base_csat),
    }

    # waterfall: baseline, then cumulative adds in canonical order
    waterfall = []
    b_served = float(np.mean(base["served"]))
    b_wait = float(np.mean(base["mean_wait"]))

    def wf_entry(step, label, fs):
        arr = mc.arrays[fs]
        served = float(np.mean(arr["served"]))
        wait = float(np.mean(arr["mean_wait"]))
        entry = {
            "step": step,
            "label": label,
            "levers_active": [lv for lv in sel if lv in fs],
            "served_per_day": served,
            "mean_wait_min": wait,
            "p90_wait_min": float(np.mean(arr["p90_wait"])),
            "mean_csat": float(np.mean(arr["mean_csat"])),
        }
        if step > 0 and b_served > 0 and b_wait > 0:
            entry["delta_vs_baseline_pct"] = {
                "served_per_day": (served - b_served) / b_served * 100.0,
                "mean_wait_min": (wait - b_wait) / b_wait * 100.0,
            }
        return entry

    waterfall.append(wf_entry(0, "Baseline (FIFO, as run today)", frozenset()))
    for k in range(1, len(sel) + 1):
        fs = frozenset(sel[:k])
        waterfall.append(wf_entry(k, LEVER_LABELS[sel[k - 1]], fs))

    # per-lever solo impact
    per_lever = []
    solo_wait_reduction = {}
    for lv in sel:
        arr = mc.arrays[frozenset([lv])]
        entry = {"lever": lv}
        s_cmp = _comparison(base["served"], arr["served"])
        w_cmp = _comparison(base["mean_wait"], arr["mean_wait"])
        p_cmp = _comparison(base["p90_wait"], arr["p90_wait"])
        entry["served_per_day_delta_pct"] = s_cmp.get("delta_pct", 0.0)
        entry["mean_wait_delta_pct"] = w_cmp.get("delta_pct", 0.0)
        entry["p90_wait_delta_pct"] = p_cmp.get("delta_pct", 0.0)
        entry["mean_csat_delta_pts"] = (float(np.mean(arr["mean_csat"]))
                                        - float(np.mean(base["mean_csat"])))
        per_lever.append(entry)
        solo_wait_reduction[lv] = b_wait - float(np.mean(arr["mean_wait"]))

    # guardrail warnings: solo lever runs that worsen p90 summon lateness.
    # Warnings only — the hard punctuality guardrail applies to the combined
    # set (asserted in tests, not here). Same 0.5-minute operational
    # tolerance as the hard gate: sub-minute p90 shifts are noise.
    guardrail_warnings = []
    base_p90_late = float(np.mean(base["p90_late"]))
    for lv in sel:
        solo_p90 = float(np.mean(mc.arrays[frozenset([lv])]["p90_late"]))
        if solo_p90 > base_p90_late + 0.5:
            guardrail_warnings.append({
                "lever": lv,
                "p90_lateness_baseline": base_p90_late,
                "p90_lateness_solo": solo_p90,
                "message": f"Solo lever '{lv}' degrades appointment p90 "
                           f"lateness vs baseline ({base_p90_late:.1f} -> "
                           f"{solo_p90:.1f} min). The combined lever set is "
                           f"the guardrail-gated configuration.",
            })

    # attribution: solo mean-wait reductions rescaled to sum to 100%
    attribution = []
    total_reduction = sum(solo_wait_reduction.values())
    for lv in sel:
        if abs(total_reduction) > 1e-9:
            share = solo_wait_reduction[lv] / total_reduction * 100.0
        else:
            share = 100.0 / len(sel) if sel else 0.0
        attribution.append({"lever": lv, "share_of_combined_gain": share})

    meta = {
        "scenario_name": sc.name,
        "schema_version": SCHEMA_VERSION,
        "engine_version": "golden" if deterministic else _engine_version(),
        "random_seed": sc.seed,
        "monte_carlo_days": sc.mc_days,
    }
    if sc.customer:
        meta["customer"] = sc.customer
    if not deterministic:
        meta["generated_at"] = datetime.now(timezone.utc).isoformat()

    report = {
        "meta": meta,
        "metrics": metrics,
        "waterfall": waterfall,
        "focus_recommendation": _focus_recommendation(sc),
        "assumption_flags": _assumption_flags(sc, sel),
        "per_lever_impact": per_lever,
        "attribution": attribution,
    }
    if guardrail_warnings:
        report["guardrail_warnings"] = guardrail_warnings
    if csv_path:
        report["raw_data_ref"] = str(csv_path)
    if mc.break_schedule:
        rec = []
        for emp in sc.employees:
            if emp.idx in mc.break_schedule:
                s0, s1 = mc.break_schedule[emp.idx]
                rec.append({
                    "employee_id": emp.id,
                    "original": {"start": fmt_hhmm(emp.brk.start),
                                 "end": fmt_hhmm(emp.brk.end)},
                    "recommended": {"start": fmt_hhmm(s0), "end": fmt_hhmm(s1)},
                })
        report["break_schedule_recommendation"] = rec

    report = _round_floats(report)
    validate_report_dict(report)
    return report


def summary_text(report: dict) -> str:
    m = report["metrics"]
    lines = [
        f"Scenario: {report['meta']['scenario_name']}",
        f"  (seed {report['meta']['random_seed']}, "
        f"{report['meta']['monte_carlo_days']} MC days)",
        f"  {'metric':<28}{'baseline':>10}{'optimized':>11}{'delta%':>9}",
    ]
    for key, label in [
        ("mean_wait_min", "mean wait (min)"),
        ("p90_wait_min", "p90 wait (min)"),
        ("served_per_day", "served / day"),
        ("turned_away_per_day", "turned away / day"),
        ("mean_csat", "predicted CSAT (0-100)"),
        ("csat_5pt", "CSAT (5-pt)"),
        ("abandoned_per_day", "abandoned / day"),
        ("makespan_min", "makespan past close (min)"),
    ]:
        c = m[key]
        delta = f"{c['delta_pct']:>8.1f}%" if "delta_pct" in c else "      —"
        lines.append(f"  {label:<28}{c['baseline']:>10.2f}{c['optimized']:>11.2f}{delta}")
    lines.append(f"  {'resolved digitally / day':<28}"
                 f"{m['resolved_digitally_per_day']:>21.2f}")
    lines.append(f"  {'incomplete cost (min/day)':<28}"
                 f"{m['incomplete_time_cost_min_per_day']:>21.2f}")
    p = m["appointment_punctuality"]
    lines.append("  appointment punctuality (baseline -> optimized):")
    for key, label, mult in [
        ("pct_on_time", "on-time (<= late_ok)", 100.0),
        ("pct_acceptable", "acceptable (<= late_acceptable)", 100.0),
        ("p50_lateness_min", "p50 lateness (min)", 1.0),
        ("p90_lateness_min", "p90 lateness (min)", 1.0),
        ("max_lateness_min", "max lateness (min)", 1.0),
    ]:
        unit = "%" if mult == 100.0 else "  "
        lines.append(f"    {label:<32}{p['baseline'][key] * mult:>8.1f}{unit}"
                     f" -> {p['optimized'][key] * mult:>7.1f}{unit}")
    for w in report.get("guardrail_warnings", []):
        lines.append(f"  GUARDRAIL WARNING: {w['message']}")
    if report.get("per_lever_impact"):
        lines.append("  per-lever solo impact (served% / wait%):")
        for e in report["per_lever_impact"]:
            lines.append(f"    {e['lever']:<24}{e['served_per_day_delta_pct']:>7.1f}%"
                         f" {e['mean_wait_delta_pct']:>7.1f}%")
    if report.get("attribution"):
        att = ", ".join(f"{a['lever']} {a['share_of_combined_gain']:.0f}%"
                        for a in report["attribution"])
        lines.append(f"  attribution of combined gain: {att}")
    return "\n".join(lines)


def write_report(report: dict, path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
