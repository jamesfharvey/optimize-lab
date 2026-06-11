# optimize-lab — Session Guardrails (READ FIRST)

This is **optimize-lab**, a standalone simulation laboratory for queue/appointment
routing optimization research.

## Hard rules
1. This is **NOT** the Expedeo product codebase. It is NOT expedeo-prototype.
2. **Never** read from, write to, reference, or open `expedeo-prototype` or any
   Expedeo product repository or folder. No product feature work happens here.
3. If a task appears to require product code, STOP and ask James instead.
4. Working directory is `~/optimize-lab/` only.

## Orientation (read in this order)
1. `docs/model-inputs-outputs-spec.md` — the normative model specification.
2. `schemas/scenario-config.v1.json` — input contract (authoritative; do not redesign).
3. `schemas/results-report.v1.json` — output contract (authoritative; do not redesign).
4. `scenarios/preset-*.json` — three validated preset scenarios.

## Standards
- Python 3.11+, numpy only (no heavy frameworks). Deterministic seeding everywhere.
- Schemas and presets are LOCKED inputs: build to them, don't rewrite them.
- Golden test vectors (seed 42, University preset) gate every change: if goldens
  shift, the change must explain why in the commit message.
