from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

DOMAIN = "aquarium_tracker"
TZ = ZoneInfo("Europe/Istanbul")


@pytest.fixture
async def setup(hass: HomeAssistant, hass_ws_client, freezer):
    import shutil; shutil.rmtree(hass.config.path("aquarium_tracker"), ignore_errors=True)
    await hass.config.async_set_time_zone("Europe/Istanbul")
    freezer.move_to(datetime(2026, 11, 11, 10, 0, tzinfo=TZ))
    calls = []
    async def notify(call):
        calls.append(dict(call.data))
    hass.services.async_register("notify", "mobile_app_se3gen", notify)
    await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, data={"notify_service": "notify.mobile_app_se3gen"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ws = await hass_ws_client(hass)
    return entry, calls, ws


async def ws_call(ws, **msg):
    await ws.send_json({"id": ws_call.n, **msg}); ws_call.n += 1
    r = await ws.receive_json()
    return r
ws_call.n = 1


async def test_flow(hass, setup, freezer, hass_ws_client, hass_admin_user):
    entry, calls, ws = setup
    r = await ws_call(ws, type="aquarium_tracker/snapshot")
    assert r["success"], r
    big = r["result"]["tanks"][0]
    assert big["mode"] == "cycling" and big["cycle"]["phase"] == "start"
    assert [t["status"] for t in big["tasks"]] == ["upcoming"]
    assert hass.states.get("sensor.big_tank_status").state == "cycling_start"

    # dose + measurement via ws
    r = await ws_call(ws, type="aquarium_tracker/add_event", tank="big_tank", event_type="dose",
                      payload={"volume_ml": 6})
    assert r["success"], r
    assert abs(r["result"]["payload"]["nh4_mg_l"] - 1.927) < 0.01
    r = await ws_call(ws, type="aquarium_tracker/add_event", tank="big_tank", event_type="measurement",
                      payload={"values": {"NH4": 2, "pH": 7.5, "temp": ""}})
    assert r["success"], r
    assert "NH3_free" in r["result"]["payload"]["derived"]
    await hass.async_block_till_done()
    assert hass.states.get("sensor.big_tank_nh4").state == "2.0"
    assert hass.states.get("sensor.big_tank_status").state == "cycling_ammonia"

    # invalid
    r = await ws_call(ws, type="aquarium_tracker/add_event", tank="big_tank", event_type="measurement",
                      payload={"values": {}})
    assert not r["success"]

    # next due = 13 Nov 21:00 ; jump there -> notification
    freezer.move_to(datetime(2026, 11, 13, 21, 0, 10, tzinfo=TZ))
    async_fire_time_changed(hass); await hass.async_block_till_done()
    sent = [c for c in calls if c.get("message") != "clear_notification" and c["data"]["tag"] == "aqt_big_tank_cycle_test"]
    assert len(sent) == 1, calls
    assert "NH4" in sent[-1]["message"]
    print("NOTIFS", [c.get("title") + " | " + c["message"] for c in calls])
    assert sent[-1]["data"]["url"] == "/aquarium-tracker?tank=big_tank&task=cycle_test"
    # nano fertilize 18:00 on 11th, water change 14th -> also fired by now? fertilize yes (overdue)
    n_before = len(calls)
    # tick again within 60 min -> no repeat
    freezer.tick(timedelta(minutes=5)); async_fire_time_changed(hass); await hass.async_block_till_done()
    assert len(calls) == n_before
    # snooze via notification action
    hass.bus.async_fire("mobile_app_notification_action", {"action": "AQT_SNOOZE|big_tank|cycle_test"})
    await hass.async_block_till_done()
    rt = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/")
    ws = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(rt))
    r = await ws_call(ws, type="aquarium_tracker/snapshot")
    assert r["result"]["tanks"][0]["tasks"][0]["status"] == "snoozed"
    # log measurement -> clears notification and moves due
    r = await ws_call(ws, type="aquarium_tracker/add_event", tank="big_tank", event_type="measurement",
                      payload={"values": {"NH4": 0}})
    await hass.async_block_till_done()
    assert any(c.get("message") == "clear_notification" and c["data"]["tag"] == "aqt_big_tank_cycle_test" for c in calls)
    r = await ws_call(ws, type="aquarium_tracker/snapshot")
    t = r["result"]["tanks"][0]["tasks"][0]
    assert t["status"] == "upcoming" and t["params"] == ["NH4", "NO2"]

    # update + delete + audit
    eid = r["result"]["tanks"][0]["latest"]["NH4"]["event_id"]
    r = await ws_call(ws, type="aquarium_tracker/update_event", event_id=eid, payload={"values": {"NH4": 0.5}})
    assert r["success"], r
    r = await ws_call(ws, type="aquarium_tracker/audit", event_id=eid)
    assert [a["action"] for a in r["result"]] == ["create", "update"]
    r = await ws_call(ws, type="aquarium_tracker/delete_event", event_id=eid)
    assert r["success"]

    # mode switch
    r = await ws_call(ws, type="aquarium_tracker/set_mode", tank="big_tank", mode="maintenance")
    assert r["success"]
    assert hass.states.get("sensor.big_tank_status").state == "maintenance"
    r = await ws_call(ws, type="aquarium_tracker/events")
    assert any(e["type"] == "system" for e in r["result"])

    # nano fertilize
    r = await ws_call(ws, type="aquarium_tracker/add_event", tank="nano_tank", event_type="fertilize",
                      payload={"doses": {"trace": 3, "potash": 3}})
    assert r["success"], r

    # service
    await hass.services.async_call(DOMAIN, "log_event", {"tank": "nano_tank", "type": "note", "note": "hello"}, blocking=True)

    # reload keeps data
    r = await ws_call(ws, type="aquarium_tracker/reload")
    assert r["success"] and r["result"]["loaded"]
    r = await ws_call(ws, type="aquarium_tracker/events")
    assert len(r["result"]) >= 5

    assert await hass.config_entries.async_unload(entry.entry_id)

