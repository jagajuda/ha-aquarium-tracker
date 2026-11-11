"""Websocket API used by the sidebar panel."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_UPDATED


def _tracker(hass: HomeAssistant, connection, msg) -> Any:
    tracker = hass.data.get(DOMAIN)
    if tracker is None:
        connection.send_error(msg["id"], "not_loaded", "Aquarium Tracker yüklü değil (config hatası olabilir, log'a bak)")
    return tracker


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    ts = dt_util.parse_datetime(value)
    if ts is None:
        raise ValueError(f"Geçersiz zaman: {value}")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt_util.get_default_time_zone())
    return ts


@websocket_api.websocket_command({vol.Required("type"): "aquarium_tracker/snapshot"})
@callback
def ws_snapshot(hass: HomeAssistant, connection, msg) -> None:
    if (tr := _tracker(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], tr.snapshot())


@websocket_api.websocket_command({vol.Required("type"): "aquarium_tracker/events"})
@callback
def ws_events(hass: HomeAssistant, connection, msg) -> None:
    if (tr := _tracker(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], tr.events)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "aquarium_tracker/add_event",
        vol.Required("tank"): str,
        vol.Required("event_type"): str,
        vol.Optional("payload", default={}): dict,
        vol.Optional("ts"): vol.Any(str, None),
        vol.Optional("note", default=""): str,
    }
)
@websocket_api.async_response
async def ws_add_event(hass: HomeAssistant, connection, msg) -> None:
    if (tr := _tracker(hass, connection, msg)) is None:
        return
    try:
        ev = await tr.async_add_event(
            msg["tank"], msg["event_type"], msg["payload"], ts=_parse_ts(msg.get("ts")), note=msg["note"]
        )
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], ev)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "aquarium_tracker/update_event",
        vol.Required("event_id"): int,
        vol.Optional("payload"): vol.Any(dict, None),
        vol.Optional("ts"): vol.Any(str, None),
        vol.Optional("note"): vol.Any(str, None),
    }
)
@websocket_api.async_response
async def ws_update_event(hass: HomeAssistant, connection, msg) -> None:
    if (tr := _tracker(hass, connection, msg)) is None:
        return
    try:
        ev = await tr.async_update_event(
            msg["event_id"], ts=_parse_ts(msg.get("ts")), payload=msg.get("payload"), note=msg.get("note")
        )
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], ev)


@websocket_api.websocket_command(
    {vol.Required("type"): "aquarium_tracker/delete_event", vol.Required("event_id"): int}
)
@websocket_api.async_response
async def ws_delete_event(hass: HomeAssistant, connection, msg) -> None:
    if (tr := _tracker(hass, connection, msg)) is None:
        return
    try:
        await tr.async_delete_event(msg["event_id"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {vol.Required("type"): "aquarium_tracker/audit", vol.Required("event_id"): int}
)
@websocket_api.async_response
async def ws_audit(hass: HomeAssistant, connection, msg) -> None:
    if (tr := _tracker(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], await tr.async_audit(msg["event_id"]))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "aquarium_tracker/set_mode",
        vol.Required("tank"): str,
        vol.Required("mode"): str,
    }
)
@websocket_api.async_response
async def ws_set_mode(hass: HomeAssistant, connection, msg) -> None:
    if (tr := _tracker(hass, connection, msg)) is None:
        return
    try:
        await tr.async_set_mode(msg["tank"], msg["mode"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "aquarium_tracker/snooze",
        vol.Required("tank"): str,
        vol.Required("task"): str,
        vol.Optional("minutes"): vol.Any(int, None),
    }
)
@websocket_api.async_response
async def ws_snooze(hass: HomeAssistant, connection, msg) -> None:
    if (tr := _tracker(hass, connection, msg)) is None:
        return
    try:
        await tr.async_snooze(msg["tank"], msg["task"], msg.get("minutes"))
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({vol.Required("type"): "aquarium_tracker/reload"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_reload(hass: HomeAssistant, connection, msg) -> None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_reload(entry.entry_id)
    connection.send_result(msg["id"], {"loaded": DOMAIN in hass.data})


@websocket_api.websocket_command({vol.Required("type"): "aquarium_tracker/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection, msg) -> None:
    @callback
    def forward() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"changed": True}))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_UPDATED, forward)
    connection.send_result(msg["id"])


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    for handler in (
        ws_snapshot,
        ws_events,
        ws_add_event,
        ws_update_event,
        ws_delete_event,
        ws_audit,
        ws_set_mode,
        ws_snooze,
        ws_reload,
        ws_subscribe,
    ):
        websocket_api.async_register_command(hass, handler)
