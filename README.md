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
and sets `raw_data_ref`. Golden vectors, once signed off, regenerate with
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
  their scheduled time; their CSAT grace is the appointment grace window.
- **CSAT** per served visit: `Base(e,s) × W_wait × W_accuracy × W_duration`
  with `W_wait = 1 − α·min(1, max(0, wait − promised)/60)`,
  `W_accuracy = 1 − β·min(1, |wait − promised|/60)`,
  `W_duration = 1 − γ·min(1, max(0, actual/target − 1))`; factors clamped to
  [0, 1]. α/β/γ from `simulation.csat_model`.
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
   eligible employees.
3. **Breaks**: the daily ±variability shift is rounded to 5 minutes and the
   window is clamped inside the shift, duration preserved. A service running
   into the break delays it; the remaining window (possibly zero) is then
   taken — the break still ends at its window end.
4. **No-shows** occupy the books (and create collision obligations) until
   their scheduled time, then drop — the office can't know earlier.
   Running-Late conversions arrive at their scheduled time.
5. **Appointment arrivals**: shown appointments check in at `scheduled −
   grace` and may be summoned from then; summoned later than `scheduled +
   grace` counts late. On-time denominator = shown appointments; shown but
   never summoned counts late. Days with no appointments report rate 1.0.
6. **Booking window**: appointments book within `[open, close − last_join]`,
   rounded to 5 minutes; `even` slots are assigned in `u_slot` order;
   smoothing forces the `even` distribution.
7. **Incompletes** are not counted as served and produce no wait/CSAT record;
   they consume `U(0.25, 0.6) × effective duration` and accrue to the time
   cost. Schema scalars `incomplete_time_cost_min_per_day` and
   `resolved_digitally_per_day` report the combined-optimized run.
8. **Optimized routing** falls back to the employee's full qualified profile
   when no focused candidate is waiting (work-conserving); the aging-cap pool
   is also the full qualified profile. Language and collision always bind.
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

### Validation status (read before trusting preset numbers)

The session-validated anchor band (University, appointment_share 0,
abandonment off, matching only, 200 days, seed 42 — baseline 185±4 served /
43.3±2.5 min wait; optimized 202±4 / 26.2±2.5) **does not fully reproduce**
under the normative effective-duration rule, and per the build prompt the
engine was not tuned to force it. Engine results: baseline 206.2 served /
42.60 wait (wait in band, served above); optimized 208.4 / 35.44.
Diagnosis: with employees serving at their true efficiency-scaled speeds in
BOTH policies, baseline FIFO already captures most of the efficiency upside
through utilization (fast employees free up more often). The anchor's 185 is
exactly total capacity ÷ mean *target* duration (3240/17.51), i.e. a baseline
world running at target speed with the efficiency matrix unlocked only by
optimization. Which baseline the lab should model is a spec-owner decision;
`tests/test_golden.py` encodes the band as a strict xfail and goldens are
withheld until the call is made. Related open item: appointment-smoothing
SOLO degrades the Clinic on-time rate (0.799 → 0.737) while the combined
stack improves it (0.830) — the strict "any lever set" guardrail reading is
also encoded as an xfail.
