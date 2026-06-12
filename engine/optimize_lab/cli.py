"""CLI: python -m optimize_lab run <scenario.json> [--out X] [--csv]"""
from __future__ import annotations

import argparse
import csv as csv_mod
import sys
from pathlib import Path

from .config import REPO_ROOT, load_scenario
from .montecarlo import run_scenario_mc
from .report import build_report, summary_text, write_report

CSV_HEADER = [
    "variant", "day", "visitor", "type", "service", "language",
    "checkin_or_scheduled_min", "promised_low_min", "promised_center_min",
    "promised_high_min", "start_min", "end_min", "employee", "status",
    "wait_min", "csat",
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="optimize_lab",
        description="optimize-lab reference engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run a scenario and emit a ResultsReport")
    run_p.add_argument("scenario", help="path to a ScenarioConfig JSON file")
    run_p.add_argument("--out", help="output path for the ResultsReport JSON "
                                     "(default: results/<name>.report.json)")
    run_p.add_argument("--csv", action="store_true",
                       help="also write the per-visitor audit CSV "
                            "(baseline + combined runs)")
    args = parser.parse_args(argv)

    scenario_path = Path(args.scenario)
    sc = load_scenario(scenario_path)
    stem = scenario_path.stem
    out_path = Path(args.out) if args.out else REPO_ROOT / "results" / f"{stem}.report.json"

    def progress(done, total):
        print(f"  ... {done}/{total} days", file=sys.stderr)

    mc = run_scenario_mc(sc, collect_csv=args.csv, progress=progress)

    csv_path = None
    if args.csv:
        csv_path = out_path.parent / f"{stem}.visits.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", newline="") as f:
            w = csv_mod.writer(f)
            w.writerow(CSV_HEADER)
            w.writerows(mc.csv_rows)

    report = build_report(sc, mc, csv_path=str(csv_path) if csv_path else None)
    write_report(report, out_path)
    print(summary_text(report))
    print(f"\nReport written to {out_path}")
    if csv_path:
        print(f"Audit CSV written to {csv_path}")
    return 0
