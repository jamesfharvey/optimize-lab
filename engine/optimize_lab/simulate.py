"""Discrete-event simulation of one operating day under one policy variant.

Event order at equal timestamps (deterministic, load-bearing):
  break_end < completion < no-show discovery < appointment arrival <
  walk-in arrival < abandonment < break_start < close.
After all events at a timestamp are applied, the summon loop runs:
appointments due (earliest scheduled first, paired per scheduling method),
then walk-ins (employees in roster order, policy picker).

Rules implemented here (spec-normative):
  - New walk-in joins only within [open, close - last_join]; in-progress
    service runs to completion after close (makespan = minutes past close).
  - Breaks are hard: no service may START inside the (daily-shifted) break
    window; a service in progress at break start completes first, then the
    remaining window (possibly shortened, down to zero) is taken.
  - Appointments check in early_summon_max_min before their slot and may be
    summoned from check-in onward (never before — "only if checked in").
    Lateness = max(0, summon - scheduled); lateness <= late_ok_min keeps the
    promise, lateness <= late_acceptable_min is tolerated, beyond that the
    start is unacceptable (punctuality stats track all three). No-shows sit
    on the books until their scheduled time, then drop (the office cannot
    know earlier).
  - Incompletes: a started visit fails with the (prep-adjusted) service
    incomplete rate, consumes U(0.25, 0.6) x effective duration, closes
    without re-queue, accrues to the incomplete time cost, and produces no
    served/wait/CSAT record.
  - Abandonment: a waiting walk-in leaves when their realized wait crosses
    their pre-drawn patience threshold.
  - Visitors still waiting at close (walk-ins and never-summoned shown
    appointments) are turned away.
"""
from __future__ import annotations

import bisect
import heapq
import math
from dataclasses import dataclass

import numpy as np

from . import abandonment, policies
from .csat import (appointment_csat, predicted_csat, promise_range,
                   visit_csat)
from .world import DayDraws, round5

R_BREAK_END = 0
R_COMPLETE = 1
R_NOSHOW = 2
R_APPT_ARR = 3
R_WALK_ARR = 4
R_ABANDON = 5
R_BREAK_START = 6
R_CLOSE = 9

EPS = 1e-9

# visitor status codes
ST_NONE = 0
ST_SERVED = 1
ST_INCOMPLETE = 2
ST_ABANDONED = 3
ST_TURNED_AWAY = 4
ST_NO_SHOW = 5
ST_DEFLECTED = 6

STATUS_NAMES = {
    ST_NONE: "unserved", ST_SERVED: "served", ST_INCOMPLETE: "incomplete",
    ST_ABANDONED: "abandoned", ST_TURNED_AWAY: "turned_away",
    ST_NO_SHOW: "no_show", ST_DEFLECTED: "deflected",
}


class _ForecastDay:
    """Shadow of _Day for the dispatch-forward quote: shares the immutable
    per-day inputs (duration matrix, focus sets, language tables, weights)
    and COPIES the mutable dispatch state, so a forecast never touches the
    real day. Satisfies the same interface the policy pickers read.

    v1.4 arrival-aware extension: ids >= n_real are SYNTHETIC expected
    future walk-ins (deterministic expectation of the active variant's own
    arrival process). They carry no language restriction (the expected-case
    visitor) and exist only inside the forecast."""
    __slots__ = ("sc", "dur", "q", "qpos", "waiting", "arr_t", "next_appt",
                 "focus", "weights", "aging_cap", "late_ok", "busy",
                 "busy_until", "on_break", "n_real", "_real")

    def __init__(self, day, synth_suffix=()):
        self.sc = day.sc
        self.dur = day.dur
        self.focus = day.focus
        self.weights = day.weights
        self.aging_cap = day.aging_cap
        self.late_ok = day.late_ok
        self.n_real = len(day.arr_t)
        if synth_suffix:
            self.arr_t = np.concatenate(
                [day.arr_t, np.array([s[0] for s in synth_suffix])])
            self.waiting = np.concatenate(
                [day.waiting, np.zeros(len(synth_suffix), dtype=bool)])
        else:
            self.arr_t = day.arr_t
            self.waiting = day.waiting.copy()
        self.q = [list(x) for x in day.q]
        self.qpos = list(day.qpos)
        self.busy = list(day.busy)
        self.busy_until = list(day.busy_until)
        self.on_break = list(day.on_break)
        self.next_appt = day.next_appt
        self._real = day

    def lang_ok(self, e, v):
        if v >= self.n_real:
            return True
        return self._real.lang_ok(e, v)

    def is_free(self, e, t):
        emp = self.sc.employees[e]
        return (not self.busy[e] and not self.on_break[e]
                and emp.work_start <= t < emp.work_end)

    def pred_csat(self, e, s_idx):
        return self._real.pred_csat(e, s_idx)


def expected_arrival_schedule(sc, dist, params: "VariantParams") -> list:
    """Deterministic EXPECTED future walk-in stream of the active variant
    (v1.4 arrival-aware quote): per service, the post-deflection,
    post-smoothing expected walk-in count, placed at the arrival shape's
    expectation quantiles (no random draws — quotes are stable and
    seed-independent). Returns a sorted [(time, service_idx)] list; the
    forecast admits only entries after the quote time (earlier expected
    arrivals are already realized, or not, in the live queue)."""
    if sc.visitors_range:
        n_exp = (sc.visitors_range[0] + sc.visitors_range[1]) / 2.0
    else:
        n_exp = float(sc.visitors_per_day)
    base = n_exp * (1.0 - params.deflect_rate)
    out = []
    for s in sc.services:
        share = base * s.share
        if s.appt_eligible:
            share *= (1.0 - params.appt_share)
        k = int(round(share))
        for j in range(k):
            out.append((dist.inverse((j + 0.5) / k), s.idx))
    out.sort()
    return out


@dataclass
class ForecastContext:
    """Live references into run_day state needed by the quote (shared, not
    copied — the forecast copies what it mutates)."""
    pending: dict          # v -> (sched, s_idx, eok) for booked, unstarted appts
    due: list              # checked-in appointments awaiting summon
    pins: dict
    method: str
    brk_win: list          # per employee: (start, end) or None, daily-shifted
    pending_brk: list
    pick: object           # the active policy picker (same fn the engine uses)
    open_m: float
    close_m: float
    early_max: float
    abandonment: bool
    synth: list            # expected future walk-ins [(time, s_idx)], sorted


def dispatch_forward_quote(day, fc: ForecastContext, v: int, t: float) -> float:
    """v1.4 check-in quote: predict walk-in v's wait by running the engine's
    OWN dispatch forward over the current state — same pickers, same duration
    matrix, same collision/reservation/break/appointment logic — with no
    future arrivals. Queue-ahead service times are discounted by the
    configured abandonment curve's conditional survival
    S(tau - arrival)/S(t - arrival): the office cannot know who will walk
    out, but it knows the curve. Appointments are planned as shows (no-shows
    are unknowable before their slot) at undiscounted durations. Incompletes
    are not modeled in the quote. If v cannot be summoned before close, the
    quote saturates at close - t.

    The quote is informational only — it feeds the walk-in promise range and
    the audit trail, never queueing/routing/abandonment decisions (asserted
    by test_quote_is_purely_informational)."""
    sc = day.sc
    E = sc.n_employees
    i0 = bisect.bisect_right(fc.synth, (t, 1 << 30))
    suffix = fc.synth[i0:]
    f = _ForecastDay(day, suffix)
    pending = dict(fc.pending)
    info = dict(fc.pending)          # static details survive forecast pops
    due = list(fc.due)
    pend_brk = list(fc.pending_brk)

    def survival(w, when):
        if not fc.abandonment:
            return 1.0
        s0 = 1.0 - abandonment.prob_abandon_by(t - f.arr_t[w])
        s1 = 1.0 - abandonment.prob_abandon_by(when - f.arr_t[w])
        return s1 / s0 if s0 > 1e-12 else 1.0

    # static future events: break boundaries, shift starts, appointment
    # check-ins (window opens). Completions are tracked dynamically.
    static = []
    for emp in sc.employees:
        bw = fc.brk_win[emp.idx]
        if bw is not None:
            if bw[0] > t:
                static.append((bw[0], 0, emp.idx))   # 0 = break start
            if bw[1] > t:
                static.append((bw[1], 1, emp.idx))   # 1 = break end
        if emp.work_start > t:
            static.append((float(emp.work_start), 2, emp.idx))  # wake-up only
    for a, (sched, _s, _e) in pending.items():
        if a not in due:
            open_at = max(fc.open_m, sched - fc.early_max)
            if open_at > t:
                static.append((open_at, 3, a))       # 3 = appointment check-in
    for k, (st, s_idx) in enumerate(suffix):
        static.append((st, 4, (f.n_real + k, s_idx)))  # 4 = expected walk-in
    static.sort()
    si = 0

    dirty = [True]

    def refresh():
        if dirty[0]:
            plist = [(s0, a, s_idx, eok)
                     for a, (s0, s_idx, eok) in pending.items()]
            f.next_appt = policies.compute_next_appt(sc, plist, fc.pins,
                                                     fc.method)
            dirty[0] = False

    tau = t
    while True:
        refresh()
        progress = True
        while progress:
            progress = False
            if due:
                for a in sorted(due, key=lambda a: (info[a][0], a)):
                    s_idx, eok = info[a][1], info[a][2]
                    if fc.method == "employee_specific":
                        pin = fc.pins.get(a)
                        cands = [pin] if pin is not None else []
                    else:
                        cands = sc.eligible_emps[s_idx]
                    e_take = next((e for e in cands
                                   if eok[e] and f.is_free(e, tau)), None)
                    if e_take is not None:
                        due.remove(a)
                        pending.pop(a, None)
                        dirty[0] = True
                        refresh()
                        f.busy[e_take] = True
                        f.busy_until[e_take] = tau + day.dur[e_take][s_idx]
                        progress = True
            for emp in sc.employees:
                if not f.is_free(emp.idx, tau):
                    continue
                got = fc.pick(f, emp.idx, tau)
                if got is None:
                    continue
                w, s_idx = got
                if w == v:
                    return tau - t
                f.waiting[w] = False
                d = day.dur[emp.idx][s_idx] * survival(w, tau)
                f.busy[emp.idx] = True
                f.busy_until[emp.idx] = tau + max(d, 1e-6)
                progress = True
        # advance to the next event
        nxt = math.inf
        for e in range(E):
            if f.busy[e] and f.busy_until[e] > tau + EPS:
                nxt = min(nxt, f.busy_until[e])
        if si < len(static):
            nxt = min(nxt, static[si][0])
        if not math.isfinite(nxt) or nxt >= fc.close_m:
            return max(0.0, fc.close_m - t)
        tau = nxt
        for e in range(E):
            if f.busy[e] and f.busy_until[e] <= tau + EPS:
                f.busy[e] = False
                if pend_brk[e]:
                    pend_brk[e] = False
                    bw = fc.brk_win[e]
                    if bw is not None and tau < bw[1] - EPS:
                        f.on_break[e] = True
        while si < len(static) and static[si][0] <= tau + EPS:
            _, kind, p = static[si]
            si += 1
            if kind == 0:
                if f.busy[p]:
                    pend_brk[p] = True
                else:
                    f.on_break[p] = True
            elif kind == 1:
                f.on_break[p] = False
                pend_brk[p] = False
            elif kind == 3:
                if p in pending and p not in due:
                    due.append(p)
            elif kind == 4:
                sid, s_idx = p
                f.waiting[sid] = True
                f.q[s_idx].append(sid)


@dataclass
class VariantParams:
    appt_share: float
    distribution: str
    matching: bool
    prep_factor: float = 1.0      # multiplies effective durations
    inc_factor: float = 1.0       # multiplies incomplete rates
    deflect_rate: float = 0.0
    noshow_reduction: float = 0.0
    breaks: dict | None = None    # {emp_idx: (start, end)} overrides (break_scheduling)


@dataclass
class DayMetrics:
    served: float
    turned_away: float
    abandoned: float
    resolved: float
    incompletes: float
    incomplete_min: float
    appts_shown: float
    pct_on_time: float        # lateness <= late_ok_min, over SHOWN appointments
    pct_acceptable: float     # lateness <= late_acceptable_min, over SHOWN
    p50_late: float           # within-day median lateness of summoned appts
    p90_late: float
    max_late: float
    mean_wait: float
    p90_wait: float
    mean_csat: float
    makespan: float


class _Day:
    """Mutable per-day state shared with the policy pickers."""
    __slots__ = (
        "sc", "dur", "q", "qpos", "waiting", "arr_t", "next_appt", "focus",
        "weights", "aging_cap", "late_ok", "busy", "busy_until", "on_break",
        "lang_eok", "vlang", "_pred_csat_cache", "gamma",
    )

    def lang_ok(self, e, v):
        li = self.vlang[v]
        return li < 0 or self.lang_eok[e][li]

    def is_free(self, e, t):
        emp = self.sc.employees[e]
        return (not self.busy[e] and not self.on_break[e]
                and emp.work_start <= t < emp.work_end)

    def pred_csat(self, e, s_idx):
        key = (e, s_idx)
        val = self._pred_csat_cache.get(key)
        if val is None:
            base = self.sc.employees[e].profile[s_idx][1]
            val = predicted_csat(base, self.dur[e][s_idx],
                                 self.sc.services[s_idx].target, self.gamma)
            self._pred_csat_cache[key] = val
        return val


def build_appointment_schedule(sc, dist, draws: DayDraws, params: VariantParams):
    """Resolve which visitors are appointments and their booked times.

    A visitor is an appointment iff their service is appointment-eligible and
    u_appt < share. 'even': slots evenly spread over [open, cutoff], assigned
    in u_slot order. 'match_arrival_pattern': inverse-CDF of the arrival shape
    at u_slot. Booked times round to 5 minutes.
    """
    n = draws.n
    share = params.appt_share
    is_appt = np.zeros(n, dtype=bool)
    sched = np.full(n, np.nan)
    if share <= 0.0:
        return is_appt, sched
    elig = np.array([sc.services[s].appt_eligible for s in draws.service])
    is_appt = (draws.u_appt < share) & elig
    ids = np.nonzero(is_appt)[0]
    if len(ids) == 0:
        return is_appt, sched
    lo, hi = float(sc.open_min), float(sc.cutoff)
    if params.distribution == "even":
        order = sorted(ids, key=lambda v: (draws.u_slot[v], v))
        width = hi - lo
        for i, v in enumerate(order):
            sched[v] = min(hi, max(lo, round5(lo + (i + 0.5) * width / len(order))))
    else:  # match_arrival_pattern
        for v in ids:
            sched[v] = min(hi, max(lo, round5(dist.inverse(draws.u_slot[v]))))
    return is_appt, sched


def _promise_cols(sc, v, is_appt, deflected, promised, status):
    """Audit-trail promise columns (low, center, high). Walk-ins carry the
    full quoted range; appointments are promised punctuality (center =
    late_ok), deflected visitors carry no promise."""
    if status[v] == ST_DEFLECTED:
        return ("", "", "")
    if is_appt[v] and not deflected[v]:
        return ("", round(float(promised[v]), 2), "")
    low, high = promise_range(float(promised[v]), sc.range_k)
    return (round(low, 2), round(float(promised[v]), 2), round(high, 2))


def run_day(sc, dist, draws: DayDraws, params: VariantParams, collect: bool = False):
    n = draws.n
    E, S = sc.n_employees, sc.n_services
    open_m, close_m = float(sc.open_min), float(sc.close_min)
    early_max = float(sc.baseline.early_summon_max)
    late_ok = float(sc.baseline.late_ok)
    late_acc = float(sc.baseline.late_acceptable)
    method = sc.baseline.scheduling_method

    # ---- interpret the day's draws under this variant ----
    deflected = (draws.u_deflect < params.deflect_rate) if params.deflect_rate > 0 \
        else np.zeros(n, dtype=bool)
    is_appt, sched = build_appointment_schedule(sc, dist, draws, params)

    ns_hard = sc.no_show_rate * (1.0 - params.noshow_reduction)
    no_show = is_appt & ~deflected & (draws.u_noshow < ns_hard)

    # ---- effective durations: target / (efficiency x form), prep-adjusted ----
    dur = [[math.inf] * S for _ in range(E)]
    for emp in sc.employees:
        for s_idx, (eff, _csat) in emp.profile.items():
            dur[emp.idx][s_idx] = (sc.services[s_idx].target
                                   / (eff * draws.form[emp.idx][s_idx])
                                   * params.prep_factor)

    # ---- day state ----
    day = _Day()
    day.sc = sc
    day.dur = dur
    day.q = [[] for _ in range(S)]
    day.qpos = [0] * S
    day.waiting = np.zeros(n, dtype=bool)
    day.arr_t = np.zeros(n)
    day.next_appt = [math.inf] * E
    day.focus = policies.compute_focus(sc, draws.form) if params.matching else None
    day.weights = sc.weights
    day.aging_cap = float(sc.aging_cap)
    day.late_ok = late_ok
    day.busy = [False] * E
    day.busy_until = [0.0] * E
    day.on_break = [False] * E
    day.gamma = sc.gamma_duration
    day._pred_csat_cache = {}
    lang_names = [name for name, _ in sc.lang_prefs]
    day.lang_eok = [[name in emp.languages for name in lang_names]
                    for emp in sc.employees]
    day.vlang = draws.lang

    pick = policies.pick_optimized if params.matching else policies.pick_baseline

    # break windows: optional override (break_scheduling lever) + daily shift,
    # clamped inside the shift while preserving duration
    brk_win = [None] * E
    for emp in sc.employees:
        base = None
        if emp.brk is not None:
            base = (params.breaks or {}).get(emp.idx, (emp.brk.start, emp.brk.end))
        if base is None:
            continue
        b0 = base[0] + draws.brk_shift[emp.idx]
        b1 = base[1] + draws.brk_shift[emp.idx]
        if b0 < emp.work_start:
            b1 += emp.work_start - b0
            b0 = emp.work_start
        if b1 > emp.work_end:
            b0 -= b1 - emp.work_end
            b1 = emp.work_end
        brk_win[emp.idx] = (b0, b1)

    pending_brk = [False] * E

    # appointment bookkeeping
    eok_all = {}      # v -> per-employee language-eligibility row (cached)

    def eok_row(v):
        li = day.vlang[v]
        if li < 0:
            return [True] * E
        return [day.lang_eok[e][li] for e in range(E)]

    booked = []       # all booked appointments incl. eventual no-shows (pins source)
    for v in range(n):
        if is_appt[v] and not deflected[v]:
            eok_all[v] = eok_row(v)
            booked.append((float(sched[v]), v, int(draws.service[v]), eok_all[v]))
    pins = policies.assign_pins(sc, booked) if method == "employee_specific" else {}
    pending = {b[1]: (b[0], b[2], b[3]) for b in booked}
    res_dirty = [True]

    due = []          # shown appointments whose summon window is open
    started = np.zeros(n, dtype=bool)

    # per-visitor outcome tracking
    status = np.zeros(n, dtype=np.int8)
    status[deflected] = ST_DEFLECTED
    status[no_show] = ST_NO_SHOW
    promised = np.zeros(n)
    start_t = np.full(n, np.nan)
    end_t = np.full(n, np.nan)
    served_by = np.full(n, -1, dtype=np.int64)
    csat_v = np.full(n, np.nan)
    wait_v = np.full(n, np.nan)

    fc = ForecastContext(
        pending=pending, due=due, pins=pins, method=method,
        brk_win=brk_win, pending_brk=pending_brk, pick=pick,
        open_m=open_m, close_m=close_m, early_max=early_max,
        abandonment=sc.abandonment_enabled,
        synth=expected_arrival_schedule(sc, dist, params),
    )

    waits, csats = [], []
    lateness = []        # summon lateness of every summoned appointment
    counters = {
        "served": 0, "turned_away": 0, "abandoned": 0, "incompletes": 0,
        "incomplete_min": 0.0, "appts_shown": 0,
    }
    last_completion = [open_m]
    closed = [False]

    # ---- event heap ----
    events = []
    seq = 0

    def push(t, rank, payload):
        nonlocal seq
        heapq.heappush(events, (t, rank, seq, payload))
        seq += 1

    for emp in sc.employees:
        if brk_win[emp.idx] is not None:
            b0, b1 = brk_win[emp.idx]
            if b1 > b0:
                push(b0, R_BREAK_START, emp.idx)
                push(b1, R_BREAK_END, emp.idx)
    push(close_m, R_CLOSE, -1)
    for v in range(n):
        if deflected[v]:
            continue
        if is_appt[v]:
            if no_show[v]:
                push(float(sched[v]), R_NOSHOW, v)
            else:
                # check-in: summonable from here on, never before
                push(max(open_m, float(sched[v]) - early_max), R_APPT_ARR, v)
        else:
            push(float(draws.arrival[v]), R_WALK_ARR, v)

    def refresh_reservations():
        if res_dirty[0]:
            plist = [(s, v, sv, eok) for v, (s, sv, eok) in pending.items()]
            day.next_appt = policies.compute_next_appt(sc, plist, pins, method)
            res_dirty[0] = False

    def start_service(e, v, t, as_appt):
        s_idx = int(draws.service[v])
        d = day.dur[e][s_idx]
        inc_rate = sc.services[s_idx].incomplete_rate * params.inc_factor
        incomplete = inc_rate > 0.0 and draws.u_inc[v] < inc_rate
        actual = d * (0.25 + 0.35 * draws.u_inc_cost[v]) if incomplete else d
        started[v] = True
        start_t[v] = t
        end_t[v] = t + actual
        served_by[v] = e
        if as_appt:
            due.remove(v)
            pending.pop(v, None)
            res_dirty[0] = True
            wait = max(0.0, t - float(sched[v]))   # = summon lateness
            lateness.append(wait)
            promised[v] = late_ok                  # the punctuality promise
        else:
            day.waiting[v] = False
            wait = t - day.arr_t[v]
        wait_v[v] = wait
        if incomplete:
            counters["incompletes"] += 1
            counters["incomplete_min"] += actual
            status[v] = ST_INCOMPLETE
        else:
            counters["served"] += 1
            waits.append(wait)
            base = sc.employees[e].profile[s_idx][1]
            if as_appt:
                cs = appointment_csat(base, wait, late_ok, late_acc, actual,
                                      sc.services[s_idx].target,
                                      sc.gamma_duration)
            else:
                cs = visit_csat(base, wait, promised[v], actual,
                                sc.services[s_idx].target, sc.alpha_wait,
                                sc.beta_early, sc.beta_late,
                                sc.gamma_duration, sc.range_k)
            csats.append(cs)
            csat_v[v] = cs
            status[v] = ST_SERVED
        day.busy[e] = True
        day.busy_until[e] = t + actual
        push(t + actual, R_COMPLETE, e)

    def try_assign(t):
        if closed[0] or t >= close_m:
            return
        refresh_reservations()
        progress = True
        while progress:
            progress = False
            # appointments first, earliest scheduled time
            if due:
                for v in sorted(due, key=lambda v: (float(sched[v]), v)):
                    s_idx = int(draws.service[v])
                    eok = eok_all[v]
                    if method == "employee_specific":
                        cand = pins.get(v)
                        cands = [cand] if cand is not None else []
                    else:
                        cands = sc.eligible_emps[s_idx]
                    e_take = next((e for e in cands
                                   if eok[e] and day.is_free(e, t)), None)
                    if e_take is not None:
                        start_service(e_take, v, t, as_appt=True)
                        refresh_reservations()
                        progress = True
            # then walk-ins, employees in roster order
            for emp in sc.employees:
                if not day.is_free(emp.idx, t):
                    continue
                got = pick(day, emp.idx, t)
                if got is not None:
                    start_service(emp.idx, got[0], t, as_appt=False)
                    refresh_reservations()
                    progress = True

    # ---- main loop ----
    while events:
        t = events[0][0]
        while events and events[0][0] == t:
            _, rank, _, payload = heapq.heappop(events)
            if rank == R_BREAK_END:
                day.on_break[payload] = False
                pending_brk[payload] = False
            elif rank == R_COMPLETE:
                e = payload
                day.busy[e] = False
                if t > last_completion[0]:
                    last_completion[0] = t
                if pending_brk[e]:
                    pending_brk[e] = False
                    if t < brk_win[e][1]:
                        day.on_break[e] = True   # shortened break, ends at window end
            elif rank == R_NOSHOW:
                pending.pop(payload, None)
                res_dirty[0] = True
            elif rank == R_APPT_ARR:
                due.append(payload)
                counters["appts_shown"] += 1
            elif rank == R_WALK_ARR:
                v = payload
                day.waiting[v] = True
                day.arr_t[v] = t
                day.q[int(draws.service[v])].append(v)
                promised[v] = dispatch_forward_quote(day, fc, v, t)
                if sc.abandonment_enabled and math.isfinite(draws.patience[v]):
                    push(t + float(draws.patience[v]), R_ABANDON, v)
            elif rank == R_ABANDON:
                v = payload
                if day.waiting[v] and not closed[0]:
                    day.waiting[v] = False
                    counters["abandoned"] += 1
                    status[v] = ST_ABANDONED
                    wait_v[v] = t - day.arr_t[v]
            elif rank == R_BREAK_START:
                e = payload
                if day.busy[e]:
                    pending_brk[e] = True
                else:
                    day.on_break[e] = True
            elif rank == R_CLOSE:
                closed[0] = True
                for v in range(n):
                    if day.waiting[v]:
                        day.waiting[v] = False
                        counters["turned_away"] += 1
                        status[v] = ST_TURNED_AWAY
                for v in list(due):
                    counters["turned_away"] += 1
                    status[v] = ST_TURNED_AWAY
                due.clear()
        if not closed[0]:
            try_assign(t)

    shown = counters["appts_shown"]
    # pct_* denominators are SHOWN appointments: a shown appointment never
    # summoned by close counts against both percentages. Lateness percentiles
    # are over summoned appointments (lateness is undefined otherwise).
    if shown > 0:
        n_ok = sum(1 for x in lateness if x <= late_ok + EPS)
        n_acc = sum(1 for x in lateness if x <= late_acc + EPS)
        pct_on_time = n_ok / shown
        pct_acceptable = n_acc / shown
    else:
        pct_on_time = pct_acceptable = 1.0
    metrics = DayMetrics(
        served=float(counters["served"]),
        turned_away=float(counters["turned_away"]),
        abandoned=float(counters["abandoned"]),
        resolved=float(int(deflected.sum())),
        incompletes=float(counters["incompletes"]),
        incomplete_min=counters["incomplete_min"],
        appts_shown=float(shown),
        pct_on_time=pct_on_time,
        pct_acceptable=pct_acceptable,
        p50_late=float(np.percentile(lateness, 50)) if lateness else 0.0,
        p90_late=float(np.percentile(lateness, 90)) if lateness else 0.0,
        max_late=float(max(lateness)) if lateness else 0.0,
        mean_wait=float(np.mean(waits)) if waits else 0.0,
        p90_wait=float(np.percentile(waits, 90)) if waits else 0.0,
        mean_csat=float(np.mean(csats)) if csats else 0.0,
        makespan=max(0.0, last_completion[0] - close_m),
    )
    rows = None
    if collect:
        rows = []
        for v in range(n):
            st = int(status[v])
            rows.append((
                v,
                "appointment" if (is_appt[v] and not deflected[v]) else "walkin",
                sc.services[int(draws.service[v])].id,
                lang_names[day.vlang[v]] if day.vlang[v] >= 0 else "",
                round(float(sched[v]), 2) if is_appt[v] and not deflected[v]
                else round(float(draws.arrival[v]), 2),
                *_promise_cols(sc, v, is_appt, deflected, promised, status),
                round(float(start_t[v]), 2) if started[v] else "",
                round(float(end_t[v]), 2) if started[v] else "",
                sc.employees[int(served_by[v])].id if served_by[v] >= 0 else "",
                STATUS_NAMES[st],
                round(float(wait_v[v]), 2) if not math.isnan(wait_v[v]) else "",
                round(float(csat_v[v]), 2) if not math.isnan(csat_v[v]) else "",
            ))
    return metrics, rows
