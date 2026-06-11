# optimize-lab

Standalone simulation laboratory: baseline FIFO operation vs. AI optimization
levers for queue + appointment service operations. **Not part of any product
codebase.** See `CLAUDE.md` for session guardrails and `docs/model-inputs-outputs-spec.md`
for the full model specification.

- `schemas/` — ScenarioConfig (inputs) and ResultsReport (outputs) contracts
- `scenarios/` — preset + customer scenario files (one engagement = one file)
- `docs/` — model spec (mirrored to Notion: Documents & Resources)
- `engine/` — Python reference engine (Phase 2)
- `workbench/` — interactive HTML app (Phase 3)

## Engine (Phase 2)

### Quickstart

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd engine
../.venv/bin/python -m optimize_lab run ../scenarios/preset-university-onestop.json --csv
../.venv/bin/python -m pytest tests/
```

Reports land in `results/` (gitignored) by default; `--out` overrides,
`--csv` also writes the per-visitor audit trail (baseline + combined runs)
and sets `raw_data_ref`. Golden vectors live in `engine/tests/golden/` and
are byte-exact; regenerate deliberately with
`OPTLAB_REGEN_GOLDEN=1 pytest tests/test_golden.py`.

### How a run works

The engine simulates each operating day as a discrete-event loop (arrivals →
queue → summon → serve) and runs the day plan over N Monte-Carlo days. All
randomness for a day is pre-drawn once (`world.DayDraws`, seeded
`(random_seed, day)`), so every policy variant consumes **identical**
arrival / no-show / form / break draws — the Monte-Carlo is fully paired.
Per scenario it runs: baseline, each selected lever solo, the cumulative
prefixes in canonical lever order (for the waterfall), and the combined set
(identical lever sets are simulated once).

### Documented formulas (engine-defined where the spec left latitude)

- **Promised wait** (walk-in check-in forecast, also the CSAT grace):
  `(remaining minutes of in-progress services + Σ queue-ahead nominal
  durations) / max(1, employees on shift and not on break)`, where the
  nominal duration of a queued visitor is `target / team-mean efficiency`
  (form and prep are unknown to the forecaster). Appointments are promised
  their scheduled time; their CSAT penalty is the punctuality lateness curve.
- **CSAT, walk-ins**: `Base(e,s) × W_wait × W_accuracy × W_duration`
  with `W_wait = 1 − α·min(1, max(0, wait − promised)/60)`,
  `W_accuracy = 1 − β·min(1, |wait − promised|/60)`,
  `W_duration = 1 − γ·min(1, max(0, actual/target − 1))`; factors clamped to
  [0, 1]. α/β/γ from `simulation.csat_model`.
- **CSAT, appointments**: `Base(e,s) × W_punctuality × W_duration` where
  `W_punctuality` is kinked-convex in lateness L = max(0, summon − scheduled):
  1.0 for L ≤ late_ok; linear ramp to 0.90 at late_acceptable; then
  `0.90 − 0.90·((L − late_acceptable)/60)²`, floored at 0. Early summons: no
  penalty, no bonus. Multiplicative on Base and duration, so a high-CSAT,
  faster-than-target employee partially recovers a late start (intentional).
  ⚠ Curve shape is grounded; steepness constants (`RAMP_DEPTH`,
  `CONVEX_HORIZON` in csat.py) are assumptions until VE.12.01 data makes the
  curve fittable.
- **5-point translation**: `5 − ((100 − c)/(100 − c_baseline))^1.5`, anchored
  so the baseline mean is exactly 4.0, flattening near the ceiling; clamped
  to [1, 5]. Reported as `metrics.csat_5pt`.
- **Attribution**: each selected lever's solo mean-wait reduction,
  proportionally rescaled so shares sum to ~100% of the combined gain
  (deliberately simple for v1; negative shares are possible if a lever's
  solo run worsens mean wait; equal split if the reductions cancel to ~0).
- **Abandonment inverse**: patience drawn once at arrival by inverting the
  conservative_v1 CDF; 85% of visitors never abandon.

### v1 interpretation choices (flagged deviations / clarifications)

1. `engine/optimize_lab/__main__.py` added beyond the prescribed tree —
   required for `python -m optimize_lab`.
2. **Collision forecast**: under `capacity`/`round_robin`, upcoming
   appointments are greedily assigned (booked-time order, fewest-first) to
   eligible employees to define `next_appt_start(e)`; pairing still happens
   at summon time. `capacity` and `round_robin` behave identically at summon
   in v1. The all-blocked exception is evaluated among currently-free
   eligible employees and is bounded by `late_ok_min` (the appointment must
   still start within the punctuality promise).
3. **Breaks**: the daily ±variability shift is rounded to 5 minutes and the
   window is clamped inside the shift, duration preserved. A service running
   into the break delays it; the remaining window (possibly zero) is then
   taken — the break still ends at its window end.
4. **No-shows** occupy the books (and create collision obligations) until
   their scheduled time, then drop — the office can't know earlier.
   Running-Late conversions arrive at their scheduled time.
5. **Appointment arrivals**: shown appointments check in at `scheduled −
   early_summon_max_min` and may be summoned from then on — never before
   check-in. Lateness = max(0, summon − scheduled); `pct_on_time` /
   `pct_acceptable` denominators are SHOWN appointments (shown-but-never-
   summoned counts against both); lateness percentiles are over summoned
   appointments. Days with no appointments report both percentages as 1.0.
6. **Booking window**: appointments book within `[open, close − last_join]`,
   rounded to 5 minutes; `even` slots are assigned in `u_slot` order;
   smoothing forces the `even` distribution.
7. **Incompletes** are not counted as served and produce no wait/CSAT record;
   they consume `U(0.25, 0.6) × effective duration` and accrue to the time
   cost. Schema scalars `incomplete_time_cost_min_per_day` and
   `resolved_digitally_per_day` report the combined-optimized run.
8. **Optimized routing** picks the candidate pool first — focused services,
   falling back to the full qualified profile when no focused candidate is
   waiting (work-conserving) — and applies the aging cap WITHIN that pool
   (longest-first override of the blended score). Scoping the cap to the
   routed pool preserves specialization under load; aged visitors of
   out-of-focus services are rescued by the focus coverage guarantee plus
   the fallback. Language and collision always bind.
9. **Walk-in generation**: arrivals are generated inside the joinable window
   `[open, close − last_join]` (the arrival shape is normalized over it), so
   nobody is door-blocked in-model; `turned_away_per_day` = still waiting at
   close (walk-ins and never-summoned shown appointments).
10. **Structurally unservable demand**: language preference is a hard filter,
    so a language×service combination with no qualified speaker (e.g. es +
    Advising in the University preset, es + Road Test/Title Transfer in DMV,
    es + New Patient in Clinic) waits until turned away/abandoned, in both
    policies alike. This is a consequence of the locked presets, not a bug.
11. **Break-scheduling lever**: greedy per-employee grid search (15-minute
    steps within the shift, duration preserved) minimizing mean wait over 20
    paired evaluation days under the baseline policy; daily break variability
    still applies to the recommended windows. Recommendation is reported as
    `break_schedule_recommendation`.

### Validation status

**Resolved 2026-06-11.** The original mismatch was environmental, not
mechanical: the session-validated band was produced on an 08:00–16:00 day
with the door open to joins until close, no language constraints, pure
drop-in and abandonment disabled — not on the University preset's
08:00–17:00 / 30-min-last-join / 5%-Spanish world. With the anchor rebuilt
to that environment (and the aging cap correctly scoped to the routed
candidate pool — see deviation 8), the engine lands all four numbers in
band with NO change to the normative effective-duration formula
(efficiency applies in both policies, as decided):

| | band | engine |
|---|---|---|
| arrivals/day | 220 ± 5 | 220.0 |
| baseline served/day | 185 ± 5 | 185.1 |
| baseline mean wait | 43.3 ± 3.0 | 44.89 |
| optimized served/day | 202 ± 5 | 201.7 |
| optimized mean wait | 26.2 ± 3.0 | 26.67 |

The anchor is asserted on every test run
(`test_golden.py::test_validation_anchor_band`); goldens for all three
presets are committed and byte-exact
(`OPTLAB_REGEN_GOLDEN=1 pytest` to regenerate deliberately).

**Punctuality guardrail (decision 2026-06-11):** hard gate on the COMBINED
lever set only — optimized p90 lateness ≤ `late_acceptable_min` AND not
worse than baseline (0.5-min operational tolerance). Solo-lever
degradations surface as `guardrail_warnings` in the ResultsReport. The
Clinic finding stands and is asserted as a warning: appointment smoothing
SOLO pushes p90 lateness from ~12.8 to ~23.4 min (share 0.60 → 0.80 under
employee-specific pinning), while the combined stack lands at ~8.3 min —
inside the clinic's 10-minute acceptable limit and better than baseline.
