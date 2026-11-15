"""Pure logic: schedules, cycle evaluation, chemistry. No Home Assistant imports."""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from .model import CycleConfig, DosingProgram, Schedule

# ---------------------------------------------------------------- chemistry

NH4_MW = 18.039
NH3_MW = 17.031


def dose_nh4_mg_l(volume_ml: float, nh4_mg_per_ml: float, tank_volume_l: float) -> float:
    """NH4+ concentration added to the tank by a dose of stock solution."""
    return volume_ml * nh4_mg_per_ml / tank_volume_l


def free_nh3_mg_l(nh4_total_mg_l: float, ph: float, temp_c: float) -> float:
    """Free (toxic) NH3 from total ammonia reported as NH4 (Emerson et al. 1975)."""
    pka = 0.09018 + 2729.92 / (273.15 + temp_c)
    fraction = 1.0 / (1.0 + 10 ** (pka - ph))
    return nh4_total_mg_l / NH4_MW * fraction * NH3_MW


# ---------------------------------------------------------------- schedules

def _matches(s: Schedule, d: date) -> bool:
    if s.type in ("daily", "interval"):
        return True
    if s.type == "weekly":
        return d.weekday() in s.weekdays
    if s.type == "monthly":
        if d.weekday() != s.weekday:
            return False
        if s.nth == -1:
            return d.day + 7 > calendar.monthrange(d.year, d.month)[1]
        return (d.day - 1) // 7 + 1 == s.nth
    return False


def add_months(d: date, months: int) -> date:
    """Same day N calendar months later, clamped to the month's last day (29 Nov + 3 -> 28 Feb)."""
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _at(d: date, s: Schedule, tz: tzinfo) -> datetime:
    return datetime.combine(d, s.at, tzinfo=tz)


def prev_slot(s: Schedule, t: datetime, tz: tzinfo) -> datetime | None:
    """Latest calendar slot <= t."""
    d0 = t.astimezone(tz).date()
    for i in range(0, 400):
        d = d0 - timedelta(days=i)
        if _matches(s, d):
            x = _at(d, s, tz)
            if x <= t:
                return x
    return None


def next_slot(s: Schedule, t: datetime, tz: tzinfo) -> datetime | None:
    """Earliest calendar slot > t."""
    d0 = t.astimezone(tz).date()
    for i in range(0, 400):
        d = d0 + timedelta(days=i)
        if _matches(s, d):
            x = _at(d, s, tz)
            if x > t:
                return x
    return None


def compute_due(
    s: Schedule,
    now: datetime,
    tz: tzinfo,
    event_times: list[datetime],
    anchor: datetime,
    early_window_h: float,
    interval_days: int | None = None,
) -> datetime:
    """Return the due time of the current/next occurrence of a task.

    interval: due = (local date of last matching event + N days) at the set time.
    calendar (daily/weekly/monthly): the timeline is tiled into windows
    [slot - early, next_slot - early). The occurrence whose window contains
    `now` is done if a matching event falls inside that window.
    """
    if s.type == "interval":
        if event_times:
            last_day = max(event_times).astimezone(tz).date()
            if s.months and not interval_days:
                return _at(add_months(last_day, s.months), s, tz)
            return _at(last_day + timedelta(days=interval_days or s.days), s, tz)
        if s.start is not None:
            return _at(s.start, s, tz)
        first = _at(anchor.astimezone(tz).date(), s, tz)
        if first < anchor:
            first = _at(anchor.astimezone(tz).date() + timedelta(days=1), s, tz)
        return first

    early = timedelta(hours=early_window_h)
    current = prev_slot(s, now + early, tz)
    upcoming = next_slot(s, now + early, tz)
    if current is None:
        return upcoming
    if current < anchor:
        return upcoming
    win_start, win_end = current - early, upcoming - early
    if any(win_start <= t < win_end for t in event_times):
        return upcoming
    return current


def next_in_rotation(
    rotate: list[str], events: list[tuple[datetime, str]], before: datetime | None
) -> str:
    """Action that follows the last rotation action logged before `before`."""
    last = None
    for ts, action in sorted(events):
        if (before is None or ts < before) and action in rotate:
            last = action
    if last is None:
        return rotate[0]
    return rotate[(rotate.index(last) + 1) % len(rotate)]


def rotation_due(
    s: Schedule,
    now: datetime,
    tz: tzinfo,
    events: list[tuple[datetime, str]],
    rotate: list[str],
    anchor: datetime,
    early_window_h: float,
) -> tuple[datetime, str]:
    """Due time and requested action for a rotating maintenance task.

    The requested action is decided at the start of the occurrence window
    (the action after the last one logged before the window), and only that
    action completes the occurrence. A skipped week therefore never shifts
    the order.
    """
    if s.type == "interval":
        times = [ts for ts, a in events if a in rotate]
        return compute_due(s, now, tz, times, anchor, early_window_h), next_in_rotation(rotate, events, None)

    early = timedelta(hours=early_window_h)
    current = prev_slot(s, now + early, tz)
    upcoming = next_slot(s, now + early, tz)
    if current is None or current < anchor:
        return upcoming, next_in_rotation(rotate, events, upcoming - early)
    win_start, win_end = current - early, upcoming - early
    requested = next_in_rotation(rotate, events, win_start)
    if any(win_start <= ts < win_end and a == requested for ts, a in events):
        return upcoming, next_in_rotation(rotate, events, win_end)
    return current, requested


# ---------------------------------------------------------------- cycle

@dataclass
class CycleStatus:
    phase: str  # start | ammonia | nitrite | verify | complete
    passes: int
    required: int
    params: list[str]
    dose_results: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "passes": self.passes,
            "required": self.required,
            "params": self.params,
            "dose_results": self.dose_results,
        }


def _value(ev: dict[str, Any], param: str) -> float | None:
    vals = (ev.get("payload") or {}).get("values") or {}
    v = vals.get(param)
    return float(v) if v is not None else None


def evaluate_cycle(events: list[dict[str, Any]], cfg: CycleConfig, now: datetime) -> CycleStatus:
    """Evaluate fishless-cycle progress.

    A dose passes when a measurement within test_window_h after it (and
    before the next dose) shows every cycle parameter <= zero_threshold.
    A dose fails if such a measurement shows any of them above threshold.
    Untested doses neither pass nor break the chain.
    The cycle is complete after `required_passes` consecutive passes.
    """
    events = sorted(events, key=lambda e: e["ts"])
    doses = [e for e in events if e["type"] == "dose"]
    meas = [e for e in events if e["type"] == "measurement"]
    thr = cfg.zero_threshold
    window = timedelta(hours=cfg.test_window_h)

    results: list[dict[str, Any]] = []
    for i, d in enumerate(doses):
        end = d["ts"] + window
        if i + 1 < len(doses):
            end = min(end, doses[i + 1]["ts"])
        result = "pending" if now < end else "untested"
        for m in meas:
            if not (d["ts"] < m["ts"] <= end):
                continue
            vals = [_value(m, p) for p in cfg.params]
            if any(v is None for v in vals):
                continue
            if all(v <= thr for v in vals):
                result = "pass"
                break
            result = "fail"
        results.append({"dose_id": d.get("id"), "ts": d["ts"].isoformat(), "result": result})

    passes = 0
    for r in reversed(results):
        if r["result"] == "pass":
            passes += 1
        elif r["result"] == "fail":
            break

    nh4_zero_seen = any((v := _value(m, "NH4")) is not None and v <= thr for m in meas)
    if passes >= cfg.required_passes:
        phase = "complete"
    elif passes >= 1:
        phase = "verify"
    elif nh4_zero_seen:
        phase = "nitrite"
    elif meas:
        phase = "ammonia"
    else:
        phase = "start"
    params = ["NH4"] if phase in ("start", "ammonia") else list(cfg.params)
    return CycleStatus(phase, passes, cfg.required_passes, params, results)


# ---------------------------------------------------------------- dosing programme

def _round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


@dataclass
class ProgramTask:
    kind: str  # measure | dose
    step: str  # baseline | test_dose | response | ramp | maintenance | weekly
    due: datetime
    drops: int | None = None


@dataclass
class ProgramStatus:
    phase: str  # idle | baseline | test_dose | response | ramp | maintenance | stable
    tasks: list[ProgramTask] = field(default_factory=list)
    baseline: float | None = None
    response: float | None = None
    k: float | None = None
    k_source: str | None = None  # measured | label
    k_reason: str | None = None  # below_resolution | deviates
    k_measured: float | None = None
    k_label: float | None = None
    required_ml: float | None = None
    remaining_ml: float | None = None
    rate_ml_per_day: float | None = None
    in_band_count: int = 0
    adjustments: list[dict] = field(default_factory=list)

    def as_dict(self, ml_per_drop: float) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "tasks"}
        d["tasks"] = [
            {"kind": t.kind, "step": t.step, "due": t.due.isoformat(), "drops": t.drops} for t in self.tasks
        ]
        if self.rate_ml_per_day is not None:
            d["rate_drops_per_day"] = round(self.rate_ml_per_day / ml_per_drop, 2)
        if self.remaining_ml is not None:
            d["remaining_drops"] = _round_half_up(self.remaining_ml / ml_per_drop)
        return d


def evaluate_program(
    events: list[dict],
    pc: DosingProgram,
    volume_l: float,
    start: datetime | None,
    now: datetime,
    tz: tzinfo,
) -> ProgramStatus:
    """Derive a dosing programme's state from the event log.

    baseline -> test dose -> response (after response_h) -> capped ramp to target
    -> maintenance (daily dose, scheduled measurement, banded dose factor)
    -> stable after `stable_count` in-band readings in a row (scheduled measurement
    stops; any later out-of-band reading resets the counter).
    """
    st = ProgramStatus(phase="idle")
    if start is None:
        return st
    mpd = pc.ml_per_drop
    st.k_label = pc.label_per_ml_per_l / volume_l
    since = start - timedelta(hours=2)

    def drops_of(e: dict) -> float:
        v = ((e.get("payload") or {}).get("doses") or {}).get(pc.product)
        return float(v) if v not in (None, "") else 0.0

    events = sorted(events, key=lambda e: e["ts"])
    doses = [(e["ts"], drops_of(e)) for e in events
             if e["type"] == "fertilize" and e["ts"] >= since and drops_of(e) > 0]
    meas = []
    for e in events:
        if e["type"] != "measurement" or e["ts"] < since:
            continue
        v = ((e.get("payload") or {}).get("values") or {}).get(pc.parameter)
        if v is not None:
            meas.append((e["ts"], float(v)))

    # 1. baseline
    if not meas:
        st.phase = "baseline"
        st.tasks.append(ProgramTask("measure", "baseline", start))
        return st
    m0 = meas[0]
    st.baseline = m0[1]

    # 2. test dose
    after = [d for d in doses if d[0] >= m0[0]]
    if not after:
        st.phase = "test_dose"
        st.tasks.append(ProgramTask("dose", "test_dose", m0[0], _round_half_up(pc.test_dose_ml / mpd)))
        return st
    d0 = after[0]
    test_ml = d0[1] * mpd

    # 3. response
    resp_from = d0[0] + timedelta(hours=pc.response_h - pc.response_tolerance_h)
    later = [m for m in meas if m[0] >= resp_from]
    if not later:
        st.phase = "response"
        st.tasks.append(ProgramTask("measure", "response", d0[0] + timedelta(hours=pc.response_h)))
        return st
    m1 = later[0]
    st.response = m1[1]
    delta = m1[1] - m0[1]
    st.k_measured = delta / test_ml if test_ml > 0 else None
    if delta < pc.kit_resolution or st.k_measured is None:
        st.k, st.k_source, st.k_reason = st.k_label, "label", "below_resolution"
    elif abs(st.k_measured - st.k_label) / st.k_label > pc.k_tolerance:
        st.k, st.k_source, st.k_reason = st.k_label, "label", "deviates"
    else:
        st.k, st.k_source = st.k_measured, "measured"

    # 4. ramp to target, capped per day
    st.required_ml = max(0.0, (pc.target - m1[1]) / st.k)
    ramp = [d for d in doses if d[0] > m1[0]]
    given, ramp_end = 0.0, None
    if st.required_ml < mpd / 2:
        ramp_end = m1[0]
    else:
        for ts, dr in ramp:
            given += dr * mpd
            if given >= st.required_ml - mpd / 2:
                ramp_end = ts
                break
    if ramp_end is None:
        st.phase = "ramp"
        st.remaining_ml = st.required_ml - given
        due = compute_due(pc.dose_schedule, now, tz, [d[0] for d in ramp], m1[0], pc.dose_early_h)
        drops = max(1, _round_half_up(min(pc.ramp_max_ml_per_day, st.remaining_ml) / mpd))
        st.tasks.append(ProgramTask("dose", "ramp", due, drops))
        return st

    # 5. maintenance
    st.remaining_ml = 0.0
    maint_start = ramp_end
    mm = [m for m in meas if m[0] > maint_start]
    rate = pc.maintenance_start_ml_per_day
    for ts, v in mm:
        band = next(b for b in pc.bands if b.matches(v))
        rate = min(rate * band.factor, pc.ramp_max_ml_per_day)
        st.in_band_count = st.in_band_count + 1 if pc.stable_low <= v <= pc.stable_high else 0
        st.adjustments.append({"ts": ts.isoformat(), "value": v, "factor": band.factor,
                               "rate_ml_per_day": round(rate, 4), "in_band_count": st.in_band_count})
    st.rate_ml_per_day = rate
    stable = st.in_band_count >= pc.stable_count
    st.phase = "stable" if stable else "maintenance"

    def rate_at(t: datetime) -> float:
        r = pc.maintenance_start_ml_per_day
        for a in st.adjustments:
            if datetime.fromisoformat(a["ts"]) < t:
                r = a["rate_ml_per_day"]
        return r

    anchor = maint_start + timedelta(hours=pc.dose_early_h)
    maint_doses = [d[0] for d in doses if d[0] > maint_start]
    due = compute_due(pc.dose_schedule, now, tz, maint_doses, anchor, pc.dose_early_h)

    # Drops per slot: cumulative planned volume rounded, so fractional drops
    # carry over (1.5/day -> 1, 2, 1, 2 ...). Missed days are not made up.
    def planned(slot: datetime) -> int:
        cum = prev = 0.0
        s = next_slot(pc.dose_schedule, anchor - timedelta(seconds=1), tz)
        while s is not None and s <= slot:
            prev = cum
            cum += rate_at(s) / mpd
            s = next_slot(pc.dose_schedule, s, tz)
        return _round_half_up(cum) - _round_half_up(prev)

    for _ in range(60):  # skip zero-drop days
        n = planned(due)
        if n > 0:
            break
        due = next_slot(pc.dose_schedule, due, tz)
    st.tasks.append(ProgramTask("dose", "maintenance", due, n))
    if not stable:
        mdue = compute_due(pc.measure_schedule, now, tz, [m[0] for m in mm], maint_start, pc.measure_early_h)
        st.tasks.append(ProgramTask("measure", "weekly", mdue))
    return st
