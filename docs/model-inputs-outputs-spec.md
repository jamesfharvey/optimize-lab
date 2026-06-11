# Optimize-Lab — Model Inputs & Outputs Specification

**Version:** 1.0 · **Date:** June 10, 2026 · **Owner:** James Harvey
**System of record:** `optimize-lab` repository → `/docs/model-inputs-outputs-spec.md` (GitHub URL to be added when the repo is pushed)
**Scope guardrail:** optimize-lab is a standalone simulation laboratory. It is **not** the Expedeo product codebase, and no work in it may read from, write to, or reference `expedeo-prototype`.

## 1. Purpose

Optimize-lab simulates a full operating day at a service location — visitors arrive, wait, and are served minute-by-minute — and compares **baseline operation (FIFO, as the office runs today)** against a set of **optimization levers**. It exists for three audiences: the product team (Andrey builds the future staffing/optimization product from this reference), customers (load their numbers, see their benchmark vs. improvement), and investors (the validated headline story).

## 2. How a scenario works

One customer engagement = one **ScenarioConfig** file. The engine runs the baseline policy and the selected lever set over N randomized Monte-Carlo days (same demand, different draws), then emits a **ResultsReport**: every metric Before / After / change, with confidence intervals, plus per-lever attribution.

## 3. Inputs

### 3.1 Scenario identity
| Input | Meaning | Default / example |
|---|---|---|
| Scenario name | Human label for the run | "State University One-Stop — Fall Surge" |
| Customer | Name shown on branded reports (blank for presets) | — |
| Notes | Free-text context | — |

### 3.2 Location & operating hours
| Input | Meaning | Default |
|---|---|---|
| Location name | Single location per scenario in v1 | — |
| Open / Close | The simulated day | 08:00 – 17:00 |
| Last join before close | Door closes to new walk-ins this many minutes before closing | 30 min |

### 3.3 Services (one row per service)
| Input | Meaning | Example |
|---|---|---|
| Code & name | Short ID + display name | FA — Financial Aid |
| Target duration | Org-declared target length (min); efficiency is measured against it | 30 |
| Demand share | % of daily visitors wanting this service (must total 100%) | 16.7% |
| Appointment eligible | Bookable as appointment, or drop-in only | Yes |
| **Incomplete rate (optional)** | Historical % of started services that end incomplete (see §4.2) | FA: 12% |

### 3.4 Employees (one entry per employee) — ★ core net-new data
| Input | Meaning | Example |
|---|---|---|
| Name / ID | Who they are | Maria / E1 |
| Work hours | Shift; defaults to location hours | 08:00–17:00 |
| Languages | Beyond org default | Spanish |
| Service profile | Qualified services — the model never routes outside it | FA, AD, CA |
| Efficiency per service | Target ÷ this employee's actual average. **2.00 = twice as fast as target; 0.80 = 20% slower.** Daily form jitter applies on top. | Maria/FA: 1.40 |
| CSAT per service | Historical satisfaction (0–100); source: CSAT Feedback (VE.12.01) once live, estimated until then | Maria/FA: 92 |
| **Break window** | Default daily break (start–end) with variability (± min). Breaks are **staggered** across staff and treated as **hard constraints** — no summoning during a break, even in surge. | 12:00–12:30, ±15 |

★ The per-employee, per-service **efficiency × CSAT matrix** is the model's most important input and is **net-new data** that does not exist in the product today.

### 3.5 Demand
| Input | Meaning | Default / example |
|---|---|---|
| Visitors per day | Total daily arrivals | 220 |
| Day-to-day range | Optional low–high band for Monte-Carlo | 200–240 |
| Arrival shape | uniform / one surge / two surges / custom hourly | One surge |
| Surge window(s) | When + strength (× base rate) | 11:00–13:00, ×2.0 |
| Language preferences | Share requiring a language match | Spanish 5% |

### 3.6 Baseline policy — how the office runs TODAY
"Before" in every report. The model never claims credit for what the customer already does.
| Input | Meaning | Default |
|---|---|---|
| Appointment share | % of demand arriving as appointments today (0% = pure drop-in) | 20% |
| Scheduling method | capacity / round-robin (pairing at summon) vs employee-specific (pinned at booking) | round-robin |
| Early summon max | An appointment may be summoned up to this many minutes before its slot — and only once the visitor has checked in. | 10 min |
| Late OK | Punctuality promise kept: summon lateness (summon − scheduled, floored at 0) within this counts as on-time. Also bounds the collision rule: a drop-in that would overrun an upcoming appointment is never started unless the appointment still starts within Late OK. | 5 min |
| Late acceptable | Tolerated on bad days, never the norm. Beyond this a start is unacceptable: the CSAT lateness penalty grows convexly and the punctuality guardrail counts it against the office. | 15 min |
| Appointment spread | Booked times spread evenly or follow the rush | Even |
| No-show rate | % of appointments that don't show | 8% |

## 4. Behavioral realities modeled

### 4.1 Employee breaks
Variable in practice (trades happen), staggered by design, and **hard constraints** — honored even during surges (labor agreements / public-sector norms). Capacity drops during break windows; the simulation reflects it.

### 4.2 Incomplete transactions
**Definition:** visitor is summoned, appears, service is started — and during service it's determined the transaction cannot complete (typical reasons: missing ID, missing documentation, wrong service, fee refusal; reasons are customer-configurable). The employee marks it incomplete and summons the next visitor. **The visit closes — incompletes do not re-queue.** (Re-queue applies only to summon no-shows, governed by the product's defer configuration.)
Model treatment: incompletes consume real employee minutes before discovery — time not spent on completable visits. Input is an optional per-service incomplete %. **Prep-in-queue reduces incompletes** (default assumption: 40–60% of would-be incompletes are caught before summoning) ⚠ assumption — validate per customer.
Out of scope for v1: recommending incomplete-reduction targets **by reason** — the model lacks causal data to do this credibly. Revisit (v2) once multi-customer observational data exists.

### 4.3 Abandonment
Visitor checks in, waits, and leaves without being served. Modeled as a **computed consequence of predicted wait time**, not a fixed input — conservative curve in v1 (low single digits at short waits rising to a ~12–15% ceiling at 90+ minute waits). Effect is **throughput-only** (abandoned visits produce no CSAT record). "Pre-abandonment" (not joining because the displayed line looks long) is acknowledged but out of scope.

## 5. Optimization levers (ordered by expected impact)

Each lever toggles **independently**. Reporting shows each selected lever **solo vs. baseline**, the **combined** impact of the selected set, and **attribution** (how much of the combined gain each lever contributes).

1. **Focus matching & smart routing** — focus each employee on services where they're at/above team average; route each summon by a blended score; an **aging cap** (default 45 min) guarantees no visitor waits past the cap regardless of score.
2. **Appointment smoothing** — shift demand out of the walk-in surge into evenly spread appointment slots, up to a target share.
3. **Prep-in-queue** ⚠ — pre-arrival intake (forms, documents, ID) shortens service durations (default −15%) and reduces incompletes (§4.2). The wait does work instead of just holding people.
4. **Deflection** ⚠ — a share of visits (default 10%) resolved digitally before arrival; reported separately, never blended into "served."
5. **Running Late** — converts a share of would-be appointment no-shows into served visits (default 15%; maps to product feature VE.11.01).
6. **Break-schedule optimization** *(optional)* — recommends a break stagger pattern fitted to the day's demand shape; impact reported like any other lever.

⚠ = assumption-flagged: defaults are estimates that must be validated per customer before quoting externally.

### Advanced settings (hidden by default)
- **Routing weights** — how routing trades speed vs fairness vs satisfaction. Presets: **wait-dominant (default, 0.6/0.4/0.0)**, balanced, fairness-first; custom tuning allowed. Evidence note: the full weight sweep moved mean wait by only ~3 points across all weightings — the levers above dominate results; weights are a fine-tune.
- **Simulation settings** — see §7.

## 6. Outputs (ResultsReport)

**Core metrics — always reported, each Before / After / Δ / 95% CI:**
mean wait (min) · p90 wait (min) · served per day · turned away per day · predicted CSAT (0–100, plus 5-point translation).

**Additional metrics:** incomplete time cost (employee-minutes/day lost to incompletes) · abandonment count/day · resolved-digitally/day (deflection, reported separately) · makespan · **appointment punctuality block (guardrail)**.

**Appointment punctuality (replaces the binary on-time rate).** Lateness = max(0, summon − scheduled), over summoned appointments. Reported baseline vs optimized: `pct_on_time` (lateness ≤ Late OK), `pct_acceptable` (lateness ≤ Late acceptable), `p50_lateness_min`, `p90_lateness_min`, `max_lateness_min`. Percentage denominators are SHOWN appointments, so a shown appointment never summoned counts against both percentages. **Guardrail:** hard requirement on the COMBINED selected lever set only — optimized p90 lateness must be ≤ Late acceptable AND must not exceed baseline p90 lateness. Solo lever runs that degrade p90 lateness emit a **guardrail warning** entry in the ResultsReport (a real finding, not a failure — e.g. appointment smoothing alone strains punctuality in appointment-led shops until the other levers relieve capacity).

**CSAT × lateness (appointments).** For appointment visitors the linear promise-accuracy penalty is replaced by a kinked-convex curve driven by the punctuality inputs: no penalty within Late OK (early summons: no penalty, no bonus); a mild linear ramp from Late OK to Late acceptable; beyond Late acceptable the penalty grows quadratically, floor-capped. The penalty is multiplicative on Base(e,s) and the duration term, so a high-CSAT, faster-than-target employee partially recovers a late start — intentional. ⚠ The curve SHAPE is operationally grounded; its STEEPNESS constants are assumptions until CSAT Feedback (VE.12.01) produces real lateness-vs-rating pairs, at which point the curve becomes fittable from customer data.

**Attribution:** per-lever solo impact · combined impact of the selected set · contribution share of each lever.

**Focus recommendation:** per employee — qualified services vs. recommended focus, with plain-language rationale grounded only in the structured data (per the AI Content Principles in the Capacity Management brief §6).

**Assumption flags:** every ⚠ lever active in a run is restated with its caveat; no report ships without its assumptions attached.

## 7. Simulation method

Discrete-event simulation of the operating day (arrivals → queue → summon → serve), run over N Monte-Carlo days with daily variation. Defaults: 200 days · daily form jitter ±12% (each person's speed today vs. their average) · fixed random seed 42 for reproducibility. **Golden test vectors** generated from the University preset gate parity between the Python reference engine and any other implementation (workbench).

**Validation anchor environment.** The session-validated reference band (baseline 185 ± 5 served / 43.3 ± 3.0 min wait; optimized 202 ± 5 / 26.2 ± 3.0; matching lever only, seed 42, 200 days) was produced in a different *environment* than the University preset, not by a different engine: an 08:00–16:00 day (480 minutes), door open to joins until close (last-join 0), no language-preference constraints, pure drop-in (appointment share 0), abandonment disabled, demand pinned at 220/day (independent of any preset recalibration). The preset itself keeps its 08:00–17:00 day, 30-minute last-join window and 5% Spanish-preference share, so preset outputs are not expected to match the anchor numbers. The anchor config is encoded in `engine/tests/test_golden.py::validation_config` and asserted on every test run.

## 8. Presets

| | University One-Stop (reference) | County DMV | Community Clinic |
|---|---|---|---|
| Demand/day | 255 (recalibrated from 220, June 11 2026) | 400 (recalibrated from 320, June 11 2026) | 180 |
| Surge | 11:00–13:00 ×2 | 8–10 ×1.8 + 12–13:30 ×1.6 | 8:30–10:30 ×1.7 + 15–16:30 ×1.5 |
| Baseline appointment share | 20% | 10% | 60% |
| Scheduling method | round-robin | capacity | employee-specific |
| Punctuality (early / late OK / late acceptable) | 10 / 5 / 15 | 10 / 5 / 15 | 10 / 5 / 10 (stricter shop) |
| Notes | Reference world; golden vectors; anchor pins 220/day independently | Ticket culture; protected Road Test; deflection trimmed to 0.10; in-office served target unreachable (see v1.2 log) | Appointment-led; punctuality binds hardest |

## 9. Out of scope (v1) / deferred

Service groups · multi-location · reason-based incomplete-reduction recommendations (v2, after cross-customer data) · pre-abandonment effects · capacity-plan modeling (governed by the product's Capacity Management capability, not the lab).

## 10. Change log

- **v1.2 — June 11, 2026.** Preset recalibration (scenario data only; engine/schemas/tests untouched). University demand 220 → 255/day; DMV demand 320 → 400/day with deflection trimmed 0.15 → 0.10 — both restore the oversubscribed-surge regime so the demo baseline shows visible pain and the optimized queue backfills deflected visits. University meets all calibration targets. DMV best-feasible: the in-office served target (combined ≥ baseline − 2%) is unreachable within scenario-data knobs at credible baseline waits — the smoothing no-show drain (~4% of demand) plus the 10% deflection floor exceeds baseline losses whenever baseline mean wait stays within 15–30 min; committed point trades a ~9% in-office served dip (plus ~40/day resolved digitally) for an in-band baseline. Validation anchor pins 220/day, unaffected.
- **v1.1 — June 11, 2026.** Punctuality model: the symmetric appointment grace window is replaced by three customer-configurable inputs (early summon max / late OK / late acceptable); the binary on-time rate is replaced by a punctuality output block (pct on-time, pct acceptable, p50/p90/max lateness); guardrail scoped to the combined lever set with solo-lever degradations reported as warnings; kinked-convex CSAT lateness curve for appointments (shape grounded, steepness assumption until VE.12.01 data); validation anchor environment documented (§7).
- **v1.0 — June 10, 2026.** Initial specification. Incorporates the June 10 working session: employee breaks (input + optional optimization lever), incomplete transactions (optional input, time-cost metric, prep linkage), abandonment (computed from predicted wait, conservative curve), levers reordered by impact, routing weights demoted to Advanced, and solo + combined + attribution reporting structure.
