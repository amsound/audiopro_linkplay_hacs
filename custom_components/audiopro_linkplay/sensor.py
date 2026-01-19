"""Diagnostic sensors for Audio Pro (LinkPlay) devices.

These sensors mirror selected fields from the integration's media_player entity
so they can be shown more easily on a dashboard.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN, LOGGER


def _safe_str(val: Any) -> str:
    """Return a stripped string or empty string."""
    if val is None:
        return ""
    return str(val).strip()


def _humanize_track_source(val: Any) -> str:
    """Pretty-format TrackSource values for display."""
    raw = _safe_str(val)
    if not raw or raw in ("-",):
        return "-"

    upper = raw.upper()
    if upper in ("NONE", "UNKNOWN"):
        return "-"

    # Common known values (captured from WiiM/LinkPlay traffic)
    known = {
        "newTuneIn": "TuneIn",
        "UPnPServer": "UPnP Server",
        "CustomRadio": "Custom Radio",
        "Linkplay Radio": "Linkplay Radio",
        "vTuner": "vTuner",
        "Radio Paradise2": "Radio Paradise",
    }
    if raw in known:
        return known[raw]

    # Replace separators
    s = raw.replace("_", " ").replace("-", " ").strip()

    # Insert spaces:
    # 1) between lower/digit and upper ("CustomRadio" -> "Custom Radio")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    # 2) between acronym and word ("UPnPServer" -> "UPnP Server")
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)

    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s or "-"


def _coerce_int(val: Any) -> int | None:
    """Best-effort conversion to int."""
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = _safe_str(val)
    if not s or s in ("-",):
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _format_audio_quality(attrs: dict[str, Any]) -> str:
    """Return a human-friendly audio quality string or '-' if unknown."""
    rate_hz = _coerce_int(attrs.get("linkplay_audio_rate_hz"))
    bits = _coerce_int(attrs.get("linkplay_audio_format_bits"))
    bitrate = _coerce_int(attrs.get("linkplay_audio_bitrate_kbps"))

    parts: list[str] = []
    if rate_hz:
        khz = rate_hz / 1000.0
        # Keep 1 decimal for common values like 44.1 kHz.
        if abs(khz - round(khz)) < 1e-6:
            parts.append(f"{int(round(khz))} kHz")
        else:
            parts.append(f"{khz:.1f} kHz")
    if bits:
        parts.append(f"{bits}-bit")
    if bitrate:
        parts.append(f"{bitrate} kbps")

    return " / ".join(parts) if parts else "-"


@dataclass(frozen=True, slots=True, kw_only=True)
class DiagSensorDescription(SensorEntityDescription):
    """Description for a diagnostic sensor entity."""

    name_suffix: str
    value_fn: Callable[[dict[str, Any]], str]


DESCRIPTIONS: tuple[DiagSensorDescription, ...] = (
    DiagSensorDescription(
        key="diag_source",
        name_suffix="Source",
        icon="mdi:import",
        value_fn=lambda attrs: _safe_str(attrs.get("source")) or "-",
    ),
    DiagSensorDescription(
        key="diag_source_detail",
        name_suffix="Source Detail",
        icon="mdi:import",
        value_fn=lambda attrs: _humanize_track_source(attrs.get("linkplay_track_source")),
    ),
    DiagSensorDescription(
        key="diag_audio_quality",
        icon="mdi:sine-wave",
        name_suffix="Audio Quality",
        value_fn=_format_audio_quality,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up diagnostic sensors for a config entry."""
    udn = entry.data.get(CONF_DEVICE_ID) or entry.unique_id
    if not udn:
        LOGGER.debug('No UDN/unique_id in config entry %s; skipping diagnostic sensors', entry.entry_id)
        return

    LOGGER.debug('Setting up diagnostic sensors for %s (%s)', entry.title, udn)

    entities: list[SensorEntity] = [
        LinkPlayDiagnosticSensor(hass, entry, udn, desc) for desc in DESCRIPTIONS
    ]
    async_add_entities(entities)


class LinkPlayDiagnosticSensor(SensorEntity):
    """A read-only diagnostic sensor mirroring the media_player entity."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        udn: str,
        description: DiagSensorDescription,
    ) -> None:
        self.hass = hass
        self._udn = udn
        self._entry_id = entry.entry_id
        self.entity_description = description

        self._attr_name = f"{entry.title} {description.name_suffix}"
        self._attr_unique_id = f"{udn}_{description.key}"
        self._attr_device_info = dr.DeviceInfo(connections={(dr.CONNECTION_UPNP, udn)})

        self._source_entity_id: str | None = None
        self._unsub_state: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Resolve and subscribe lazily; platform setups can run concurrently.
        self.hass.async_create_task(self._async_resolve_and_subscribe())

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

    def _resolve_media_player_entity_id(self) -> str | None:
        """Resolve the media_player entity_id for this config entry.

        Prefer config-entry linkage (robust across unique_id changes), fall back to
        unique_id lookup by UDN.
        """
        ent_reg = er.async_get(self.hass)

        # Prefer linking by config entry
        try:
            for e in er.async_entries_for_config_entry(ent_reg, self._entry_id):
                if e.domain == MEDIA_PLAYER_DOMAIN and getattr(e, 'platform', None) == DOMAIN:
                    return e.entity_id
        except Exception:  # noqa: BLE001
            pass

        return ent_reg.async_get_entity_id(MEDIA_PLAYER_DOMAIN, DOMAIN, self._udn)

    async def _async_resolve_and_subscribe(self) -> None:
        # Retry briefly in case the media_player entity isn't registered yet.
        for _ in range(10):
            entity_id = self._resolve_media_player_entity_id()
            if entity_id:
                self._set_source_entity_id(entity_id)
                self.async_write_ha_state()
                return
            await asyncio.sleep(1)

        LOGGER.debug(
            "Could not resolve media_player entity_id for %s (%s)",
            self._udn,
            self.entity_description.key,
        )

    def _set_source_entity_id(self, entity_id: str) -> None:
        if entity_id == self._source_entity_id:
            return

        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

        self._source_entity_id = entity_id

        @callback
        def _handle_state_change(event: Any) -> None:  # noqa: ANN401
            self.async_write_ha_state()

        self._unsub_state = async_track_state_change_event(
            self.hass, [entity_id], _handle_state_change
        )

    def _source_attrs(self) -> dict[str, Any]:
        if not self._source_entity_id:
            return {}
        st = self.hass.states.get(self._source_entity_id)
        if st is None:
            return {}
        # State.attributes is Mapping[str, Any]; a dict copy keeps it safe.
        return dict(st.attributes)

    @property
    def available(self) -> bool:
        if not self._source_entity_id:
            return False
        st = self.hass.states.get(self._source_entity_id)
        if st is None:
            return False
        return st.state != STATE_UNAVAILABLE

    @property
    def native_value(self) -> str:
        attrs = self._source_attrs()
        try:
            return self.entity_description.value_fn(attrs)
        except Exception:  # noqa: BLE001
            return "-"
