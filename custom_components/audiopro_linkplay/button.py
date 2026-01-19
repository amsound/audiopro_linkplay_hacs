"""Button entities for Audio Pro (LinkPlay) devices.

Currently includes a reboot button using the LinkPlay/WiiM HTTP API.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE_ID, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, LOGGER

LINKPLAY_HTTPAPI_PATH = "/httpapi.asp"
LINKPLAY_HTTP_TIMEOUT = 5


def _host_from_location(location: str) -> str | None:
    """Best-effort host extraction for LinkPlay HTTP API."""
    try:
        return urlparse(location).hostname
    except Exception:  # noqa: BLE001
        return None


async def _async_send_httpapi_command(
    hass: HomeAssistant, location: str, command: str
) -> None:
    """Send a LinkPlay httpapi.asp command (best-effort)."""
    host = _host_from_location(location)
    if not host:
        LOGGER.debug("No host for LinkPlay HTTP API (location=%s)", location)
        return

    cmd_q = quote(command, safe=":")  # keep colons readable
    url = f"https://{host}{LINKPLAY_HTTPAPI_PATH}?command={cmd_q}"

    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(LINKPLAY_HTTP_TIMEOUT):
            async with session.get(url, ssl=False) as resp:
                await resp.read()
    except Exception as err:  # noqa: BLE001
        LOGGER.debug("LinkPlay HTTP API call failed (%s): %s", url, err)


@dataclass(frozen=True, slots=True)
class LinkPlayButtonDescription(ButtonEntityDescription):
    """Describes a LinkPlay button entity."""


DESCRIPTIONS: tuple[LinkPlayButtonDescription, ...] = (
    LinkPlayButtonDescription(
        key="reboot",
        name="Reboot",
        icon="mdi:cached",
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up button entities for a config entry."""
    udn = entry.data.get(CONF_DEVICE_ID) or entry.unique_id
    if not udn:
        LOGGER.debug("No UDN/unique_id in config entry %s; skipping buttons", entry.entry_id)
        return

    location = entry.data.get(CONF_URL)
    if not location:
        LOGGER.debug("No URL in config entry %s; skipping buttons", entry.entry_id)
        return

    entities = [LinkPlayRebootButton(hass, entry, udn, location, DESCRIPTIONS[0])]
    async_add_entities(entities)


class LinkPlayRebootButton(ButtonEntity):
    """A reboot button for LinkPlay devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        udn: str,
        location: str,
        description: LinkPlayButtonDescription,
    ) -> None:
        self.hass = hass
        self._entry_id = entry.entry_id
        self._udn = udn
        self._location = location
        self.entity_description = description

        self._attr_name = f"{entry.title} {description.name}"
        self._attr_unique_id = f"{udn}_{description.key}"
        self._attr_device_info = dr.DeviceInfo(
            connections={(dr.CONNECTION_UPNP, udn)}
        )

    async def async_press(self) -> None:
        # Command discovered via Wireshark:
        # https://<device>/httpapi.asp?command=StartRebootTime:0
        await _async_send_httpapi_command(self.hass, self._location, "StartRebootTime:0")

        # Best-effort: if the media_player entity is present, start a short
        # recovery window to reconnect/resubscribe after the reboot.
        entity_map = self.hass.data.get(f"{DOMAIN}_entities", {})
        mp = entity_map.get(self._entry_id)
        if mp is not None and hasattr(mp, "async_start_reboot_recovery"):
            try:
                mp.async_start_reboot_recovery()
            except Exception:  # noqa: BLE001
                pass
