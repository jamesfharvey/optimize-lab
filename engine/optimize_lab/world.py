"""World generation: arrival distribution and per-day pre-drawn randomness.

Pairing contract: ALL stochastic values a day can consume are drawn here,
once per day, in a fixed order, from numpy's default_rng seeded with
SeedSequence((random_seed, day_index)). Every policy variant then interprets
the same DayDraws deterministically, which makes the Monte-Carlo paired
(identical arrival / no-show / form / break draws across runs).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import abandonment
from .config import Scenario


class ArrivalDist:
    """Piecewise-constant arrival rate over the joinable window [open, cutoff]."""

    def __init__(self, sc: Scenario):
        lo, hi = float(sc.open_min), float(sc.cutoff)
        pattern = sc.arrival_pattern
        shape = pattern["shape"]
        bounds = {lo, hi}
        if shape in ("single_surge", "double_surge"):
            from .config import parse_hhmm
            self._surges = [(max(lo, parse_hhmm(s["start"])),
                             min(hi, parse_hhmm(s["end"])),
                             float(s["multiplier"]))
                            for s in pattern.get("surges", [])]
            for s0, s1, _ in self._surges:
                if s0 < s1:
                    bounds.add(s0)
                    bounds.add(s1)
        elif shape == "custom":
            weights = pattern.get("custom_hourly_weights") or []
            self._hours = []
            t = lo
            i = 0
            while t < hi and i < len(weights):
                t1 = min(hi, t + 60.0)
                self._hours.append((t, t1, float(weights[i])))
                bounds.add(t1)
                t = t1
                i += 1
            if not self._hours:
                self._hours = [(lo, hi, 1.0)]
        edges = sorted(b for b in bounds if lo <= b <= hi)
        self.segments = []
        for a, b in zip(edges[:-1], edges[1:]):
            mid = (a + b) / 2.0
            self.segments.append((a, b, self._rate_at(mid, shape)))
        total = sum((b - a) * r for a, b, r in self.segments)
        if total <= 0:
            raise ValueError("arrival pattern has zero total weight")
        self._cum = []
        acc = 0.0
        for a, b, r in self.segments:
            acc += (b - a) * r / total
            self._cum.append(acc)
        self._cum[-1] = 1.0

    def _rate_at(self, t: float, shape: str) -> float:
        if shape == "uniform":
            return 1.0
        if shape in ("single_surge", "double_surge"):
            rate = 1.0
            for s0, s1, mult in self._surges:
                if s0 <= t < s1:
                    rate *= mult
            return rate
        # custom
        for a, b, w in self._hours:
            if a <= t < b:
                return w
        return self._hours[-1][2]

    def inverse(self, u: float) -> float:
        """u in [0,1) -> arrival time in minutes."""
        prev = 0.0
        for (a, b, _r), c in zip(self.segments, self._cum):
            if u <= c:
                span = c - prev
                frac = 0.0 if span <= 0 else (u - prev) / span
                return a + frac * (b - a)
            prev = c
        return self.segments[-1][1]


@dataclass
class DayDraws:
    """Everything random about one simulated day, pre-drawn."""
    day: int
    n: int                    # visitors today
    u_appt: np.ndarray        # appointment-selection uniform per visitor
    arrival: np.ndarray       # walk-in check-in time (used when visitor is a walk-in)
    u_slot: np.ndarray        # appointment slot-assignment uniform
    service: np.ndarray       # service idx per visitor
    lang: np.ndarray          # language requirement index into scenario.lang_prefs, -1 = none
    patience: np.ndarray      # walk-in patience threshold in minutes (inf = never abandons)
    u_noshow: np.ndarray
    u_deflect: np.ndarray
    u_inc: np.ndarray         # incomplete-failure uniform
    u_inc_cost: np.ndarray    # incomplete time-cost uniform (maps to U(0.25, 0.6))
    form: np.ndarray          # (E, S) daily form multipliers, N(1, jitter) clipped [0.6, 1.4]
    brk_shift: np.ndarray     # per-employee break shift (minutes, rounded to 5)


def gen_day_draws(sc: Scenario, day: int, arrival_dist: ArrivalDist) -> DayDraws:
    rng = np.random.default_rng(np.random.SeedSequence((sc.seed, day)))

    # Draw order is fixed and load-bearing: do not reorder.
    if sc.visitors_range:
        lo, hi = sc.visitors_range
        n = int(rng.integers(lo, hi + 1))
    else:
        n = sc.visitors_per_day

    u_appt = rng.random(n)
    u_arrival = rng.random(n)
    u_slot = rng.random(n)
    u_service = rng.random(n)
    u_lang = rng.random(n)
    u_patience = rng.random(n)
    u_noshow = rng.random(n)
    u_deflect = rng.random(n)
    u_inc = rng.random(n)
    u_inc_cost = rng.random(n)
    form = np.clip(
        rng.normal(1.0, sc.jitter, size=(sc.n_employees, sc.n_services)),
        0.6, 1.4,
    )
    u_brk = rng.random(sc.n_employees)

    shares = np.cumsum([s.share for s in sc.services])
    shares[-1] = 1.0
    service = np.searchsorted(shares, u_service, side="right").astype(np.int64)
    service = np.minimum(service, sc.n_services - 1)

    lang = np.full(n, -1, dtype=np.int64)
    if sc.lang_prefs:
        cum = 0.0
        for li, (_, share) in enumerate(sc.lang_prefs):
            lang[(u_lang >= cum) & (u_lang < cum + share)] = li
            cum += share

    arrival = np.array([arrival_dist.inverse(u) for u in u_arrival])
    patience = np.array([abandonment.threshold_from_u(u) for u in u_patience])

    brk_shift = np.zeros(sc.n_employees)
    for e in sc.employees:
        if e.brk is not None:
            raw = (u_brk[e.idx] * 2.0 - 1.0) * e.brk.variability
            brk_shift[e.idx] = 5.0 * round(raw / 5.0)

    return DayDraws(
        day=day, n=n, u_appt=u_appt, arrival=arrival, u_slot=u_slot,
        service=service, lang=lang, patience=patience, u_noshow=u_noshow,
        u_deflect=u_deflect, u_inc=u_inc, u_inc_cost=u_inc_cost,
        form=form, brk_shift=brk_shift,
    )


def round5(x: float) -> float:
    return 5.0 * round(x / 5.0)
