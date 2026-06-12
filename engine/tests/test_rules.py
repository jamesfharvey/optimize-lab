"""Unit tests for every normative simulation rule."""
import math

import numpy as np
import pytest

from conftest import PRESETS, hand_draws, load_mini, mini_config

from optimize_lab import abandonment, policies
from optimize_lab.config import load_scenario
from optimize_lab.csat import (appointment_csat, predicted_csat,
                               punctuality_factor, to_five_point, visit_csat)
from optimize_lab.levers import make_params, optimize_breaks
from optimize_lab.montecarlo import run_scenario_mc
from optimize_lab.simulate import VariantParams, build_appointment_schedule, run_day
from optimize_lab.world import ArrivalDist, gen_day_draws

BASE = VariantParams(appt_share=0.0, distribution="even", matching=False)

# row indices in the collect=True output (v1.4: promise is a range)
(R_TYPE, R_SVC, R_CHECKIN, R_PROM_LOW, R_PROMISED, R_PROM_HIGH, R_START,
 R_END, R_EMP, R_STATUS, R_WAIT, R_CSAT) = 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13


def run(sc, draws, params=BASE):
    return run_day(sc, ArrivalDist(sc), draws, params, collect=True)


# ---------------------------------------------------------------- durations

def test_effective_duration_formula():
    # eff 2.0 and form 0.8: dur = 30 / (2.0 * 0.8) = 18.75; prep multiplies.
    sc = load_mini(employees=[{"id": "E1", "name": "One", "profile": [
        {"service_id": "A", "efficiency": 2.0, "csat": 80},
        {"service_id": "B", "efficiency": 1.0, "csat": 80}]}])
    form = np.ones((1, 2))
    form[0][0] = 0.8
    d = hand_draws(sc, arrivals=[540.0], services=["A"], form=form)
    _, rows = run(sc, d)
    assert rows[0][R_END] - rows[0][R_START] == pytest.approx(30 / (2.0 * 0.8),
                                                              abs=0.011)
    _, rows = run(sc, d, VariantParams(appt_share=0.0, distribution="even",
                                       matching=False, prep_factor=0.85))
    # audit rows round times to 2 decimals
    assert rows[0][R_END] - rows[0][R_START] == pytest.approx(18.75 * 0.85,
                                                              abs=0.011)


def test_daily_form_clipped():
    sc = load_mini(simulation={"daily_form_jitter": 0.4, "monte_carlo_days": 1,
                               "random_seed": 1})
    dist = ArrivalDist(sc)
    for day in range(50):
        f = gen_day_draws(sc, day, dist).form
        assert f.min() >= 0.6 - 1e-12 and f.max() <= 1.4 + 1e-12


# ---------------------------------------------------------------- collision

def collision_scenario(target_a):
    cfg = mini_config()
    cfg["services"][0]["target_duration_min"] = target_a
    return load_scenario(cfg)


def collision_draws(sc):
    # visitor 0: walk-in for A at 09:50; visitor 1: appointment for B.
    # With share .5 only v1 books; single 'even' slot lands at 10:15 (615).
    return hand_draws(sc, arrivals=[590.0, 0.0], services=["A", "B"],
                      u_appt=np.array([0.99, 0.1]))


def test_collision_rule_blocks_dropin():
    # A takes 40 min: 590+40=630 > appt 615, and 630 > 615+late_ok(5) so even
    # the all-blocked exception refuses; employee idles until the appointment.
    sc = collision_scenario(40)
    params = VariantParams(appt_share=0.5, distribution="even", matching=False)
    m, rows = run(sc, collision_draws(sc), params)
    appt, walk = rows[1], rows[0]
    assert appt[R_CHECKIN] == 615
    assert appt[R_START] == 605            # summoned at window open (s - grace)
    assert walk[R_START] == 615            # only after the appointment cleared
    assert m.pct_on_time == 1.0
    assert m.served == 2


def test_collision_exception_within_late_ok():
    # A takes 30 min: every eligible employee is blocked (there is only one),
    # shortest-overrun employee may take it since 590+30=620 <= 615+late_ok(5).
    sc = collision_scenario(30)
    params = VariantParams(appt_share=0.5, distribution="even", matching=False)
    m, rows = run(sc, collision_draws(sc), params)
    assert rows[0][R_START] == 590         # walk-in taken immediately
    assert rows[1][R_START] == 620         # appointment right after, within late_ok
    assert m.pct_on_time == 1.0            # lateness 5 <= late_ok 5
    assert m.max_late == pytest.approx(5)


def test_collision_excluded_when_another_employee_clean():
    # E1 is blocked by its reservation but E2 could serve v cleanly later, so
    # the exception must NOT fire for E1.
    sc = load_mini(employees=[
        {"id": "E1", "name": "One", "profile": [
            {"service_id": "A", "efficiency": 2.0, "csat": 80},
            {"service_id": "B", "efficiency": 1.0, "csat": 80}]},
        {"id": "E2", "name": "Two", "profile": [
            {"service_id": "A", "efficiency": 1.0, "csat": 80},
            {"service_id": "B", "efficiency": 1.0, "csat": 80}]},
    ])

    class FakeDay:
        sc = None
        def lang_ok(self, e, v): return True
        def is_free(self, e, t): return True

    day = FakeDay()
    day.sc = sc
    day.dur = [[15.0, math.inf], [30.0, math.inf]]
    day.next_appt = [610.0, math.inf]
    day.late_ok = 5.0
    assert policies._collision_allowed(day, 0, 0, 0, 600.0) is False
    assert policies._collision_allowed(day, 1, 0, 0, 600.0) is True


# ---------------------------------------------------------------- breaks

def test_break_integrity():
    sc = load_mini(employees=[{
        "id": "E1", "name": "One",
        "break_window": {"start": "10:00", "end": "10:30", "variability_min": 0},
        "profile": [{"service_id": "A", "efficiency": 1.0, "csat": 80},
                    {"service_id": "B", "efficiency": 1.0, "csat": 80}]}])
    # B service started 09:55 runs into the break; break then shortened;
    # next visitor must not start inside the break window.
    d = hand_draws(sc, arrivals=[595.0, 606.0], services=["B", "B"])
    m, rows = run(sc, d)
    assert rows[0][R_START] == 595 and rows[0][R_END] == 605
    assert rows[1][R_START] == 630          # not 605: break (shortened) honored
    assert m.served == 2


def test_break_blocks_start_at_window_open():
    sc = load_mini(employees=[{
        "id": "E1", "name": "One",
        "break_window": {"start": "10:00", "end": "10:30", "variability_min": 0},
        "profile": [{"service_id": "B", "efficiency": 1.0, "csat": 80},
                    {"service_id": "A", "efficiency": 1.0, "csat": 80}]}])
    d = hand_draws(sc, arrivals=[600.0], services=["B"])
    _, rows = run(sc, d)
    assert rows[0][R_START] == 630          # arrival exactly at break start waits


def test_no_invented_breaks():
    sc = load_mini()   # no break_window declared
    assert optimize_breaks(sc, ArrivalDist(sc)) == {}
    d = hand_draws(sc, arrivals=[600.0, 601.0], services=["B", "B"])
    _, rows = run(sc, d, VariantParams(appt_share=0.0, distribution="even",
                                       matching=False, breaks={}))
    assert rows[0][R_START] == 600 and rows[1][R_START] == 610


def test_break_shift_rounded_to_5():
    sc = load_mini(employees=[{
        "id": "E1", "name": "One",
        "break_window": {"start": "10:00", "end": "10:30", "variability_min": 15},
        "profile": [{"service_id": "A", "efficiency": 1.0, "csat": 80},
                    {"service_id": "B", "efficiency": 1.0, "csat": 80}]}])
    dist = ArrivalDist(sc)
    seen = set()
    for day in range(40):
        shift = gen_day_draws(sc, day, dist).brk_shift[0]
        assert shift % 5 == 0 and abs(shift) <= 15
        seen.add(shift)
    assert len(seen) > 1                    # variability actually varies


# ---------------------------------------------------------------- pickers

class FakeDay:
    """Duck-typed _Day for surgical picker tests."""

    def __init__(self, sc, dur, q, arr_t, focus=None, next_appt=None,
                 weights=(0.6, 0.4, 0.0), aging_cap=45, late_ok=5,
                 lang_block=()):
        self.sc = sc
        self.dur = dur
        self.q = [list(x) for x in q]
        self.qpos = [0] * len(q)
        self.waiting = np.ones(16, dtype=bool)
        self.arr_t = np.array(arr_t + [0.0] * (16 - len(arr_t)))
        self.focus = focus
        self.next_appt = next_appt or [math.inf] * sc.n_employees
        self.weights = weights
        self.aging_cap = aging_cap
        self.late_ok = late_ok
        self._lang_block = set(lang_block)

    def lang_ok(self, e, v):
        return (e, v) not in self._lang_block

    def is_free(self, e, t):
        return True

    def pred_csat(self, e, s):
        return self.sc.employees[e].profile[s][1]


def two_emp_scenario():
    return load_mini(employees=[
        {"id": "E1", "name": "One", "profile": [
            {"service_id": "A", "efficiency": 2.0, "csat": 80},
            {"service_id": "B", "efficiency": 0.5, "csat": 70}]},
        {"id": "E2", "name": "Two", "profile": [
            {"service_id": "A", "efficiency": 1.0, "csat": 90},
            {"service_id": "B", "efficiency": 1.5, "csat": 85}]},
    ])


def test_focus_sets_and_aging_cap():
    sc = two_emp_scenario()
    focus = policies.compute_focus(sc)         # means: A 1.5, B 1.0
    assert focus[0] == {0} and focus[1] == {1}
    dur = [[15.0, 20.0], [30.0, 20.0 / 3]]
    # aging cap is scoped to the ROUTED pool: with a focused candidate
    # waiting, E1 stays specialized even though the out-of-focus B visitor
    # has waited past the cap (B's own focused employee rescues them)
    day = FakeDay(sc, dur, q=[[0], [1]], arr_t=[595.0, 550.0], focus=focus)
    assert policies.pick_optimized(day, 0, 600.0)[0] == 0
    # within the focused pool, an aged candidate overrides the blended score
    day = FakeDay(sc, dur, q=[[0, 2], [1]], arr_t=[550.0, 550.0, 595.0],
                  focus=focus)
    assert policies.pick_optimized(day, 0, 600.0)[0] == 0   # aged focused head
    # below the cap, the focused service wins despite the longer B wait
    day = FakeDay(sc, dur, q=[[0], [1]], arr_t=[595.0, 580.0], focus=focus)
    assert policies.pick_optimized(day, 0, 600.0)[0] == 0
    # no focused candidate waiting -> work-conserving fallback, and the cap
    # applies within the fallback pool (aged B is rescued here)
    day = FakeDay(sc, dur, q=[[], [1]], arr_t=[0.0, 550.0], focus=focus)
    assert policies.pick_optimized(day, 0, 600.0)[0] == 1


def test_focus_never_empty_and_full_coverage():
    # an employee below team mean everywhere keeps their best service
    sc = load_mini(employees=[
        {"id": "W", "name": "Weak", "profile": [
            {"service_id": "A", "efficiency": 0.5, "csat": 70},
            {"service_id": "B", "efficiency": 0.4, "csat": 70}]},
        {"id": "S", "name": "Strong", "profile": [
            {"service_id": "A", "efficiency": 2.0, "csat": 90},
            {"service_id": "B", "efficiency": 2.0, "csat": 90}]},
    ])
    focus = policies.compute_focus(sc)
    assert focus[0] == {0}                       # best-of-their-services kept
    # invariants under daily form, on a real preset
    uni = load_scenario(PRESETS[-1])
    dist = ArrivalDist(uni)
    for day in range(20):
        form = gen_day_draws(uni, day, dist).form
        f = policies.compute_focus(uni, form)
        assert all(f[e.idx] for e in uni.employees)
        for s in uni.services:
            assert any(s.idx in f[e] for e in uni.eligible_emps[s.idx])


def test_blended_score_weights():
    sc = two_emp_scenario()
    day = FakeDay(sc, dur=None, q=[[], []], arr_t=[0.0, 0.0])
    cands = [(0, 0, 5.0, 10.0), (1, 1, 10.0, 20.0)]   # (v, s, wait, dur)
    day.weights = (0.6, 0.4, 0.0)
    assert policies._blended_pick(day, 0, cands)[0] == 0   # throughput-leaning
    day.weights = (0.0, 1.0, 0.0)
    assert policies._blended_pick(day, 0, cands)[0] == 1   # fairness-only


def test_language_hard_filter_in_picker():
    sc = two_emp_scenario()
    dur = [[15.0, 20.0], [30.0, 20.0 / 3]]
    day = FakeDay(sc, dur, q=[[0], []], arr_t=[590.0], lang_block={(0, 0)})
    assert policies.pick_baseline(day, 0, 600.0) is None
    assert policies.pick_baseline(day, 1, 600.0)[0] == 0


def test_language_hard_filter_end_to_end():
    sc = load_mini(demand={"visitors_per_day": 1,
                           "arrival_pattern": {"shape": "uniform"},
                           "language_preferences": [
                               {"language": "es", "share": 1.0}]})
    d = hand_draws(sc, arrivals=[560.0], services=["A"],
                   lang=np.array([0], dtype=np.int64))
    m, rows = run(sc, d)
    assert m.served == 0
    assert m.turned_away == 1
    assert rows[0][R_STATUS] == "turned_away"   # idle employee, but no es


# ---------------------------------------------------------------- incompletes

def test_incomplete_accounting_and_prep():
    cfg = mini_config()
    cfg["services"][0]["incomplete_rate"] = 0.4
    sc = load_scenario(cfg)
    d = hand_draws(sc, arrivals=[540.0, 541.0], services=["A", "B"],
                   u_inc=np.array([0.3, 0.99]), u_inc_cost=np.array([0.5, 0.5]))
    m, rows = run(sc, d)
    consumed = 30 * (0.25 + 0.35 * 0.5)          # U(0.25, 0.6) at u=0.5
    assert m.incompletes == 1 and m.served == 1
    assert m.incomplete_min == pytest.approx(consumed)
    assert rows[0][R_STATUS] == "incomplete"
    assert rows[0][R_END] - rows[0][R_START] == pytest.approx(consumed)
    assert rows[1][R_START] == pytest.approx(540 + consumed)   # no re-queue ahead
    assert m.mean_wait == pytest.approx(rows[1][R_START] - 541)  # excl. incomplete
    # prep cuts the rate: 0.4 * (1-0.5) = 0.2 <= u_inc 0.3 -> completes now
    prep = VariantParams(appt_share=0.0, distribution="even", matching=False,
                         prep_factor=0.85, inc_factor=0.5)
    m2, rows2 = run(sc, d, prep)
    assert m2.incompletes == 0 and m2.served == 2
    assert rows2[0][R_END] - rows2[0][R_START] == pytest.approx(30 * 0.85)


# ---------------------------------------------------------------- abandonment

def test_abandonment_curve_shape():
    assert abandonment.prob_abandon_by(10) == 0.0
    assert abandonment.prob_abandon_by(15) == 0.0
    assert abandonment.prob_abandon_by(60) == pytest.approx(0.12)
    assert abandonment.prob_abandon_by(90) == pytest.approx(0.15)
    assert abandonment.prob_abandon_by(240) == pytest.approx(0.15)
    waits = np.linspace(0, 120, 200)
    probs = [abandonment.prob_abandon_by(w) for w in waits]
    assert all(b >= a for a, b in zip(probs, probs[1:]))     # monotone
    assert abandonment.threshold_from_u(0.0) == pytest.approx(15.0)
    assert abandonment.threshold_from_u(0.06) == pytest.approx(37.5)
    assert abandonment.threshold_from_u(0.12) == pytest.approx(60.0)
    assert abandonment.threshold_from_u(0.135) == pytest.approx(75.0)
    assert abandonment.threshold_from_u(0.151) == math.inf
    assert abandonment.threshold_from_u(0.9) == math.inf


def test_abandonment_in_simulation():
    cfg = mini_config(simulation={"abandonment_model": {"enabled": True},
                                  "daily_form_jitter": 0.0,
                                  "monte_carlo_days": 1, "random_seed": 7})
    sc = load_scenario(cfg)
    d = hand_draws(sc, arrivals=[540.0, 545.0], services=["A", "B"],
                   patience=np.array([math.inf, 5.0]))
    m, rows = run(sc, d)        # E1 busy with A until 570; v1 leaves at 550
    assert m.abandoned == 1
    assert rows[1][R_STATUS] == "abandoned"
    assert rows[1][R_WAIT] == pytest.approx(5.0)
    # patient enough -> served instead
    d2 = hand_draws(sc, arrivals=[540.0, 545.0], services=["A", "B"],
                    patience=np.array([math.inf, 40.0]))
    m2, _ = run(sc, d2)
    assert m2.abandoned == 0 and m2.served == 2
    # disabled model ignores patience entirely
    sc_off = load_mini()
    m3, _ = run(sc_off, d)
    assert m3.abandoned == 0


# ---------------------------------------------------------------- demand levers

def test_smoothing_even_slots():
    uni = load_scenario(PRESETS[-1])  # university
    dist = ArrivalDist(uni)
    draws = gen_day_draws(uni, 0, dist)
    params = VariantParams(appt_share=1.0, distribution="even", matching=False)
    is_appt, sched = build_appointment_schedule(uni, dist, draws, params)
    times = sorted(sched[is_appt])
    assert len(times) == draws.n                  # all services appt-eligible
    assert all(t % 5 == 0 for t in times)
    assert times[0] >= uni.open_min and times[-1] <= uni.cutoff
    gaps = np.diff(times)
    assert gaps.max() <= 10                       # ~ (510/220) rounded to 5-grid


def test_appointment_subset_pairing():
    # same draws, higher share: baseline appointment set is a subset
    uni = load_scenario(PRESETS[-1])
    dist = ArrivalDist(uni)
    draws = gen_day_draws(uni, 3, dist)
    lo, _ = build_appointment_schedule(
        uni, dist, draws, VariantParams(0.2, "even", False))
    hi, _ = build_appointment_schedule(
        uni, dist, draws, VariantParams(0.75, "even", False))
    assert (lo & ~hi).sum() == 0


def test_match_arrival_pattern_follows_surge():
    uni = load_scenario(PRESETS[-1])
    dist = ArrivalDist(uni)
    draws = gen_day_draws(uni, 0, dist)
    params = VariantParams(1.0, "match_arrival_pattern", False)
    is_appt, sched = build_appointment_schedule(uni, dist, draws, params)
    t = sched[is_appt]
    in_surge = ((t >= 660) & (t < 780)).mean()    # 11:00-13:00
    assert in_surge > 0.30                        # > the 23.5% uniform share


def test_deflection_reported_separately():
    sc = load_mini()
    d = hand_draws(sc, arrivals=[560.0, 565.0], services=["B", "B"],
                   u_deflect=np.array([0.1, 0.9]))
    m, rows = run(sc, d, VariantParams(0.0, "even", False, deflect_rate=0.5))
    assert m.resolved == 1 and m.served == 1
    assert rows[0][R_STATUS] == "deflected" and rows[0][R_START] == ""
    m2, _ = run(sc, d)                            # lever off: both served
    assert m2.resolved == 0 and m2.served == 2


def test_running_late_converts_no_shows():
    cfg = mini_config(policy={
        "baseline": {"appointment_share": 1.0},
        "optimized": {},
        "no_show_rate": 0.5,
    })
    sc = load_scenario(cfg)
    d = hand_draws(sc, arrivals=[0.0], services=["B"],
                   u_appt=np.array([0.1]), u_noshow=np.array([0.3]))
    m, rows = run(sc, d, VariantParams(1.0, "even", False))
    assert m.served == 0 and rows[0][R_STATUS] == "no_show"
    assert m.appts_shown == 0
    m2, rows2 = run(sc, d, VariantParams(1.0, "even", False,
                                         noshow_reduction=0.5))
    assert m2.served == 1 and rows2[0][R_STATUS] == "served"


# ---------------------------------------------------------------- appointments

def test_pins_round_robin_and_employee_specific_waits():
    cfg = mini_config(
        services=[{"id": "B", "name": "Beta", "target_duration_min": 10,
                   "demand_share": 1.0}],
        employees=[
            {"id": "E1", "name": "Slow", "profile": [
                {"service_id": "B", "efficiency": 0.08, "csat": 80}]},
            {"id": "E2", "name": "Fast", "profile": [
                {"service_id": "B", "efficiency": 1.0, "csat": 80}]},
        ])
    d_kw = dict(arrivals=[0.0, 0.0, 0.0], services=["B", "B", "B"],
                u_appt=np.array([0.1, 0.1, 0.1]),
                u_slot=np.array([0.1, 0.5, 0.9]))
    # even slots land at 565 / 615 / 665; pins alternate E1, E2, E1
    sc_es = load_scenario(mini_config(policy={
        "baseline": {"appointment_share": 1.0,
                     "scheduling_method": "employee_specific",
                     "appointment_grace_min": 10},
        "optimized": {}, "no_show_rate": 0.0,
    }, **{k: v for k, v in cfg.items() if k in ("services", "employees")}))
    pins = policies.assign_pins(
        sc_es, [(565.0, 0, 0, [True, True]), (615.0, 1, 0, [True, True]),
                (665.0, 2, 0, [True, True])])
    assert pins == {0: 0, 1: 1, 2: 0}
    m_es, rows_es = run(sc_es, hand_draws(sc_es, **d_kw),
                        VariantParams(1.0, "even", False))
    # E1 (dur 125) ties up appt 0 until 680; appt 2 is pinned and goes late
    assert rows_es[2][R_START] == pytest.approx(680)
    assert m_es.pct_on_time == pytest.approx(2 / 3)     # lateness 15 > late_ok 5
    assert m_es.pct_acceptable == pytest.approx(1.0)    # but within 15-min limit
    assert m_es.max_late == pytest.approx(15)
    sc_rr = load_scenario(mini_config(policy={
        "baseline": {"appointment_share": 1.0,
                     "scheduling_method": "round_robin",
                     "appointment_grace_min": 10},
        "optimized": {}, "no_show_rate": 0.0,
    }, **{k: v for k, v in cfg.items() if k in ("services", "employees")}))
    m_rr, rows_rr = run(sc_rr, hand_draws(sc_rr, **d_kw),
                        VariantParams(1.0, "even", False))
    # first eligible FREE employee takes it at summon time instead
    assert rows_rr[2][R_START] == pytest.approx(655)
    assert m_rr.pct_on_time == 1.0


def test_punctuality_stats_capture_lateness():
    cfg = mini_config(
        services=[{"id": "A", "name": "Long", "target_duration_min": 100,
                   "demand_share": 0.5},
                  {"id": "B", "name": "Beta", "target_duration_min": 10,
                   "demand_share": 0.5}],
        policy={"baseline": {"appointment_share": 1.0},
                "optimized": {}, "no_show_rate": 0.0})
    sc = load_scenario(cfg)
    # appt slots: 580 (A, runs 570-670) and 650 (B, summoned 670 > 660 = late)
    d = hand_draws(sc, arrivals=[0.0, 0.0], services=["A", "B"],
                   u_appt=np.array([0.1, 0.1]), u_slot=np.array([0.1, 0.9]))
    m, rows = run(sc, d, VariantParams(1.0, "even", False))
    assert rows[1][R_START] == pytest.approx(670)
    assert m.served == 2
    assert m.pct_on_time == pytest.approx(0.5)      # lateness 20 > late_ok 5
    assert m.pct_acceptable == pytest.approx(0.5)   # and > late_acceptable 15
    assert m.p90_late == pytest.approx(np.percentile([0.0, 20.0], 90))
    assert m.max_late == pytest.approx(20)


# ---------------------------------------------------------------- day boundary

def test_last_join_cutoff_on_generated_arrivals():
    uni = load_scenario(PRESETS[-1])
    dist = ArrivalDist(uni)
    for day in range(20):
        arr = gen_day_draws(uni, day, dist).arrival
        assert arr.min() >= uni.open_min
        assert arr.max() <= uni.cutoff


def test_turned_away_and_makespan():
    sc = load_mini()
    d = hand_draws(sc, arrivals=[689.0, 689.0, 689.0], services=["A", "A", "A"])
    m, rows = run(sc, d)
    assert m.served == 2                       # 689-719, 719-749
    assert m.turned_away == 1                  # third still queued at close
    assert rows[1][R_END] == pytest.approx(749)
    assert m.makespan == pytest.approx(29)     # 749 - 720, runs to completion


# ---------------------------------------------------------------- forecasts

def test_promised_wait_dispatch_forward():
    sc = load_mini()
    d = hand_draws(sc, arrivals=[540.0, 550.0, 551.0],
                   services=["A", "B", "B"])
    _, rows = run(sc, d)
    # dispatch replay: v0 starts at once; v1 waits for the A completion at
    # 570; v2 waits behind v1 (570 + 10 - 551)
    assert rows[0][R_PROMISED] == pytest.approx(0.0)
    assert rows[1][R_PROMISED] == pytest.approx(20.0)
    assert rows[2][R_PROMISED] == pytest.approx(29.0)
    # range columns: band = max(2, 0.15 * center)
    assert rows[2][R_PROM_LOW] == pytest.approx(29.0 - 4.35)
    assert rows[2][R_PROM_HIGH] == pytest.approx(29.0 + 4.35)
    assert rows[0][R_PROM_LOW] == 0.0 and rows[0][R_PROM_HIGH] == 2.0


def test_quote_models_parallel_dispatch():
    # Two employees busy until 555/570; one A visitor queued ahead. The old
    # work/capacity quote said (13+28+20)/2 = 30.5; the dispatch-forward
    # quote follows the actual assignment order: E1 frees at 555, takes the
    # head; both free at 570 and v starts there -> 28.
    sc = load_mini(employees=[
        {"id": "E1", "name": "One", "profile": [
            {"service_id": "A", "efficiency": 2.0, "csat": 80},
            {"service_id": "B", "efficiency": 1.0, "csat": 80}]},
        {"id": "E2", "name": "Two", "profile": [
            {"service_id": "A", "efficiency": 1.0, "csat": 80},
            {"service_id": "B", "efficiency": 1.0, "csat": 80}]},
    ])
    d = hand_draws(sc, arrivals=[540.0, 540.0, 541.0, 542.0],
                   services=["A", "A", "A", "A"])
    _, rows = run(sc, d)
    assert rows[0][R_START] == 540 and rows[0][R_END] == 555    # E1, dur 15
    assert rows[1][R_START] == 540 and rows[1][R_END] == 570    # E2, dur 30
    assert rows[3][R_PROMISED] == pytest.approx(28.0)
    assert rows[3][R_START] == pytest.approx(570)               # promise == actual


def test_quote_abandonment_survival_discount():
    # Queue-ahead work is discounted by conditional survival: w (B, queued
    # since 541) is forecast to be served at 600 with survival
    # S(59)/S(49) = 0.8827/0.9093, shortening its expected 30-min service.
    cfg = mini_config(
        services=[{"id": "A", "name": "Long", "target_duration_min": 60,
                   "demand_share": 0.5},
                  {"id": "B", "name": "Beta", "target_duration_min": 30,
                   "demand_share": 0.5}],
        simulation={"abandonment_model": {"enabled": True},
                    "daily_form_jitter": 0.0, "monte_carlo_days": 1,
                    "random_seed": 7})
    sc = load_scenario(cfg)
    d = hand_draws(sc, arrivals=[540.0, 541.0, 590.0],
                   services=["A", "B", "B"])
    _, rows = run(sc, d)
    s0 = 1 - abandonment.prob_abandon_by(590 - 541)
    s1 = 1 - abandonment.prob_abandon_by(600 - 541)
    expected = (600 - 590) + 30.0 * s1 / s0
    assert rows[2][R_PROMISED] == pytest.approx(expected, abs=0.011)
    # with abandonment disabled the same quote is undiscounted
    sc_off = load_scenario(mini_config(
        services=cfg["services"],
        simulation={"abandonment_model": {"enabled": False},
                    "daily_form_jitter": 0.0, "monte_carlo_days": 1,
                    "random_seed": 7}))
    _, rows_off = run(sc_off, d)
    assert rows_off[2][R_PROMISED] == pytest.approx(40.0)


def test_quote_expects_future_arrivals_under_matching():
    # Arrival-aware quote (v1.4): under matching, the throughput-leaning
    # score lets expected future short jobs (B, 10 min, one every ~10 min)
    # jump a queued long-service visitor (A, 30 min) until the aging cap
    # rescues them — so the honest quote for v is the cap itself, not the
    # naive "next completion" 5 minutes. Baseline FIFO is arrival-immune:
    # younger expected arrivals can never out-wait v.
    cfg = mini_config(demand={"visitors_per_day": 30,
                              "arrival_pattern": {"shape": "uniform"}})
    sc = load_scenario(cfg)
    d = hand_draws(sc, arrivals=[575.0, 600.0], services=["A", "A"])
    opt = VariantParams(appt_share=0.0, distribution="even", matching=True)
    _, rows = run(sc, d, opt)
    assert rows[1][R_PROMISED] == pytest.approx(45.0)   # = aging_cap_min
    _, rows_fifo = run(sc, d)
    assert rows_fifo[1][R_PROMISED] == pytest.approx(5.0)  # FIFO: next free


def test_expected_arrival_schedule_deterministic():
    from optimize_lab.simulate import expected_arrival_schedule
    sc = load_scenario(mini_config(demand={
        "visitors_per_day": 30, "arrival_pattern": {"shape": "uniform"}}))
    dist = ArrivalDist(sc)
    a = expected_arrival_schedule(sc, dist, BASE)
    assert a == expected_arrival_schedule(sc, dist, BASE)   # no randomness
    assert len(a) == 30                                     # 15 per service
    assert all(sc.open_min <= x[0] <= sc.cutoff for x in a)
    # demand-side levers thin the stream
    thin = expected_arrival_schedule(
        sc, dist, VariantParams(appt_share=0.5, distribution="even",
                                matching=False, deflect_rate=0.2))
    assert len(thin) == 2 * round(15 * 0.8 * 0.5)


def test_quote_is_purely_informational():
    """AC5 invariance: the quote feeds only the walk-in promise terms (CSAT)
    and the audit trail — never queueing, routing, or abandonment. Replacing
    it with a constant must leave every operational metric identical."""
    import dataclasses
    import optimize_lab.simulate as sim
    uni = load_scenario(PRESETS[-1])
    dist = ArrivalDist(uni)
    draws = gen_day_draws(uni, 0, dist)
    for fs in (frozenset(), frozenset(["matching", "appointment_smoothing",
                                       "prep_in_queue", "deflection",
                                       "running_late"])):
        params = make_params(uni, fs)
        real, _ = run_day(uni, dist, draws, params)
        orig = sim.dispatch_forward_quote
        sim.dispatch_forward_quote = lambda day, fc, v, t: 7.0
        try:
            patched, _ = run_day(uni, dist, draws, params)
        finally:
            sim.dispatch_forward_quote = orig
        a, b = dataclasses.asdict(real), dataclasses.asdict(patched)
        for field in a:
            if field == "mean_csat":
                assert a[field] != b[field] or a[field] == 0.0
            else:
                assert a[field] == b[field], f"operational drift in {field}"


# ---------------------------------------------------------------- CSAT

def test_csat_formula_asymmetric_ranges():
    kw = dict(alpha=0.6, beta_early=0.1, beta_late=0.4, gamma=0.3,
              range_k=0.15)
    # center 30 -> band max(2, 4.5) = 4.5 -> promised range [25.5, 34.5]
    assert visit_csat(80, 30, 30, 20, 30, **kw) == pytest.approx(80.0)
    assert visit_csat(80, 34, 30, 20, 30, **kw) == pytest.approx(80.0)
    assert visit_csat(80, 26, 30, 20, 30, **kw) == pytest.approx(80.0)
    # late by 10 past high: W_acc = 1 - 0.4*(10/30); W_wait = 1 - 0.6*(10/60)
    assert visit_csat(80, 44.5, 30, 20, 30, **kw) == pytest.approx(
        80 * (1 - 0.6 * 10 / 60) * (1 - 0.4 * 10 / 30))
    # early by 5.5 below low: mild beta_early, no W_wait penalty
    assert visit_csat(80, 20, 30, 20, 30, **kw) == pytest.approx(
        80 * (1 - 0.1 * 5.5 / 30))
    # over-duration penalty unchanged
    assert visit_csat(80, 30, 30, 45, 30, **kw) == pytest.approx(
        80 * (1 - 0.3 * 0.5))
    # tiny center: band floor 2 min applies, late ratio caps at 1
    assert visit_csat(80, 10, 0.0, 20, 30, **kw) == pytest.approx(
        80 * (1 - 0.6 * 8 / 60) * (1 - 0.4))
    assert predicted_csat(90, 45, 30, 0.3) == pytest.approx(90 * 0.85)


def test_promise_range_band():
    from optimize_lab.csat import promise_range
    assert promise_range(40.0, 0.15) == (34.0, 46.0)
    assert promise_range(5.0, 0.15) == (3.0, 7.0)      # 2-min floor
    assert promise_range(0.0, 0.15) == (0.0, 2.0)
    # early side never penalized harder than late side at defaults
    kw = dict(alpha=0.6, beta_early=0.1, beta_late=0.4, gamma=0.3,
              range_k=0.15)
    early = visit_csat(80, 20, 40, 20, 30, **kw)
    late = visit_csat(80, 60, 40, 20, 30, **kw)
    assert early > late


def test_punctuality_curve_kinked_convex():
    k1, k2 = 5.0, 15.0
    # promise kept: no penalty (early summons clamp to lateness 0 upstream)
    assert punctuality_factor(0, k1, k2) == 1.0
    assert punctuality_factor(5, k1, k2) == 1.0
    # mild linear ramp between the kinks
    assert punctuality_factor(10, k1, k2) == pytest.approx(0.95)
    assert punctuality_factor(15, k1, k2) == pytest.approx(0.90)
    # convex (quadratic) growth past late_acceptable, floor-capped at 0
    assert punctuality_factor(45, k1, k2) == pytest.approx(
        0.90 - 0.90 * (30 / 60) ** 2)
    assert punctuality_factor(500, k1, k2) == 0.0
    # convexity: equal lateness increments cost increasingly more
    drops = [punctuality_factor(k2 + d, k1, k2)
             - punctuality_factor(k2 + d + 10, k1, k2) for d in (0, 10, 20)]
    assert drops[0] < drops[1] < drops[2]
    # monotone non-increasing overall
    vals = [punctuality_factor(x, k1, k2) for x in np.linspace(0, 120, 240)]
    assert all(b <= a + 1e-12 for a, b in zip(vals, vals[1:]))


def test_high_base_csat_partially_recovers_late_start():
    # Same lateness (20 min, beyond late_acceptable 15): a high-CSAT,
    # faster-than-target employee retains a higher predicted score than a
    # low-CSAT, slower-than-target one. The multiplicative recovery is
    # intentional (see csat.py docstring).
    k1, k2, gamma = 5.0, 15.0, 0.3
    high = appointment_csat(92, 20.0, k1, k2, actual_duration=24,
                            target_duration=30, gamma=gamma)
    low = appointment_csat(75, 20.0, k1, k2, actual_duration=45,
                           target_duration=30, gamma=gamma)
    assert high > low
    assert high == pytest.approx(92 * punctuality_factor(20, k1, k2))


def test_unsummoned_shown_appointment_counts_against_pct():
    # Employee's shift ends before the slot: the appointment checks in, is
    # never summoned, and counts against both punctuality percentages while
    # the lateness percentiles stay over summoned appointments only.
    sc = load_mini(employees=[{
        "id": "E1", "name": "One", "work_end": "10:00",
        "profile": [{"service_id": "A", "efficiency": 1.0, "csat": 80},
                    {"service_id": "B", "efficiency": 1.0, "csat": 80}]}])
    d = hand_draws(sc, arrivals=[0.0], services=["B"],
                   u_appt=np.array([0.1]))     # single even slot at 615
    m, rows = run(sc, d, VariantParams(0.5, "even", False))
    assert m.appts_shown == 1
    assert m.turned_away == 1
    assert m.pct_on_time == 0.0
    assert m.pct_acceptable == 0.0
    assert m.max_late == 0.0                   # no summoned lateness samples
    assert rows[0][R_STATUS] == "turned_away"


def test_five_point_translation():
    assert to_five_point(70, 70) == pytest.approx(4.0)    # baseline anchors 4.0
    assert to_five_point(100, 70) == pytest.approx(5.0)
    vals = [to_five_point(c, 70) for c in (70, 80, 90, 95)]
    assert all(b > a for a, b in zip(vals, vals[1:]))
    gains = np.diff(vals)
    assert gains[-1] < gains[0]                            # diminishing returns


# ---------------------------------------------------------------- pairing / MC

def test_day_draws_deterministic():
    uni = load_scenario(PRESETS[-1])
    dist = ArrivalDist(uni)
    a, b = gen_day_draws(uni, 11, dist), gen_day_draws(uni, 11, dist)
    for field in ("u_appt", "arrival", "service", "lang", "patience",
                  "u_noshow", "form", "brk_shift"):
        np.testing.assert_array_equal(getattr(a, field), getattr(b, field))
    c = gen_day_draws(uni, 12, dist)
    assert not np.array_equal(a.arrival, c.arrival)


def test_paired_monte_carlo_and_repeatability():
    cfg = mini_config()
    cfg["demand"]["visitors_per_day"] = 15
    cfg["simulation"]["monte_carlo_days"] = 4
    sc = load_scenario(cfg)
    r1 = run_scenario_mc(sc)
    r2 = run_scenario_mc(sc)
    for fs in r1.arrays:
        for f, arr in r1.arrays[fs].items():
            np.testing.assert_array_equal(arr, r2.arrays[fs][f])
    # paired CI: identical variant sets -> per-day differences exactly zero
    base = r1.of("baseline")
    solo = r1.of("solo:matching")
    assert len(base["served"]) == 4
    assert not np.array_equal(base["mean_wait"], np.zeros(4))
    assert r1.plan["combined"] == r1.plan["solo:matching"]   # dedup, same set
    np.testing.assert_array_equal(solo["served"], r1.of("combined")["served"])


# ---------------------------------------------------------------- break search

def test_break_search_grid_and_improvement():
    cfg = mini_config(
        demand={"visitors_per_day": 8,
                "arrival_pattern": {"shape": "single_surge",
                                    "surges": [{"start": "10:00", "end": "10:30",
                                                "multiplier": 8.0}]}},
        employees=[{
            "id": "E1", "name": "One",
            "break_window": {"start": "10:00", "end": "10:30",
                             "variability_min": 0},
            "profile": [{"service_id": "A", "efficiency": 1.0, "csat": 80},
                        {"service_id": "B", "efficiency": 1.0, "csat": 80}]}],
        simulation={"monte_carlo_days": 6, "daily_form_jitter": 0.0,
                    "random_seed": 3,
                    "abandonment_model": {"enabled": False}})
    sc = load_scenario(cfg)
    dist = ArrivalDist(sc)
    rec = optimize_breaks(sc, dist, eval_days=6)
    (b0, b1) = rec[0]
    assert (b0 - sc.employees[0].work_start) % 15 == 0     # 15-min grid
    assert b1 - b0 == 30                                   # duration preserved
    assert sc.employees[0].work_start <= b0 and b1 <= sc.employees[0].work_end

    def mean_wait(schedule):
        params = make_params(sc, frozenset(["break_scheduling"]), schedule)
        days = [gen_day_draws(sc, d, dist) for d in range(6)]
        return float(np.mean([run_day(sc, dist, dd, params)[0].mean_wait
                              for dd in days]))

    original = {0: (600.0, 630.0)}
    assert mean_wait(rec) <= mean_wait(original) + 1e-9
    assert rec[0] != original[0]      # surge break is moved off the rush
