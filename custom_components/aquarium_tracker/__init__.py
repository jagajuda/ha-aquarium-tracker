"""Aquarium Tracker: timeline, reminders and cycle tracking for aquariums."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

import voluptuous as vol
import yaml

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_NOTIFY_SERVICE,
    CONFIG_DIR,
    CONFIG_FILE,
    DB_FILE,
    DOMAIN,
    PANEL_COMPONENT,
    PANEL_URL_PATH,
    STATIC_URL,
    VERSION,
)
from .core import AquariumTracker
from .model import ConfigError, MODES, USER_EVENT_TYPES, parse_config
from .store import EventStore
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AquariumConfigEntry = ConfigEntry[AquariumTracker]


def _load_yaml(path: Path, default: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        shutil.copyfile(default, path)
        _LOGGER.info("Created default config at %s", path)
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_websocket(hass)
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AquariumConfigEntry) -> bool:
    cfg_dir = Path(hass.config.path(CONFIG_DIR))
    try:
        raw = await hass.async_add_executor_job(
            _load_yaml, cfg_dir / CONFIG_FILE, Path(__file__).parent / "default_tanks.yaml"
        )
        config = parse_config(raw)
    except (ConfigError, yaml.YAMLError) as err:
        raise ConfigEntryError(f"tanks.yaml: {err}") from err

    store = EventStore(str(cfg_dir / DB_FILE))
    await hass.async_add_executor_job(store.open)

    tracker = AquariumTracker(hass, config, store, entry.data[CONF_NOTIFY_SERVICE])
    await tracker.async_start()
    entry.runtime_data = tracker
    hass.data[DOMAIN] = tracker

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _register_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AquariumConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        tracker: AquariumTracker = entry.runtime_data
        await tracker.async_stop()
        await hass.async_add_executor_job(tracker.store.close)
        hass.data.pop(DOMAIN, None)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: AquariumConfigEntry) -> None:
    # Panel is kept across reloads (so an open panel survives "reload"); drop it on removal.
    frontend.async_remove_panel(hass, PANEL_URL_PATH)


async def _register_panel(hass: HomeAssistant) -> None:
    flag = f"{DOMAIN}_static"
    if not hass.data.get(flag):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), False)]
        )
        hass.data[flag] = True
    if PANEL_URL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title="Akvaryum",
        sidebar_icon="mdi:fishbowl-outline",
        module_url=f"{STATIC_URL}/aquarium-tracker-panel.js?v={VERSION}",
        require_admin=False,
        config={},
    )


def _tracker(hass: HomeAssistant) -> AquariumTracker:
    tracker = hass.data.get(DOMAIN)
    if tracker is None:
        raise HomeAssistantError("Aquarium Tracker is not loaded")
    return tracker


def _register_services(hass: HomeAssistant) -> None:
    async def reload(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_reload(entry.entry_id)

    async def log_event(call: ServiceCall) -> None:
        ts: datetime | None = call.data.get("timestamp")
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt_util.get_default_time_zone())
        try:
            await _tracker(hass).async_add_event(
                call.data["tank"],
                call.data["type"],
                call.data.get("payload") or {},
                ts=ts,
                note=call.data.get("note", ""),
                source="service",
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def set_mode(call: ServiceCall) -> None:
        try:
            await _tracker(hass).async_set_mode(call.data["tank"], call.data["mode"])
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def snooze(call: ServiceCall) -> None:
        try:
            await _tracker(hass).async_snooze(call.data["tank"], call.data["task"], call.data.get("minutes"))
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    hass.services.async_register(DOMAIN, "reload", reload)
    hass.services.async_register(
        DOMAIN,
        "log_event",
        log_event,
        schema=vol.Schema(
            {
                vol.Required("tank"): cv.string,
                vol.Required("type"): vol.In(USER_EVENT_TYPES),
                vol.Optional("payload"): dict,
                vol.Optional("timestamp"): cv.datetime,
                vol.Optional("note"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "set_mode",
        set_mode,
        schema=vol.Schema({vol.Required("tank"): cv.string, vol.Required("mode"): vol.In(MODES)}),
    )
    hass.services.async_register(
        DOMAIN,
        "snooze",
        snooze,
        schema=vol.Schema(
            {
                vol.Required("tank"): cv.string,
                vol.Required("task"): cv.string,
                vol.Optional("minutes"): vol.All(int, vol.Range(min=1)),
            }
        ),
    )
