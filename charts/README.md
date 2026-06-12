# charts/ — results presentation layer

> **v1.5 note:** CSAT figures include the absolute-wait disutility term
> (W_time — long waits cost satisfaction even when accurately promised).
> **v1.4 note:** CSAT figures reflect the policy-aware, arrival-aware
> dispatch-forward check-in quote with promise ranges and asymmetric
> accuracy (spec v1.4). Operational figures (waits, served, lateness) are
> unchanged from v1.3 — the quote is informational only.

Read-only over the committed engine: `build_charts.py` re-runs each preset's
full Monte-Carlo plan and renders the comparison set below. **Paired-run
method:** every lever column is simulated against the *identical* per-day
draws (arrivals, no-shows, daily form, breaks — seeded `(seed, day)`), so each
cumulative increment is attributable to exactly the lever added in that
column and the columns sum coherently to the combined result. Cumulative
columns follow the spec's canonical impact order (enabled levers only).
Conventions: baseline gray, improvements teal, degradations red, combined
dark slate, ⚠ marks assumption-flagged levers (prep, deflection). PNGs at
200 dpi; every number behind every chart is in `data/*.csv`.

Rebuild: `../.venv/bin/python build_charts.py`

Per preset (`university`, `dmv`, `clinic`):

- `<key>-mean-wait-waterfall.png` — mean wait: baseline → one floating bar
  per lever increment → combined.
- `<key>-served-waterfall.png` — in-office served/day, same structure; the
  hatched segment on the combined bar is resolved-digitally demand, drawn on
  top and never blended into served.
- `<key>-baseline-vs-combined.png` — grouped bars, baseline vs combined:
  p90 wait, abandoned/day, predicted CSAT (0–100).
- `<key>-punctuality.png` — appointment lateness p50/p90/max, baseline vs
  combined, with the scenario's `late_ok` and `late_acceptable` thresholds
  as horizontal lines.

`data/` per preset: `-progression.csv` (every metric per cumulative column,
with increments and deltas vs baseline), `-solo.csv` (each lever alone vs
baseline — order-independent; answers a different question than the
progression and is deliberately not reconciled with it), `-comparison.csv`,
`-punctuality.csv`.
