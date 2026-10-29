"""Config and options flow for Prometheus Hosts."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PrometheusAuthError, PrometheusClient, PrometheusError
from .const import (
    CONF_ALERTMANAGER_URL,
    CONF_SCAN_INTERVAL,
    CONF_STALE_HOURS,
    CONF_VERIFY_SSL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_HOURS,
    DOMAIN,
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Optional(CONF_USERNAME): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Optional(CONF_ALERTMANAGER_URL): str,
        vol.Optional(CONF_VERIFY_SSL, default=True): bool,
    }
)


async def _validate(hass, data: dict[str, Any]) -> str | None:
    session = async_get_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, True))
    client = PrometheusClient(session, data[CONF_URL], data.get(CONF_USERNAME), data.get(CONF_PASSWORD))
    try:
        await client.query("up")
    except PrometheusAuthError:
        return "invalid_auth"
    except PrometheusError:
        return "cannot_connect"
    if data.get(CONF_ALERTMANAGER_URL):
        try:
            await client.alertmanager_alerts(data[CONF_ALERTMANAGER_URL])
        except PrometheusError:
            return "cannot_connect_alertmanager"
    return None


class PrometheusHostsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_URL] = user_input[CONF_URL].rstrip("/")
            if user_input.get(CONF_ALERTMANAGER_URL):
                user_input[CONF_ALERTMANAGER_URL] = user_input[CONF_ALERTMANAGER_URL].rstrip("/")
            await self.async_set_unique_id(user_input[CONF_URL])
            self._abort_if_unique_id_configured()
            if (error := await _validate(self.hass, user_input)) is None:
                return self.async_create_entry(title=user_input[CONF_URL], data=user_input)
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            if (error := await _validate(self.hass, data)) is None:
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
                    vol.Optional(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return PrometheusHostsOptionsFlow()


class PrometheusHostsOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                    ): vol.All(int, vol.Range(min=30, max=900)),
                    vol.Required(
                        CONF_STALE_HOURS, default=opts.get(CONF_STALE_HOURS, DEFAULT_STALE_HOURS)
                    ): vol.All(int, vol.Range(min=1, max=720)),
                }
            ),
        )
