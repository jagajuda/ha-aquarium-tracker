import importlib.util
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "aquarium_tracker"
# Load model/logic/store as a fake package without importing HA's __init__
pkg = types.ModuleType("aqt")
pkg.__path__ = [str(PKG)]
sys.modules["aqt"] = pkg
for name in ("model", "logic", "store"):
    spec = importlib.util.spec_from_file_location(f"aqt.{name}", PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"aqt.{name}"] = mod
    spec.loader.exec_module(mod)

from aqt.model import parse_config, ConfigError  # noqa: E402
from aqt.logic import compute_due, evaluate_cycle, dose_nh4_mg_l, free_nh3_mg_l  # noqa: E402
from aqt.store import EventStore  # noqa: E402

TZ = ZoneInfo("Europe/Istanbul")
CFG = parse_config(yaml.safe_load(open(ROOT / "aquarium_tracker" / "tanks.yaml")))


def t(s):
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def test_config():
    big = CFG.tanks["big_tank"]
    assert abs(big.stock_solutions["nh4cl"].nh4_mg_per_ml - 33.72) < 0.01
    assert big.tasks["cycle_test"].modes == ["cycling"]
    assert CFG.tanks["nano_tank"].tasks["water_change"].schedule.weekdays == [5]
    assert big.tasks["cycle_test"].schedule.at.hour == 21


def test_unquoted_time():
    raw = yaml.safe_load(open(ROOT / "aquarium_tracker" / "tanks.yaml"))
    raw["tanks"]["nano_tank"]["tasks"]["fertilize"]["schedule"]["at"] = 1080  # 18:00 sexagesimal
    c = parse_config(raw)
    assert c.tanks["nano_tank"].tasks["fertilize"].schedule.at.hour == 18


def test_bad_config():
    with pytest.raises(ConfigError):
        parse_config({"parameters": {"NH4": {}}, "tanks": {"x": {"net_volume_l": 0}}})


def test_chem():
    assert abs(dose_nh4_mg_l(1, 33.72, 105) - 0.321) < 0.001
    v = free_nh3_mg_l(1.0, 7.5, 25)
    assert 0.015 < v < 0.02


def test_interval_due():
    task = CFG.tanks["big_tank"].tasks["cycle_test"]
    anchor = t("2026-11-11T10:00")
    due = compute_due(task.schedule, anchor, TZ, [], anchor, 6)
    assert due == t("2026-11-11T21:00")
    due = compute_due(task.schedule, t("2026-11-11T22:00"), TZ, [t("2026-11-11T21:30")], anchor, 6)
    assert due == t("2026-11-13T21:00")
    due = compute_due(task.schedule, t("2026-11-11T22:00"), TZ, [t("2026-11-11T21:30")], anchor, 6, interval_days=1)
    assert due == t("2026-11-12T21:00")


def test_daily_due():
    task = CFG.tanks["nano_tank"].tasks["fertilize"]
    s = task.schedule
    anchor = t("2026-11-11T00:00")
    # yesterday's 18:00 missed (no anchor) -> stays overdue until today's window opens
    assert compute_due(s, t("2026-11-11T09:00"), TZ, [], t("2026-11-01T00:00"), 6) == t("2026-11-10T18:00")
    # morning, nothing logged -> today's 18:00
    assert compute_due(s, t("2026-11-11T09:00"), TZ, [], anchor, 6) == t("2026-11-11T18:00")
    # fertilized at 13:00 (inside 6h early window) -> tomorrow
    assert compute_due(s, t("2026-11-11T14:00"), TZ, [t("2026-11-11T13:00")], anchor, 6) == t("2026-11-12T18:00")
    # fertilized at 10:00 (outside window) -> still today
    assert compute_due(s, t("2026-11-11T14:00"), TZ, [t("2026-11-11T10:00")], anchor, 6) == t("2026-11-11T18:00")
    # 20:00, not done -> overdue today's slot
    assert compute_due(s, t("2026-11-11T20:00"), TZ, [], anchor, 6) == t("2026-11-11T18:00")
    # anchor after slot -> skip it
    assert compute_due(s, t("2026-11-11T20:00"), TZ, [], t("2026-11-11T19:00"), 6) == t("2026-11-12T18:00")


def test_weekly_monthly():
    wc = CFG.tanks["nano_tank"].tasks["water_change"].schedule
    anchor = t("2026-11-10T00:00")
    # Wed 11 Nov -> Sat 14 Nov 14:00
    assert compute_due(wc, t("2026-11-11T09:00"), TZ, [], anchor, 24) == t("2026-11-14T14:00")
    # done Friday 15:00 -> next Saturday
    assert compute_due(wc, t("2026-11-13T16:00"), TZ, [t("2026-11-13T15:00")], anchor, 24) == t("2026-11-21T14:00")
    mt = CFG.tanks["nano_tank"].tasks["monthly_test"].schedule
    assert compute_due(mt, t("2026-11-11T09:00"), TZ, [], anchor, 48) == t("2026-12-06T11:00")


def ev(type_, ts, **vals):
    return {"id": None, "type": type_, "ts": t(ts), "payload": {"values": vals}}


def test_cycle():
    cfg = CFG.tanks["big_tank"].cycle
    now = t("2026-11-30T12:00")
    evs = [ev("dose", "2026-11-10T21:00"), ev("measurement", "2026-11-12T21:00", NH4=2.0)]
    st = evaluate_cycle(evs, cfg, now)
    assert st.phase == "ammonia" and st.params == ["NH4"]
    evs += [ev("measurement", "2026-11-18T21:00", NH4=0.0)]
    assert evaluate_cycle(evs, cfg, now).phase == "nitrite"
    evs += [ev("dose", "2026-11-20T21:00"), ev("measurement", "2026-11-21T20:00", NH4=0, NO2=0.5)]
    st = evaluate_cycle(evs, cfg, now)
    assert st.phase == "nitrite" and st.dose_results[-1]["result"] == "fail"
    evs += [ev("dose", "2026-11-22T21:00"), ev("measurement", "2026-11-23T20:00", NH4=0, NO2=0)]
    assert evaluate_cycle(evs, cfg, now).phase == "verify"
    evs += [ev("dose", "2026-11-24T21:00")]  # untested
    evs += [ev("dose", "2026-11-26T21:00"), ev("measurement", "2026-11-27T19:00", NH4=0, NO2=0)]
    st = evaluate_cycle(evs, cfg, now)
    assert st.phase == "complete" and st.passes == 2


def test_store(tmp_path):
    s = EventStore(str(tmp_path / "e.db"))
    s.open()
    e = s.add_event("big_tank", t("2026-11-11T21:00"), "dose", "dose", {"volume_ml": 3})
    s.update_event(e["id"], payload={"volume_ml": 4})
    assert s.list_events()[0]["payload"]["volume_ml"] == 4
    s.delete_event(e["id"])
    assert s.list_events() == []
    assert [a["action"] for a in s.audit_for(e["id"])] == ["create", "update", "delete"]
    s.set_state("k", {"a": 1})
    assert s.get_state()["k"] == {"a": 1}


def test_rotation():
    from aqt.logic import rotation_due
    task = CFG.tanks["nano_tank"].tasks["filter_maintenance"]
    s, rot = task.schedule, task.rotate
    assert rot == ["filter_clean", "prefilter_clean"]
    anchor = t("2026-11-11T10:00")
    # no history -> Sat 14 Nov, main filter first
    assert rotation_due(s, t("2026-11-11T10:00"), TZ, [], rot, anchor, 24) == (t("2026-11-14T14:00"), "filter_clean")
    # last week's prefilter logged retroactively -> still main filter
    hist = [(t("2026-11-07T14:00"), "prefilter_clean")]
    assert rotation_due(s, t("2026-11-11T10:00"), TZ, hist, rot, anchor, 24)[1] == "filter_clean"
    # Saturday 15:00, not done -> overdue, main filter
    assert rotation_due(s, t("2026-11-14T15:00"), TZ, hist, rot, anchor, 24) == (t("2026-11-14T14:00"), "filter_clean")
    # logging the wrong action does not complete it
    wrong = hist + [(t("2026-11-14T15:00"), "prefilter_clean")]
    assert rotation_due(s, t("2026-11-14T16:00"), TZ, wrong, rot, anchor, 24) == (t("2026-11-14T14:00"), "filter_clean")
    # main filter done -> next Saturday asks prefilter
    done = hist + [(t("2026-11-14T15:00"), "filter_clean")]
    assert rotation_due(s, t("2026-11-14T16:00"), TZ, done, rot, anchor, 24) == (t("2026-11-21T14:00"), "prefilter_clean")
    # skip the 21st entirely -> on the 28th it still asks prefilter (order kept)
    assert rotation_due(s, t("2026-11-28T15:00"), TZ, done, rot, anchor, 24) == (t("2026-11-28T14:00"), "prefilter_clean")
    # unrelated actions (glass_clean) never affect rotation
    extra = done + [(t("2026-11-20T10:00"), "glass_clean")]
    assert rotation_due(s, t("2026-11-21T15:00"), TZ, extra, rot, anchor, 24)[1] == "prefilter_clean"


def test_interval_months_and_start():
    from aqt.logic import add_months
    from datetime import date
    task = CFG.tanks["nano_tank"].tasks["root_tabs"]
    s = task.schedule
    assert s.months == 3 and s.start == date(2026, 11, 29) and task.products == ["power_caps"]
    anchor = t("2026-11-15T10:00")
    # nothing logged -> start date at 14:00
    assert compute_due(s, t("2026-11-15T10:00"), TZ, [], anchor, 6) == t("2026-11-29T14:00")
    # logged on 29 Nov -> 28 Feb (clamped), then 28 May
    assert compute_due(s, t("2026-11-29T15:00"), TZ, [t("2026-11-29T14:10")], anchor, 6) == t("2027-02-28T14:00")
    assert add_months(date(2027, 2, 28), 3) == date(2027, 5, 28)
    assert add_months(date(2026, 12, 31), 2) == date(2027, 2, 28)
    # logged late (5 Dec) -> next counts from the actual date
    assert compute_due(s, t("2026-12-05T15:00"), TZ, [t("2026-12-05T12:00")], anchor, 6) == t("2027-03-05T14:00")


def test_schedule_validation():
    raw = yaml.safe_load(open(ROOT / "aquarium_tracker" / "tanks.yaml"))
    raw["tanks"]["nano_tank"]["tasks"]["fertilize"]["schedule"]["months"] = 2  # daily + months
    with pytest.raises(ConfigError):
        parse_config(raw)
    raw = yaml.safe_load(open(ROOT / "aquarium_tracker" / "tanks.yaml"))
    raw["tanks"]["nano_tank"]["tasks"]["root_tabs"]["products"] = ["nope"]
    with pytest.raises(ConfigError):
        parse_config(raw)


# ---------------------------------------------------------------- nitrogen programme
def _prog_events(*items):
    out = []
    for kind, ts, v in items:
        if kind == "m":
            out.append({"type": "measurement", "ts": t(ts), "payload": {"values": {"NO3": v}}})
        else:
            out.append({"type": "fertilize", "ts": t(ts), "payload": {"doses": {"nitrogen": v}}})
    return out


def test_program_flow():
    from aqt.logic import evaluate_program
    pc = CFG.tanks["nano_tank"].programs["nitrogen"]
    start = t("2026-11-16T18:00")
    ev = lambda *i: evaluate_program(_prog_events(*i), pc, 12, start, NOW[0], TZ)
    NOW = [t("2026-11-16T10:00")]
    assert evaluate_program([], pc, 12, None, NOW[0], TZ).phase == "idle"
    st = ev()
    assert st.phase == "baseline" and st.tasks[0].due == start
    assert abs(st.k_label - 2.525) < 0.01
    NOW[0] = t("2026-11-16T18:10")
    st = ev(("m", "2026-11-16T18:05", 5))
    assert st.phase == "test_dose" and st.tasks[0].drops == 20
    st = ev(("m", "2026-11-16T18:05", 5), ("d", "2026-11-16T18:07", 20))
    assert st.phase == "response" and st.tasks[0].due == t("2026-11-17T18:07")
    # rise below kit resolution -> label k; required = (12-5.5)/2.525 = 2.57 mL = 51 drops
    NOW[0] = t("2026-11-17T18:30")
    base = [("m", "2026-11-16T18:05", 5), ("d", "2026-11-16T18:07", 20), ("m", "2026-11-17T18:20", 5.5)]
    st = ev(*base)
    assert st.phase == "ramp" and st.k_source == "label" and st.k_reason == "below_resolution"
    assert st.tasks[0].drops == 10  # capped at 0.5 mL/day
    assert st.tasks[0].due == t("2026-11-18T18:00")
    # measured k within tolerance is used: rise 2 mg/L per 1 mL -> k=2 (label 2.525, -21%)
    st2 = ev(("m", "2026-11-16T18:05", 5), ("d", "2026-11-16T18:07", 20), ("m", "2026-11-17T18:20", 7))
    assert st2.k_source == "measured" and st2.k == 2.0
    # measured k far off -> label
    st3 = ev(("m", "2026-11-16T18:05", 5), ("d", "2026-11-16T18:07", 20), ("m", "2026-11-17T18:20", 15))
    assert st3.k_source == "label" and st3.k_reason == "deviates"
    # ramp: 51 drops at 10/day -> 6 days; after 5 doses, last one is 1 drop
    ramp = [("d", f"2026-11-{18+i}T18:05", 10) for i in range(5)]
    NOW[0] = t("2026-11-22T19:00")
    st = ev(*base, *ramp)
    assert st.phase == "ramp" and st.tasks[0].drops == 1, (st.phase, st.tasks)
    # finishing the ramp -> maintenance, first dose next day, 1.4 drops/day -> 1,1,2,1,2...
    NOW[0] = t("2026-11-23T19:00")
    st = ev(*base, *ramp, ("d", "2026-11-23T18:05", 1))
    assert st.phase == "maintenance"
    assert st.tasks[0].kind == "dose" and st.tasks[0].due == t("2026-11-24T18:00")
    assert st.tasks[1].kind == "measure" and st.tasks[1].due == t("2026-11-28T13:00")
    seq = []
    log = list(base) + ramp + [("d", "2026-11-23T18:05", 1)]
    for day in range(24, 31):
        NOW[0] = t(f"2026-11-{day}T17:00")
        s_ = ev(*log)
        dose = s_.tasks[0]
        seq.append(dose.drops)
        log.append(("d", f"2026-11-{day}T18:05", dose.drops))
    assert sum(seq) == 10 and set(seq) <= {1, 2}, seq  # 1.4/day * 7 = 9.8 -> 10


def test_program_bands_and_stable():
    from aqt.logic import evaluate_program
    pc = CFG.tanks["nano_tank"].programs["nitrogen"]
    start = t("2026-11-16T18:00")
    base = [("m", "2026-11-16T18:05", 12), ("d", "2026-11-16T18:07", 20), ("m", "2026-11-17T18:20", 12.5)]
    # already at target -> straight to maintenance
    st = evaluate_program(_prog_events(*base), pc, 12, start, t("2026-11-17T19:00"), TZ)
    assert st.phase == "maintenance" and st.required_ml == 0
    # NO3 22 -> halve; then 3 in-band readings -> stable, weekly measure gone
    reads = [("m", "2026-11-21T13:00", 22), ("m", "2026-11-28T13:00", 12),
             ("m", "2026-12-05T13:00", 11), ("m", "2026-12-12T13:00", 14)]
    st = evaluate_program(_prog_events(*base, *reads[:1]), pc, 12, start, t("2026-11-21T14:00"), TZ)
    assert abs(st.rate_ml_per_day - 0.035) < 1e-9 and st.adjustments[0]["factor"] == 0.5
    st = evaluate_program(_prog_events(*base, *reads), pc, 12, start, t("2026-12-12T14:00"), TZ)
    assert st.phase == "stable" and st.in_band_count == 3
    assert [x.kind for x in st.tasks] == ["dose"]
    # out of band later (monthly test 4) -> x2, back to weekly
    st = evaluate_program(_prog_events(*base, *reads, ("m", "2027-01-03T11:00", 4)), pc, 12, start,
                          t("2027-01-03T12:00"), TZ)
    assert st.phase == "maintenance" and st.in_band_count == 0 and st.adjustments[-1]["factor"] == 2.0
    assert [x.kind for x in st.tasks] == ["dose", "measure"]
    # 15-20 band -> x0.75, edges: 15 keeps, 20 -> 0.75, 10 keeps, 5 -> 1.5
    for v, f in [(15, 1.0), (20, 0.75), (17, 0.75), (10, 1.0), (5, 1.5), (4.9, 2.0), (20.1, 0.5)]:
        assert next(b for b in pc.bands if b.matches(v)).factor == f, v
