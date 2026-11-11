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
