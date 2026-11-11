"""Config flow: pick the phone that receives reminders."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import CONF_NOTIFY_SERVICE, DOMAIN


def _notify_options(hass) -> list[str]:
    services = hass.services.async_services().get("notify", {})
    return sorted(f"notify.{name}" for name in services if name.startswith("mobile_app_"))


def _schema(hass, default: str | None) -> vol.Schema:
    options = _notify_options(hass)
    key = vol.Required(CONF_NOTIFY_SERVICE, default=default) if default else vol.Required(CONF_NOTIFY_SERVICE)
    return vol.Schema(
        {
            key: SelectSelector(
                SelectSelectorConfig(options=options, custom_value=True, mode=SelectSelectorMode.DROPDOWN)
            )
        }
    )


class AquariumTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="Aquarium Tracker", data=user_input)
        options = _notify_options(self.hass)
        return self.async_show_form(step_id="user", data_schema=_schema(self.hass, options[0] if options else None))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AquariumTrackerOptionsFlow()


class AquariumTrackerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, **user_input}
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(self.hass, self.config_entry.data.get(CONF_NOTIFY_SERVICE)),
        )
