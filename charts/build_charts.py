"""Results presentation layer: lever-progression tables + comparison charts.

Read-only against the committed engine — runs each preset's paired
Monte-Carlo plan (identical per-day draws across every lever set, so
cumulative increments are attributable and the columns sum coherently),
then writes:
  charts/<key>-mean-wait-waterfall.png
  charts/<key>-served-waterfall.png      (resolved-digitally hatched on top,
                                          never blended into served)
  charts/<key>-baseline-vs-combined.png  (p90 wait, abandoned, CSAT)
  charts/<key>-punctuality.png           (p50/p90/max lateness + thresholds)
  charts/data/<key>-progression.csv      (every number behind the waterfalls)
  charts/data/<key>-solo.csv             (solo impact, order-independent)
  charts/data/<key>-comparison.csv
  charts/data/<key>-punctuality.csv
and prints the lever-progression + solo tables to stdout.

Run:  ../.venv/bin/python build_charts.py   (from charts/, or any cwd)
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

import matplotlib                                    # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                      # noqa: E402

from optimize_lab.config import load_scenario        # noqa: E402
from optimize_lab.csat import to_five_point          # noqa: E402
from optimize_lab.levers import selected_levers      # noqa: E402
from optimize_lab.montecarlo import run_scenario_mc  # noqa: E402

CHARTS = REPO / "charts"
DATA = CHARTS / "data"

GRAY, TEAL, RED, SLATE = "#9ca3af", "#0d9488", "#dc2626", "#334155"
FLAGGED = {"prep_in_queue", "deflection"}            # assumption-flagged levers
SHORT = {
    "matching": "Matching",
    "appointment_smoothing": "Smoothing",
    "prep_in_queue": "Prep ⚠",
    "deflection": "Deflection ⚠",
    "running_late": "Running Late",
    "break_scheduling": "Break sched",
}
PRESETS = [
    ("university", "University One-Stop", "preset-university-onestop.json"),
    ("dmv", "County DMV", "preset-dmv.json"),
    ("clinic", "Community Clinic", "preset-clinic.json"),
]
METRICS = [
    ("mean_wait", "mean wait (min)", False),
    ("p90_wait", "p90 wait (min)", False),
    ("served", "in-office served/day", True),
    ("resolved", "resolved digitally/day", True),
    ("abandoned", "abandoned/day", False),
    ("turned_away", "turned away/day", False),
    ("mean_csat", "predicted CSAT (0-100)", True),
    ("csat_5pt", "predicted CSAT (5-pt)", True),
    ("p90_late", "appt p90 lateness (min)", False),
]  # (field, label, higher_is_better)


def column_values(arrays, base_csat):
    out = {f: float(np.mean(arrays[f])) for f in
           ("mean_wait", "p90_wait", "served", "resolved", "abandoned",
            "turned_away", "mean_csat", "p90_late")}
    out["csat_5pt"] = to_five_point(out["mean_csat"], base_csat)
    return out


def build_progression(sc, mc):
    sel = selected_levers(sc)
    base_csat = float(np.mean(mc.of("baseline")["mean_csat"]))
    cols = [("Baseline", [], column_values(mc.of("baseline"), base_csat))]
    for k in range(1, len(sel) + 1):
        fs = frozenset(sel[:k])
        label = "+ " + SHORT[sel[k - 1]]
        if k == len(sel):
            label += "  (= COMBINED)"
        cols.append((label, sel[:k], column_values(mc.arrays[fs], base_csat)))
    return sel, cols


def solo_impacts(sc, mc):
    sel = selected_levers(sc)
    base = mc.of("baseline")
    bw, bs = np.mean(base["mean_wait"]), np.mean(base["served"])
    bp, bc = np.mean(base["p90_wait"]), np.mean(base["mean_csat"])
    rows = []
    for lv in sel:
        a = mc.arrays[frozenset([lv])]
        rows.append({
            "lever": lv,
            "served_delta_pct": (np.mean(a["served"]) - bs) / bs * 100,
            "mean_wait_delta_pct": (np.mean(a["mean_wait"]) - bw) / bw * 100,
            "p90_wait_delta_pct": (np.mean(a["p90_wait"]) - bp) / bp * 100,
            "csat_delta_pts": np.mean(a["mean_csat"]) - bc,
        })
    return rows


def fmt(v, field):
    return f"{v:.2f}" if field in ("csat_5pt",) else f"{v:.1f}"


def print_tables(name, sc, cols, solo):
    width = 26
    print(f"\n{'=' * 100}\n{name} — Cumulative progression (order-dependent, paired runs)\n")
    header = f"{'metric':<{width}}" + "".join(f"{c[0]:>24}" for c in cols)
    print(header)
    for field, label, _hib in METRICS:
        cells = [f"{fmt(cols[0][2][field], field):>24}"]
        for i in range(1, len(cols)):
            v, p = cols[i][2][field], cols[i - 1][2][field]
            cells.append(f"{fmt(v, field):>14} ({v - p:+.1f})")
        print(f"{label:<{width}}" + "".join(cells))
    last, first = cols[-1][2], cols[0][2]
    print(f"{'TOTAL (combined vs base)':<{width}}"
          f"wait {last['mean_wait'] - first['mean_wait']:+.1f} min "
          f"({(last['mean_wait'] / first['mean_wait'] - 1) * 100:+.1f}%) · "
          f"served {last['served'] - first['served']:+.1f}/day "
          f"({(last['served'] / first['served'] - 1) * 100:+.1f}%) · "
          f"CSAT {last['mean_csat'] - first['mean_csat']:+.1f} pts · "
          f"p90 lateness {last['p90_late'] - first['p90_late']:+.2f} min")
    print(f"\n{name} — Solo impact (each lever alone vs baseline, order-independent)")
    print(f"{'lever':<24}{'served Δ%':>12}{'wait Δ%':>12}{'p90 wait Δ%':>14}{'CSAT Δpts':>12}")
    for r in solo:
        print(f"{r['lever']:<24}{r['served_delta_pct']:>12.1f}{r['mean_wait_delta_pct']:>12.1f}"
              f"{r['p90_wait_delta_pct']:>14.1f}{r['csat_delta_pts']:>12.1f}")


def sign_flips(cols, solo):
    """Levers whose cumulative increment disagrees in sign with their solo
    impact (mean wait or served)."""
    flips = []
    for i in range(1, len(cols)):
        lever = cols[i][1][-1]
        s = next(r for r in solo if r["lever"] == lever)
        for field, solo_key in (("mean_wait", "mean_wait_delta_pct"),
                                ("served", "served_delta_pct")):
            inc = cols[i][2][field] - cols[i - 1][2][field]
            if abs(inc) > 0.15 and abs(s[solo_key]) > 0.1 and \
                    np.sign(inc) != np.sign(s[solo_key]):
                flips.append((lever, field, inc, s[solo_key]))
    return flips


def write_progression_csv(key, cols):
    with open(DATA / f"{key}-progression.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["step", "column", "levers_active", "metric", "value",
                    "increment_vs_prev", "delta_vs_baseline"])
        for field, label, _ in METRICS:
            for i, (clabel, levers, vals) in enumerate(cols):
                inc = "" if i == 0 else round(vals[field] - cols[i - 1][2][field], 4)
                delta = "" if i == 0 else round(vals[field] - cols[0][2][field], 4)
                w.writerow([i, clabel, "|".join(levers), label,
                            round(vals[field], 4), inc, delta])


def annotate(ax, x, y, text, color="black"):
    ax.annotate(text, (x, y), textcoords="offset points", xytext=(0, 4),
                ha="center", fontsize=8, color=color)


def waterfall(key, name, cols, field, label, higher_is_better, extra_resolved=False):
    fig, ax = plt.subplots(figsize=(9, 5))
    n = len(cols)
    base_v = cols[0][2][field]
    ax.bar(0, base_v, color=GRAY, width=0.7)
    annotate(ax, 0, base_v, f"{base_v:.1f}")
    for i in range(1, n):
        prev, cur = cols[i - 1][2][field], cols[i][2][field]
        delta = cur - prev
        improved = (delta > 0) == higher_is_better or abs(delta) < 1e-9
        color = TEAL if improved else RED
        ax.bar(i, abs(delta), bottom=min(prev, cur), color=color, width=0.7)
        annotate(ax, i, max(prev, cur), f"{delta:+.1f}", color=color)
    comb = cols[-1][2][field]
    ax.bar(n, comb, color=SLATE, width=0.7)
    annotate(ax, n, comb, f"{comb:.1f}")
    if extra_resolved:
        resolved = cols[-1][2]["resolved"]
        if resolved > 0.05:
            ax.bar(n, resolved, bottom=comb, facecolor="none",
                   edgecolor=TEAL, hatch="//", width=0.7)
            annotate(ax, n, comb + resolved,
                     f"+{resolved:.1f} resolved digitally (not served)", TEAL)
    labels = [c[0].replace("  (= COMBINED)", "") for c in cols] + ["COMBINED"]
    ax.set_xticks(range(n + 1), labels, rotation=18, ha="right", fontsize=8)
    ax.set_ylabel(label)
    ax.set_title(f"{name} — {label.split(' (')[0].title()} by Lever "
                 f"(cumulative, paired runs)")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(CHARTS / f"{key}-{'served' if extra_resolved else 'mean-wait'}-waterfall.png",
                dpi=200)
    plt.close(fig)


def comparison_chart(key, name, cols):
    base, comb = cols[0][2], cols[-1][2]
    fields = [("p90_wait", "p90 wait (min)", False),
              ("abandoned", "abandoned/day", False),
              ("mean_csat", "CSAT (0-100)", True)]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    xs = np.arange(len(fields))
    rows = []
    for j, (f, lab, hib) in enumerate(fields):
        b, c = base[f], comb[f]
        improved = (c > b) == hib or abs(c - b) < 1e-9
        ax.bar(xs[j] - 0.18, b, width=0.34, color=GRAY)
        ax.bar(xs[j] + 0.18, c, width=0.34, color=TEAL if improved else RED)
        annotate(ax, xs[j] - 0.18, b, f"{b:.1f}")
        annotate(ax, xs[j] + 0.18, c, f"{c:.1f}")
        rows.append((lab, b, c))
    ax.set_xticks(xs, [f[1] for f in fields], fontsize=9)
    ax.set_title(f"{name} — Baseline (gray) vs Combined Optimized")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(CHARTS / f"{key}-baseline-vs-combined.png", dpi=200)
    plt.close(fig)
    with open(DATA / f"{key}-comparison.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["metric", "baseline", "combined"])
        for lab, b, c in rows:
            w.writerow([lab, round(b, 4), round(c, 4)])


def punctuality_chart(key, name, sc, mc):
    b, c = mc.of("baseline"), mc.of("combined")
    stats = [("p50 lateness", np.mean(b["p50_late"]), np.mean(c["p50_late"])),
             ("p90 lateness", np.mean(b["p90_late"]), np.mean(c["p90_late"])),
             ("max lateness", np.max(b["max_late"]), np.max(c["max_late"]))]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    xs = np.arange(len(stats))
    for j, (lab, bv, cv) in enumerate(stats):
        improved = cv <= bv + 1e-9
        ax.bar(xs[j] - 0.18, bv, width=0.34, color=GRAY)
        ax.bar(xs[j] + 0.18, cv, width=0.34, color=TEAL if improved else RED)
        annotate(ax, xs[j] - 0.18, bv, f"{bv:.1f}")
        annotate(ax, xs[j] + 0.18, cv, f"{cv:.1f}")
    ax.axhline(sc.baseline.late_ok, ls="--", color=SLATE, lw=1)
    ax.axhline(sc.baseline.late_acceptable, ls="-.", color=RED, lw=1)
    ax.text(2.45, sc.baseline.late_ok, f" late_ok = {sc.baseline.late_ok}",
            fontsize=8, va="bottom", color=SLATE)
    ax.text(2.45, sc.baseline.late_acceptable,
            f" late_acceptable = {sc.baseline.late_acceptable}",
            fontsize=8, va="bottom", color=RED)
    ax.set_xticks(xs, [s[0] for s in stats], fontsize=9)
    ax.set_ylabel("minutes")
    ax.set_title(f"{name} — Appointment Lateness: Baseline (gray) vs Combined")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(CHARTS / f"{key}-punctuality.png", dpi=200)
    plt.close(fig)
    with open(DATA / f"{key}-punctuality.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["stat", "baseline", "combined", "late_ok_min",
                    "late_acceptable_min"])
        for lab, bv, cv in stats:
            w.writerow([lab, round(float(bv), 4), round(float(cv), 4),
                        sc.baseline.late_ok, sc.baseline.late_acceptable])


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    for key, name, fname in PRESETS:
        sc = load_scenario(REPO / "scenarios" / fname)
        mc = run_scenario_mc(sc)
        sel, cols = build_progression(sc, mc)
        solo = solo_impacts(sc, mc)
        print_tables(name, sc, cols, solo)
        for lever, field, inc, solo_pct in sign_flips(cols, solo):
            print(f"  SIGN FLIP — {lever} on {field}: cumulative increment "
                  f"{inc:+.1f} vs solo impact {solo_pct:+.1f}%: the lever "
                  f"interacts with the levers already applied before it "
                  f"(capacity freed or demand reshaped upstream changes what "
                  f"this lever has left to do).")
        write_progression_csv(key, cols)
        with open(DATA / f"{key}-solo.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["lever", "served_delta_pct", "mean_wait_delta_pct",
                        "p90_wait_delta_pct", "csat_delta_pts"])
            for r in solo:
                w.writerow([r["lever"], round(r["served_delta_pct"], 2),
                            round(r["mean_wait_delta_pct"], 2),
                            round(r["p90_wait_delta_pct"], 2),
                            round(r["csat_delta_pts"], 2)])
        waterfall(key, name, cols, "mean_wait", "mean wait (min)", False)
        waterfall(key, name, cols, "served", "in-office served/day", True,
                  extra_resolved=True)
        comparison_chart(key, name, cols)
        punctuality_chart(key, name, sc, mc)
        print(f"  -> charts/{key}-*.png + charts/data/{key}-*.csv written")


if __name__ == "__main__":
    main()
