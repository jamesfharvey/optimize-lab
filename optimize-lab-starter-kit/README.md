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
