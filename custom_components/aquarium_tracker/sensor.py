"""Sensors: latest value per tank parameter, tank status, next task."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_UPDATED
from .core import AquariumTracker
from .model import Tank


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    tracker: AquariumTracker = entry.runtime_data
    entities: list[SensorEntity] = []
    for tank in tracker.config.tanks.values():
        for param in tank.parameters:
            entities.append(ParameterSensor(tracker, tank, param))
        entities.append(StatusSensor(tracker, tank))
        if tank.tasks:
            entities.append(NextTaskSensor(tracker, tank))
    async_add_entities(entities)


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, tracker: AquariumTracker, tank: Tank) -> None:
        self._tracker = tracker
        self._tank = tank
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tank.key)},
            name=tank.name,
            manufacturer="Aquarium Tracker",
            model=f"{tank.net_volume_l:g} L",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._refresh))
        self._update()

    @callback
    def _refresh(self) -> None:
        self._update()
        self.async_write_ha_state()

    def _update(self) -> None:
        raise NotImplementedError


class ParameterSensor(_Base):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, tracker: AquariumTracker, tank: Tank, param: str) -> None:
        super().__init__(tracker, tank)
        self._param = param
        self._attr_unique_id = f"{tank.key}_{param.lower()}"
        self._attr_name = param
        self._attr_suggested_object_id = f"{tank.key}_{param.lower()}"
        unit = tracker.config.parameters[param].unit
        self._attr_native_unit_of_measurement = unit or None
        if param == "temp":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE

    def _update(self) -> None:
        latest = self._tracker.latest_values(self._tank.key).get(self._param)
        self._attr_native_value = latest["value"] if latest else None
        attrs: dict[str, Any] = {}
        if latest:
            attrs["measured_at"] = latest["ts"]
            if "NH3_free" in latest:
                attrs["nh3_free_mg_l"] = latest["NH3_free"]
        self._attr_extra_state_attributes = attrs


class StatusSensor(_Base):
    _attr_icon = "mdi:fishbowl-outline"

    def __init__(self, tracker: AquariumTracker, tank: Tank) -> None:
        super().__init__(tracker, tank)
        self._attr_unique_id = f"{tank.key}_status"
        self._attr_name = "Status"
        self._attr_suggested_object_id = f"{tank.key}_status"

    def _update(self) -> None:
        mode = self._tracker.mode(self._tank.key)
        attrs: dict[str, Any] = {"mode": mode}
        if self._tracker.has_cycle(self._tank) and mode == "cycling":
            cs = self._tracker.cycle_status(self._tank)
            self._attr_native_value = f"cycling_{cs.phase}"
            attrs.update(cycle_phase=cs.phase, passes=cs.passes, required_passes=cs.required, next_params=cs.params)
        else:
            self._attr_native_value = mode
        self._attr_extra_state_attributes = attrs


class NextTaskSensor(_Base):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, tracker: AquariumTracker, tank: Tank) -> None:
        super().__init__(tracker, tank)
        self._attr_unique_id = f"{tank.key}_next_task"
        self._attr_name = "Next task"
        self._attr_suggested_object_id = f"{tank.key}_next_task"

    def _update(self) -> None:
        now = dt_util.utcnow()
        infos = [
            self._tracker.task_info(self._tank, t, now) for t in self._tank.tasks.values()
        ]
        infos = [i for i in infos if i["active"] and i["due"]]
        infos.sort(key=lambda i: i["due"])
        if not infos:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        first = infos[0]
        self._attr_native_value = dt_util.parse_datetime(first["due"])
        self._attr_extra_state_attributes = {
            "task": first["task"],
            "title": first["title"],
            "status": first["status"],
            "params": first["params"],
            "overdue": [i["task"] for i in infos if i["status"] == "due"],
        }
