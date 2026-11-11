"""Pure logic: schedules, cycle evaluation, chemistry. No Home Assistant imports."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from .model import CycleConfig, Schedule

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
        days = interval_days or s.days
        if event_times:
            last = max(event_times)
            return _at(last.astimezone(tz).date() + timedelta(days=days), s, tz)
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
