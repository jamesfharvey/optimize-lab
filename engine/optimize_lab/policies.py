"""Routing policies: baseline FIFO and optimized matching.

Both policies share two hard rules:
  - eligibility filters (service profile, language, shift, break) and
  - the appointment collision rule: at decision time t an employee e may not
    start a drop-in v if t + eff_duration(e, v) > next_appt_start(e) — unless
    EVERY eligible employee for v is excluded the same way, in which case the
    free employee with the shortest overrun may take v, and only if doing so
    still summons e's appointment within +grace.

next_appt_start(e) comes from a reservation forecast:
  - employee_specific: appointments are pinned at generation (round-robin =
    greedy fewest-pins-first in booked-time order among eligible employees);
    next_appt_start(e) is e's earliest unserved pinned appointment.
  - capacity / round_robin: pairing happens at summon time, so the forecast
    greedily assigns upcoming appointments (booked-time order) to eligible
    employees with the fewest assignments; this is a v1 simplification,
    documented in the README. capacity and round_robin behave identically
    at summon time in v1.
"""
from __future__ import annotations

import math

EPS = 1e-9


def compute_focus(sc, form=None):
    """Per-employee focus sets for the matching lever.

    rate(e, s) = efficiency(e, s) x form(e, s); an employee focuses on the
    services where their rate is at/above the team mean rate for that service.
    Guarantees: no employee has an empty focus set (keep their best service);
    every service keeps at least one focused employee (add the best one back).
    With form=None the expected form (1.0) is used — that static variant feeds
    the focus_recommendation in the report.
    """
    E = sc.n_employees

    def rate(e_idx, s_idx):
        eff = sc.employees[e_idx].profile[s_idx][0]
        return eff * (form[e_idx][s_idx] if form is not None else 1.0)

    team_mean = []
    for s in sc.services:
        rates = [rate(e, s.idx) for e in sc.eligible_emps[s.idx]]
        team_mean.append(sum(rates) / len(rates))

    focus = []
    for emp in sc.employees:
        kept = {s for s in emp.profile if rate(emp.idx, s) >= team_mean[s] - EPS}
        if not kept:
            kept = {max(emp.profile, key=lambda s: (rate(emp.idx, s), -s))}
        focus.append(kept)

    for s in sc.services:
        if not any(s.idx in focus[e] for e in sc.eligible_emps[s.idx]):
            best = max(sc.eligible_emps[s.idx], key=lambda e: (rate(e, s.idx), -e))
            focus[best].add(s.idx)
    return focus


def assign_pins(sc, appts):
    """employee_specific: pin each booked appointment (sched-time order) to the
    eligible employee with the fewest pins so far. appts: [(sched, v, s_idx, lang_ok_fn_input)].
    Returns {visitor: employee_idx or None}."""
    counts = [0] * sc.n_employees
    pins = {}
    for sched, v, s_idx, eok in sorted(appts, key=lambda a: (a[0], a[1])):
        elig = [e for e in sc.eligible_emps[s_idx] if eok[e]]
        if not elig:
            pins[v] = None
            continue
        best = min(elig, key=lambda e: (counts[e], e))
        counts[best] += 1
        pins[v] = best
    return pins


def compute_next_appt(sc, pending, pins, method):
    """next_appt_start per employee from currently-booked, not-yet-started
    appointments. pending: [(sched, v, s_idx, eok)]."""
    nxt = [math.inf] * sc.n_employees
    if method == "employee_specific":
        for sched, v, s_idx, eok in pending:
            e = pins.get(v)
            if e is not None and sched < nxt[e]:
                nxt[e] = sched
        return nxt
    counts = [0] * sc.n_employees
    for sched, v, s_idx, eok in sorted(pending, key=lambda a: (a[0], a[1])):
        elig = [e for e in sc.eligible_emps[s_idx]
                if eok[e] and sc.employees[e].work_end > sched]
        if not elig:
            continue
        best = min(elig, key=lambda e: (counts[e], e))
        counts[best] += 1
        if sched < nxt[best]:
            nxt[best] = sched
    return nxt


def _collision_allowed(day, e, v, s_idx, t):
    """Apply the collision rule (with its all-blocked exception) for employee e
    taking drop-in v of service s at time t."""
    d = day.dur[e][s_idx]
    if t + d <= day.next_appt[e] + EPS:
        return True
    # Exception: only if EVERY eligible employee is excluded the same way.
    for e2 in day.sc.eligible_emps[s_idx]:
        if not day.lang_ok(e2, v):
            continue
        if t + day.dur[e2][s_idx] <= day.next_appt[e2] + EPS:
            return False  # someone could take it cleanly; e stays excluded
    # All excluded -> the FREE employee with the shortest overrun may take it,
    # if the overrun still summons e's appointment within +grace.
    free_elig = [e2 for e2 in day.sc.eligible_emps[s_idx]
                 if day.lang_ok(e2, v) and day.is_free(e2, t)]
    if not free_elig:
        return False
    overrun = {e2: t + day.dur[e2][s_idx] - day.next_appt[e2] for e2 in free_elig}
    shortest = min(free_elig, key=lambda e2: (overrun[e2], e2))
    if shortest != e:
        return False
    return t + d <= day.next_appt[e] + day.grace + EPS


def _heads(day, e, t, services):
    """Oldest waiting, language-eligible, collision-allowed walk-in per service.
    Returns [(v, s_idx, wait, dur)]."""
    out = []
    for s_idx in services:
        q = day.q[s_idx]
        pos = day.qpos[s_idx]
        # advance the shared head pointer past departed visitors
        while pos < len(q) and not day.waiting[q[pos]]:
            pos += 1
        day.qpos[s_idx] = pos
        head = None
        for i in range(pos, len(q)):
            v = q[i]
            if day.waiting[v] and day.lang_ok(e, v):
                head = v
                break
        if head is None:
            continue
        if not _collision_allowed(day, e, head, s_idx, t):
            continue
        out.append((head, s_idx, t - day.arr_t[head], day.dur[e][s_idx]))
    return out


def pick_baseline(day, e, t):
    """Pure FIFO: longest current wait among e's eligible candidates."""
    cands = _heads(day, e, t, day.sc.employees[e].profile.keys())
    if not cands:
        return None
    return min(cands, key=lambda c: (-c[2], c[0]))[:2]


def pick_optimized(day, e, t):
    """Aging cap first (qualified services, longest wait); otherwise blended
    score over focused candidates; falls back to all qualified services when
    no focused candidate is waiting (work-conserving, documented)."""
    profile = day.sc.employees[e].profile.keys()
    all_cands = _heads(day, e, t, profile)
    if not all_cands:
        return None
    aged = [c for c in all_cands if c[2] >= day.aging_cap - EPS]
    if aged:
        return min(aged, key=lambda c: (-c[2], c[0]))[:2]
    focused = [c for c in all_cands if c[1] in day.focus[e]]
    pool = focused if focused else all_cands
    return _blended_pick(day, e, pool)[:2]


def _blended_pick(day, e, cands):
    if len(cands) == 1:
        return cands[0]
    wt, ww, wc = day.weights
    durs = [c[3] for c in cands]
    waits = [c[2] for c in cands]
    pcs = [day.pred_csat(e, c[1]) for c in cands]

    def norm(vals):
        lo, hi = min(vals), max(vals)
        span = hi - lo
        if span <= EPS:
            return [0.0] * len(vals)
        return [(x - lo) / span for x in vals]

    ndur, nwait, ncsat = norm(durs), norm(waits), norm(pcs)
    best, best_key = None, None
    for i, c in enumerate(cands):
        score = wt * (1.0 - ndur[i]) + ww * nwait[i] + wc * ncsat[i]
        key = (-score, -c[2], c[1], c[0])
        if best_key is None or key < best_key:
            best, best_key = c, key
    return best
