"""Audio Pro (LinkPlay) integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import LOGGER

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER, Platform.SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up audiopro_linkplay from a config entry."""
    LOGGER.debug("Setting up config entry: %s", entry.unique_id)

    async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Handle options update: reload to apply changes immediately."""
        LOGGER.debug("Options updated for %s; reloading entry", entry.unique_id)
        await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
