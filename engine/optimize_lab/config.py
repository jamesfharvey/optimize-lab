"""Scenario loading: JSON-schema validation, default application, normalization.

The schema at schemas/scenario-config.v1.json is the authoritative contract.
jsonschema does not inject defaults, so every default declared there is
applied explicitly here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_SCHEMA_PATH = REPO_ROOT / "schemas" / "scenario-config.v1.json"
RESULTS_SCHEMA_PATH = REPO_ROOT / "schemas" / "results-report.v1.json"

WEIGHT_PRESETS = {
    "wait_dominant": (0.6, 0.4, 0.0),
    "balanced": (0.34, 0.33, 0.33),
    "fairness_first": (0.0, 0.9, 0.1),
}

LEVER_ORDER = [
    "matching",
    "appointment_smoothing",
    "prep_in_queue",
    "deflection",
    "running_late",
    "break_scheduling",
]

DEFAULT_CSAT_WHEN_MISSING = 75.0


def parse_hhmm(s: str) -> int:
    """'08:30' -> minutes from midnight."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def fmt_hhmm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


@dataclass(frozen=True)
class Service:
    idx: int
    id: str
    name: str
    target: float
    share: float
    appt_eligible: bool
    incomplete_rate: float


@dataclass(frozen=True)
class BreakWindow:
    start: int          # minutes from midnight
    end: int
    variability: int


@dataclass(frozen=True)
class Employee:
    idx: int
    id: str
    name: str
    languages: frozenset
    work_start: int
    work_end: int
    profile: dict        # service idx -> (efficiency, csat)
    brk: BreakWindow | None


@dataclass(frozen=True)
class BaselinePolicy:
    appointment_share: float
    scheduling_method: str
    early_summon_max: int     # may summon this early, only once checked in
    late_ok: int              # promise kept within this (on-time)
    late_acceptable: int      # tolerated on bad days, never the norm
    distribution: str


@dataclass
class Scenario:
    raw: dict
    name: str
    customer: str | None
    open_min: int
    close_min: int
    last_join: int
    cutoff: int                       # latest join time = close - last_join
    services: list
    employees: list
    visitors_per_day: int
    visitors_range: tuple | None
    arrival_pattern: dict
    lang_prefs: list                  # [(language, share)]
    baseline: BaselinePolicy
    levers: dict                      # normalized optimized-lever config
    no_show_rate: float
    mc_days: int
    jitter: float
    seed: int
    alpha_wait: float
    beta_early: float          # v1.4: mild early-side accuracy penalty (ASSUMPTION)
    beta_late: float           # v1.4: late-side accuracy penalty (legacy beta_accuracy)
    range_k: float             # v1.4: promise band = max(2, range_k x center) (ASSUMPTION)
    gamma_duration: float
    abandonment_enabled: bool
    weights: tuple                    # (throughput, wait, csat)
    aging_cap: int
    service_index: dict = field(default_factory=dict)
    mean_eff: list = field(default_factory=list)      # team mean efficiency per service
    eligible_emps: list = field(default_factory=list)  # per service: [employee idx]

    @property
    def n_services(self) -> int:
        return len(self.services)

    @property
    def n_employees(self) -> int:
        return len(self.employees)


def _load_schema(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def validate_scenario_dict(data: dict) -> None:
    schema = _load_schema(SCENARIO_SCHEMA_PATH)
    jsonschema.Draft202012Validator(schema).validate(data)


def validate_report_dict(data: dict) -> None:
    schema = _load_schema(RESULTS_SCHEMA_PATH)
    jsonschema.Draft202012Validator(schema).validate(data)


def _normalize_levers(opt: dict) -> dict:
    """Apply schema defaults to policy.optimized."""
    matching = opt.get("matching", {})
    smoothing = opt.get("appointment_smoothing", {})
    prep = opt.get("prep_in_queue", {})
    deflection = opt.get("deflection", {})
    running_late = opt.get("running_late", {})
    break_sched = opt.get("break_scheduling", {})
    return {
        "matching": {
            "enabled": matching.get("enabled", True),
            "aging_cap_min": matching.get("aging_cap_min", 45),
            "weight_preset": matching.get("weight_preset", "wait_dominant"),
            "weights": matching.get("weights"),
        },
        "appointment_smoothing": {
            "enabled": smoothing.get("enabled", False),
            "target_appointment_share": smoothing.get("target_appointment_share", 0.75),
        },
        "prep_in_queue": {
            "enabled": prep.get("enabled", False),
            "duration_reduction": prep.get("duration_reduction", 0.15),
            "incomplete_reduction": prep.get("incomplete_reduction", 0.5),
        },
        "deflection": {
            "enabled": deflection.get("enabled", False),
            "rate": deflection.get("rate", 0.02),
        },
        "running_late": {
            "enabled": running_late.get("enabled", False),
            "no_show_reduction": running_late.get("no_show_reduction", 0.15),
        },
        "break_scheduling": {
            "enabled": break_sched.get("enabled", False),
        },
    }


def load_scenario(source) -> Scenario:
    """Load and validate a ScenarioConfig from a path or an already-parsed dict."""
    if isinstance(source, (str, Path)):
        with open(source) as f:
            data = json.load(f)
    else:
        data = source

    validate_scenario_dict(data)

    loc = data["location"]
    open_min = parse_hhmm(loc["open"])
    close_min = parse_hhmm(loc["close"])
    if close_min <= open_min:
        raise ValueError("location.close must be after location.open")
    last_join = loc.get("last_join_minutes_before_close", 30)
    cutoff = close_min - last_join
    if cutoff <= open_min:
        raise ValueError("last_join_minutes_before_close leaves no joinable window")

    services = []
    service_index = {}
    for i, s in enumerate(data["services"]):
        if s["id"] in service_index:
            raise ValueError(f"duplicate service id {s['id']}")
        service_index[s["id"]] = i
        services.append(Service(
            idx=i, id=s["id"], name=s["name"],
            target=float(s["target_duration_min"]),
            share=float(s["demand_share"]),
            appt_eligible=s.get("appointment_eligible", True),
            incomplete_rate=float(s.get("incomplete_rate", 0.0)),
        ))
    total_share = sum(s.share for s in services)
    if abs(total_share - 1.0) > 1e-6:
        raise ValueError(f"service demand_share values sum to {total_share}, expected 1.0")

    employees = []
    for i, e in enumerate(data["employees"]):
        profile = {}
        for p in e["profile"]:
            sid = p["service_id"]
            if sid not in service_index:
                raise ValueError(f"employee {e['id']} references unknown service {sid}")
            profile[service_index[sid]] = (
                float(p["efficiency"]),
                float(p.get("csat", DEFAULT_CSAT_WHEN_MISSING)),
            )
        brk = None
        bw = e.get("break_window")
        if bw and bw.get("start") and bw.get("end"):
            brk = BreakWindow(
                start=parse_hhmm(bw["start"]),
                end=parse_hhmm(bw["end"]),
                variability=bw.get("variability_min", 15),
            )
        employees.append(Employee(
            idx=i, id=e["id"], name=e["name"],
            languages=frozenset(e.get("languages", [])),
            work_start=parse_hhmm(e["work_start"]) if e.get("work_start") else open_min,
            work_end=parse_hhmm(e["work_end"]) if e.get("work_end") else close_min,
            profile=profile,
            brk=brk,
        ))

    demand = data["demand"]
    rng_range = None
    if demand.get("visitors_per_day_range"):
        r = demand["visitors_per_day_range"]
        rng_range = (int(r["low"]), int(r["high"]))

    pol = data["policy"]
    base = pol["baseline"]
    punct = base.get("appointment_punctuality", {})
    baseline = BaselinePolicy(
        appointment_share=float(base["appointment_share"]),
        scheduling_method=base.get("scheduling_method", "round_robin"),
        early_summon_max=int(punct.get("early_summon_max_min", 10)),
        late_ok=int(punct.get("late_ok_min", 5)),
        late_acceptable=int(punct.get("late_acceptable_min", 15)),
        distribution=base.get("appointment_distribution", "even"),
    )
    if baseline.late_acceptable < baseline.late_ok:
        raise ValueError("late_acceptable_min must be >= late_ok_min")
    levers = _normalize_levers(pol.get("optimized", {}))

    sim = data.get("simulation", {})
    csat_model = sim.get("csat_model", {})
    aband = sim.get("abandonment_model", {})

    matching = levers["matching"]
    if matching["weights"]:
        w = matching["weights"]
        weights = (w.get("throughput", 0.6), w.get("wait", 0.4), w.get("csat", 0.0))
    else:
        weights = WEIGHT_PRESETS[matching["weight_preset"]]
    if abs(sum(weights) - 1.0) > 1e-6:
        raise ValueError(f"routing weights must sum to 1, got {weights}")

    sc = Scenario(
        raw=data,
        name=data["meta"]["scenario_name"],
        customer=data["meta"].get("customer"),
        open_min=open_min,
        close_min=close_min,
        last_join=last_join,
        cutoff=cutoff,
        services=services,
        employees=employees,
        visitors_per_day=int(demand["visitors_per_day"]),
        visitors_range=rng_range,
        arrival_pattern=demand["arrival_pattern"],
        lang_prefs=[(lp["language"], float(lp["share"]))
                    for lp in demand.get("language_preferences", [])],
        baseline=baseline,
        levers=levers,
        no_show_rate=float(pol.get("no_show_rate", 0.08)),
        mc_days=int(sim.get("monte_carlo_days", 200)),
        jitter=float(sim.get("daily_form_jitter", 0.12)),
        seed=int(sim.get("random_seed", 42)),
        alpha_wait=float(csat_model.get("alpha_wait", 0.6)),
        beta_early=float(csat_model.get("beta_early", 0.1)),
        beta_late=float(csat_model.get("beta_late",
                                       csat_model.get("beta_accuracy", 0.4))),
        range_k=float(csat_model.get("range_k", 0.15)),
        gamma_duration=float(csat_model.get("gamma_duration", 0.3)),
        abandonment_enabled=aband.get("enabled", True),
        weights=weights,
        aging_cap=matching["aging_cap_min"],
        service_index=service_index,
    )

    # Derived helpers: team mean efficiency and eligible employee list per service.
    for s in sc.services:
        effs = [emp.profile[s.idx][0] for emp in sc.employees if s.idx in emp.profile]
        if not effs:
            raise ValueError(f"service {s.id} has no qualified employee")
        sc.mean_eff.append(sum(effs) / len(effs))
        sc.eligible_emps.append([emp.idx for emp in sc.employees if s.idx in emp.profile])

    lang_total = sum(share for _, share in sc.lang_prefs)
    if lang_total > 1.0 + 1e-9:
        raise ValueError("language preference shares exceed 1.0")

    return sc
