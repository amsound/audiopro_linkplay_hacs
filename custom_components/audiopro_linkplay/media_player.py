"""Support for DLNA DMR (Device Media Renderer)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Sequence
import contextlib
import html
import re
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse
from xml.etree import ElementTree as ET

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
import functools
from typing import Any, Concatenate, ParamSpec, TypeVar

from async_upnp_client.client import UpnpService, UpnpStateVariable
from async_upnp_client.const import NotificationSubType
from async_upnp_client.exceptions import UpnpError, UpnpResponseError
from async_upnp_client.profiles.dlna import DmrDevice, PlayMode, TransportState
from async_upnp_client.utils import async_get_local_ip
from didl_lite import didl_lite

from homeassistant import config_entries
from homeassistant.components import media_source, ssdp
from homeassistant.components.media_player import (
    ATTR_MEDIA_EXTRA,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    async_process_play_media_url,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_MAC, CONF_TYPE, CONF_URL
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from .const import (
    CONF_BROWSE_UNFILTERED,
    CONF_CALLBACK_URL_OVERRIDE,
    CONF_LISTEN_HOST,
    CONF_LISTEN_PORT,
    CONF_POLL_AVAILABILITY,
    CONF_VOLUME_STEP_PCT,
    DOMAIN,
    DEFAULT_VOLUME_STEP_PCT,
    MAX_VOLUME_STEP_PCT,
    MIN_VOLUME_STEP_PCT,
    LOGGER as _LOGGER,
    MEDIA_METADATA_DIDL,
    MEDIA_TYPE_MAP,
    MEDIA_UPNP_CLASS_MAP,
    REPEAT_PLAY_MODES,
    SHUFFLE_PLAY_MODES,
    STREAMABLE_PROTOCOLS,
)

# LinkPlay/WiiM HTTP API additions
#
# We expose friendly, stable source names to Home Assistant, and map them to
# LinkPlay switchmode tokens.
#
# Notes:
# - "AirPlay" is intentionally a *visibility-only* pseudo-source: it may appear
#   as the current source but selecting it does not send any command.
# - "Multiroom (Secondary)" is also visibility-only: it indicates the device is
#   acting as a secondary speaker in a multiroom group.
LINKPLAY_SOURCE_UI_TO_TOKEN: dict[str, str | None] = {
    # Switchable inputs
    "Wi-Fi": "wifi",
    "Bluetooth": "bluetooth",
    "HDMI ARC": "HDMI",
    "Line-In": "line-in",
    "Optical": "optical",
    # Visibility-only states
    "AirPlay": None,
    "Multiroom (Secondary)": None,
}

LINKPLAY_SOURCE_LIST: list[str] = list(LINKPLAY_SOURCE_UI_TO_TOKEN.keys())
LINKPLAY_SOURCE_TOKEN_TO_UI: dict[str, str] = {
    token.lower(): ui for ui, token in LINKPLAY_SOURCE_UI_TO_TOKEN.items() if token
}

# AVTransport LastChange fields used for source detection.
#
# Prefer PlaybackStorageMedium and fall back to CurrentTrackURI/AVTransportURI.
LINKPLAY_PLAYBACK_STORAGE_MEDIUM_TO_SOURCE_UI: dict[str, str] = {
    # Switchable inputs
    "HDMI": "HDMI ARC",
    "OPTICAL": "Optical",
    "LINE-IN": "Line-In",
    "BLUETOOTH": "Bluetooth",
    "RADIO-NETWORK": "Wi-Fi",
    "SONGLIST-NETWORK": "Wi-Fi",

    # Visibility-only states
    "AIRPLAY": "AirPlay",
    "MULTIROOM-SLAVE": "Multiroom (Secondary)",
}

# Some firmwares mirror PlaybackStorageMedium into URI fields.
# Treat any token starting with SONGLIST- or ending in -NETWORK as Wi-Fi.
LINKPLAY_NETWORK_TOKEN_PREFIXES: tuple[str, ...] = ("SONGLIST-",)
LINKPLAY_PRESET_MODES: list[str] = [f"Preset {i}" for i in range(1, 7)]
LINKPLAY_HTTPAPI_PATH = "/httpapi.asp"
LINKPLAY_HTTP_TIMEOUT = 10
LINKPLAY_POSITION_POLL_INTERVAL = timedelta(seconds=5)

PLAYQUEUE_SERVICE_TYPE = "urn:schemas-wiimu-com:service:PlayQueue:1"

# LinkPlay/WiiM loop modes as observed in controller traffic.
# These encode shuffle + repeat in a single integer.
_LOOPMODE_TO_STATE: dict[int, tuple[bool, RepeatMode]] = {
    4: (False, RepeatMode.OFF),   # normal
    0: (False, RepeatMode.ALL),   # repeat all
    1: (False, RepeatMode.ONE),   # repeat one
    3: (True, RepeatMode.OFF),    # shuffle
    2: (True, RepeatMode.ALL),    # shuffle + repeat all
    5: (True, RepeatMode.ONE),    # shuffle + repeat one
}
_STATE_TO_LOOPMODE: dict[tuple[bool, RepeatMode], int] = {
    v: k for k, v in _LOOPMODE_TO_STATE.items()
}


from .data import EventListenAddr, get_domain_data

PARALLEL_UPDATES = 0

_TRANSPORT_STATE_TO_MEDIA_PLAYER_STATE = {
    TransportState.PLAYING: MediaPlayerState.PLAYING,
    TransportState.TRANSITIONING: MediaPlayerState.PLAYING,
    TransportState.PAUSED_PLAYBACK: MediaPlayerState.PAUSED,
    TransportState.PAUSED_RECORDING: MediaPlayerState.PAUSED,
    # Unable to map this state to anything reasonable, so it's "Unknown"
    TransportState.VENDOR_DEFINED: None,
    None: MediaPlayerState.ON,
}
_DlnaDmrEntityT = TypeVar("_DlnaDmrEntityT", bound="DlnaDmrEntity")
_P = ParamSpec("_P")
_R = TypeVar("_R")


def catch_request_errors(
    func: Callable[Concatenate[_DlnaDmrEntityT, _P], Awaitable[_R]],
) -> Callable[Concatenate[_DlnaDmrEntityT, _P], Coroutine[Any, Any, _R | None]]:
    """Catch UpnpError errors."""

    @functools.wraps(func)
    async def wrapper(
        self: _DlnaDmrEntityT, *args: _P.args, **kwargs: _P.kwargs
    ) -> _R | None:
        """Catch UpnpError errors and check availability before and after request."""
        if not self.available:
            _LOGGER.warning(
                "Device disappeared when trying to call service %s", func.__name__
            )
            return None
        try:
            return await func(self, *args, **kwargs)
        except UpnpError as err:
            self.check_available = True
            _LOGGER.error("Error during call %s: %r", func.__name__, err)
        return None

    return wrapper


async def async_setup_entry(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the DlnaDmrEntity from a config entry."""
    _LOGGER.debug("media_player.async_setup_entry %s (%s)", entry.entry_id, entry.title)

    udn = entry.data[CONF_DEVICE_ID]
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    if (
        (
            existing_entity_id := ent_reg.async_get_entity_id(
                domain=MEDIA_PLAYER_DOMAIN, platform=DOMAIN, unique_id=udn
            )
        )
        and (existing_entry := ent_reg.async_get(existing_entity_id))
        and (device_id := existing_entry.device_id)
        and (device_entry := dev_reg.async_get(device_id))
        and (dr.CONNECTION_UPNP, udn) not in device_entry.connections
    ):
        # If the existing device is missing the udn connection, add it
        # now to ensure that when the entity gets added it is linked to
        # the correct device.
        dev_reg.async_update_device(
            device_id,
            merge_connections={(dr.CONNECTION_UPNP, udn)},
        )

    # Create our own device-wrapping entity
    entity = DlnaDmrEntity(
        udn=udn,
        device_type=entry.data[CONF_TYPE],
        name=entry.title,
        event_host=entry.options.get(CONF_LISTEN_HOST),
        event_port=entry.options.get(CONF_LISTEN_PORT) or 0,
        event_callback_url=entry.options.get(CONF_CALLBACK_URL_OVERRIDE),
        poll_availability=entry.options.get(CONF_POLL_AVAILABILITY, False),
        location=entry.data[CONF_URL],
        mac_address=entry.data.get(CONF_MAC),
        browse_unfiltered=entry.options.get(CONF_BROWSE_UNFILTERED, False),
        config_entry=entry,
    )

    async_add_entities([entity])


class DlnaDmrEntity(MediaPlayerEntity):
    """Representation of a DLNA DMR device as a HA entity."""

    udn: str
    device_type: str

    _event_addr: EventListenAddr
    poll_availability: bool
    # Last known URL for the device, used when adding this entity to hass to try
    # to connect before SSDP has rediscovered it, or when SSDP discovery fails.
    location: str
    # Should the async_browse_media function *not* filter out incompatible media?
    browse_unfiltered: bool

    _device_lock: asyncio.Lock  # Held when connecting or disconnecting the device
    _device: DmrDevice | None = None
    check_available: bool = False
    _ssdp_connect_failed: bool = False

    # Track BOOTID in SSDP advertisements for device changes
    _bootid: int | None = None

    # We rely on UPnP eventing for state updates. We only poll position while
    # playing (see _start_position_polling) and optionally attempt reconnects
    # when poll_availability is enabled.
    _attr_should_poll = False

    # Name of the current sound mode, not supported by DLNA
    _attr_sound_mode = None

    def __init__(
        self,
        udn: str,
        device_type: str,
        name: str,
        event_host: str | None,
        event_port: int,
        event_callback_url: str | None,
        poll_availability: bool,
        location: str,
        mac_address: str | None,
        browse_unfiltered: bool,
        config_entry: config_entries.ConfigEntry,
    ) -> None:
        """Initialize DLNA DMR entity."""
        self.udn = udn
        self.device_type = device_type
        self._attr_name = name
        self._event_addr = EventListenAddr(event_host, event_port, event_callback_url)
        self.poll_availability = poll_availability
        self._attr_should_poll = bool(poll_availability)
        self.location = location
        self.mac_address = mac_address
        self.browse_unfiltered = browse_unfiltered
        self._device_lock = asyncio.Lock()

        # Store the event handler we use for GENA subscriptions so we can
        # subscribe to vendor services (e.g. PlayQueue) in addition to the
        # standard DLNA services.
        self._event_handler = None
        self._playqueue_subscription = None  # vendor service GENA subscription

        # Optional Linkplay position polling while playing.
        # Keep this attribute defined so event callbacks can't crash if polling
        # is stopped before it was ever started.
        self._unsub_position_poll: CALLBACK_TYPE | None = None

        # Cache mute state locally because some LinkPlay/WiiM/AudioPro DMRs
        # don't reliably report mute state via GENA events.
        self._cached_is_muted: bool | None = None

        # LinkPlay/WiiM source + metadata extracted from AVTransport LastChange.
        # We prefer PlaybackStorageMedium and fall back to CurrentTrackURI/
        # AVTransportURI.
        self._linkplay_playback_storage_medium: str | None = None
        self._linkplay_current_track_uri: str | None = None
        self._linkplay_avtransport_uri: str | None = None
        self._linkplay_track_source: str | None = None

        # Audio quality (best-effort) parsed from embedded DIDL metadata.
        self._linkplay_audio_rate_hz: int | None = None
        self._linkplay_audio_format_bits: int | None = None
        self._linkplay_audio_bitrate_kbps: int | None = None
        self._linkplay_last_metadata: str | None = None
        # Cache volume level locally because some LinkPlay/AudioPro devices
        # only report volume inside RenderingControl LastChange events.
        self._cached_volume_level: float | None = None

        # Cache repeat/shuffle because some LinkPlay/AudioPro devices only
        # report play mode inside AVTransport LastChange events.
        self._cached_shuffle: bool | None = None
        self._cached_repeat: RepeatMode | None = None
        self._cached_loop_mode: int | None = None

        # LinkPlay state derived from UPnP NOTIFY/LastChange (event-driven).
        # Stored as the UI-visible source string from LINKPLAY_SOURCE_LIST.
        self._linkplay_source_name: str | None = None
        # Extra artwork URL pulled from LinkPlay/AudioPro-specific UPnP actions
        # (e.g., GetInfoEx). Used as a fallback when standard DIDL metadata is
        # missing (notably during AirPlay).
        self._linkplay_media_image_url: str | None = None
        # Cache for HA media image proxy.
        # Tuple of (url_or_key, bytes, content_type)
        self._cached_media_image: tuple[str, bytes, str] | None = None
        self._cached_fallback_image: tuple[str, bytes] | None = None
        self._cached_icon_image: bytes | None = None

        self._background_setup_task: asyncio.Task[None] | None = None
        self._updated_registry: bool = False
        self._config_entry = config_entry
        self._attr_device_info = dr.DeviceInfo(connections={(dr.CONNECTION_UPNP, udn)})
        # We don't expose/bother with browsing for LinkPlay-based renderers
        self._can_browse_media = False
        self._resubscribe_reconnect = False
    def _find_service(self, service_type: str):
        """Best-effort lookup of a UPnP service.

        async_upnp_client's profile objects expose different ..."""
        if not self._device:
            return None

        profile_device = getattr(self._device, "profile_device", None)
        if profile_device is None:
            return None

        # Preferred: ProfileDevice.find_service
        try:
            if hasattr(profile_device, "find_service"):
                svc = profile_device.find_service(service_type)
                if svc is not None:
                    return svc
        except Exception:  # pragma: no cover
            pass

        # Fallback: underlying UpnpDevice.find_service
        upnp_device = getattr(profile_device, "device", None) or getattr(
            profile_device, "_device", None
        )
        try:
            if upnp_device is not None and hasattr(upnp_device, "find_service"):
                return upnp_device.find_service(service_type)
        except Exception:  # pragma: no cover
            pass

        return None

    async def _async_subscribe_playqueue(self) -> None:
            """Ensure we are subscribed to PlayQueue events (vendor service).
    
            LinkPlay/WiiM/Audio Pro devices report shuffle/repeat via the vendor
            PlayQueue service (LoopMode). We rely on GENA NOTIFY/LastChange only.
            """
            if not self._device:
                return
    
            handler = self._event_handler
            if handler is None:
                return
    
            svc = self._find_service(PLAYQUEUE_SERVICE_TYPE)
    
            # Some firmwares expose the same service under an unexpected type/id;
            # fall back to scanning all services by name/event path.
            if svc is None:
                profile_device = getattr(self._device, "profile_device", None)
                upnp_device = getattr(profile_device, "device", None) or getattr(
                    profile_device, "_device", None
                )
                all_services = []
                if upnp_device is not None:
                    services_obj = getattr(upnp_device, "services", None)
                    if isinstance(services_obj, dict):
                        all_services = list(services_obj.values())
                    elif services_obj is not None:
                        all_services = list(services_obj)
    
                for cand in all_services:
                    st = getattr(cand, "service_type", "") or ""
                    sid = getattr(cand, "service_id", "") or ""
                    ev = str(getattr(cand, "event_sub_url", "") or "")
                    if (
                        "PlayQueue" in st
                        or "PlayQueue" in sid
                        or "PlayQueue1" in ev
                        or "PlayQueue" in ev
                    ):
                        svc = cand
                        break
    
            if svc is None:
                _LOGGER.debug("PlayQueue service not found; shuffle/repeat updates from external controllers will not be received")
                return
    
            # Attach our event callback; vendor services are not wired by the DLNA profile.
            try:
                svc.on_event = self._on_event
            except Exception:  # pragma: no cover
                pass
    
            # Subscribe (with auto-renew if the handler supports it).
            try:
                import inspect
    
                kwargs = {}
                try:
                    params = inspect.signature(handler.async_subscribe).parameters
                    if "timeout" in params:
                        kwargs["timeout"] = timedelta(seconds=300)
                    if "auto_resubscribe" in params:
                        kwargs["auto_resubscribe"] = True
                except Exception:  # pragma: no cover
                    pass
    
                sub = await handler.async_subscribe(svc, **kwargs)
                self._playqueue_subscription = sub
                _LOGGER.debug(
                    "Subscribed to PlayQueue events: service_id=%s service_type=%s sid=%s",
                    getattr(svc, "service_id", None),
                    getattr(svc, "service_type", None),
                    getattr(sub, "sid", None),
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Failed to subscribe to PlayQueue events: %r", err)

    def _has_action(self, service_type: str, action_name: str) -> bool:
        """Return True if the device exposes the given action."""
        service = self._find_service(service_type)
        if not service:
            return False
        try:
            return service.action(action_name) is not None
        except Exception:  # defensive
            return False

    async def _async_call_action(
        self, service_type: str, action_name: str, **kwargs
    ) -> None:
        """Call a UPnP action via the underlying service."""
        service = self._find_service(service_type)
        if not service:
            raise UpnpError(f"Service not found: {service_type}")
        action = service.action(action_name)
        if not action:
            raise UpnpError(f"Action not found: {service_type}#{action_name}")

        # Inject InstanceID=0 if required and not provided.
        try:
            in_args = getattr(action, "in_arguments", [])
            if any(getattr(arg, "name", None) == "InstanceID" for arg in in_args):
                kwargs.setdefault("InstanceID", 0)
        except Exception:
            pass

        await action.async_call(**kwargs)

    async def _async_call_action_with_response(
        self, service_type: str, action_name: str, **kwargs
    ) -> dict[str, Any] | None:
        """Call a UPnP action and return response arguments.

        async_upnp_client returns a mapping of output argument names to values.
        Some LinkPlay/AudioPro devices expose non-standard actions (e.g.
        AVTransport:GetInfoEx) which are the best source for artwork when
        streaming via AirPlay.
        """
        service = self._find_service(service_type)
        if not service:
            return None
        action = service.action(action_name)
        if not action:
            return None

        # Inject InstanceID=0 if required and not provided.
        try:
            in_args = getattr(action, "in_arguments", [])
            if any(getattr(arg, "name", None) == "InstanceID" for arg in in_args):
                kwargs.setdefault("InstanceID", 0)
        except Exception:
            pass

        try:
            resp = await action.async_call(**kwargs)
        except Exception as err:  # pragma: no cover
            _LOGGER.debug("UPnP action %s#%s failed: %s", service_type, action_name, err)
            return None

        if resp is None:
            return None
        return dict(resp)

    def _has_avtransport_action(self, action_name: str) -> bool:
        """Return True if AVTransport exposes a specific action."""
        svc = self._find_service("urn:schemas-upnp-org:service:AVTransport:1")
        if svc is None:
            return False
        try:
            return svc.action(action_name) is not None
        except Exception:  # pragma: no cover
            return False


    def _has_playqueue_action(self, action_name: str) -> bool:
        """Return True if PlayQueue exposes a specific action."""
        svc = self._find_service(PLAYQUEUE_SERVICE_TYPE)
        if svc is None:
            return False
        try:
            return svc.action(action_name) is not None
        except Exception:  # pragma: no cover
            return False

    def _update_cached_loop_mode(self, loop_mode: int | str | None) -> None:
        """Update cached shuffle/repeat from a LinkPlay LoopMode value."""
        if loop_mode is None:
            return
        try:
            lm = int(loop_mode)
        except Exception:  # noqa: BLE001
            return

        self._cached_loop_mode = lm
        if lm in _LOOPMODE_TO_STATE:
            shuffle, repeat = _LOOPMODE_TO_STATE[lm]
            self._cached_shuffle = shuffle
            self._cached_repeat = repeat

    def _current_loop_mode(self) -> int:
        """Return the best-known LoopMode (defaults to normal)."""
        if self._cached_loop_mode is not None:
            return self._cached_loop_mode

        shuffle = self._cached_shuffle if self._cached_shuffle is not None else False
        repeat = self._cached_repeat if self._cached_repeat is not None else RepeatMode.OFF
        return _STATE_TO_LOOPMODE.get((shuffle, repeat), 4)
    async def _async_call_avtransport_action(self, action_name: str) -> None:
        """Call a simple AVTransport action such as Next/Previous."""
        svc = self._find_service("urn:schemas-upnp-org:service:AVTransport:1")
        if svc is None:
            raise UpnpError(f"AVTransport service not found for {action_name}")

        action = svc.action(action_name)
        if action is None:
            raise UpnpError(f"AVTransport action not found: {action_name}")

        # Most AVTransport actions require only InstanceID.
        kwargs: dict[str, Any] = {}
        try:
            in_args = getattr(action, "in_arguments", [])
            for arg in in_args:
                if getattr(arg, "name", None) == "InstanceID":
                    kwargs["InstanceID"] = 0
        except Exception:  # pragma: no cover
            kwargs = {"InstanceID": 0}

        await action.async_call(**kwargs)

    async def async_added_to_hass(self) -> None:
        """Handle addition."""
        # Update this entity when the associated config entry is modified
        self.async_on_remove(
            self._config_entry.add_update_listener(self.async_config_update_listener)
        )

        # Get SSDP notifications for only this device
        self.async_on_remove(
            await ssdp.async_register_callback(
                self.hass, self.async_ssdp_callback, {"USN": self.usn}
            )
        )

        # async_upnp_client.SsdpListener only reports byebye once for each *UDN*
        # (device name) which often is not the USN (service within the device)
        # that we're interested in. So also listen for byebye advertisements for
        # the UDN, which is reported in the _udn field of the combined_headers.
        self.async_on_remove(
            await ssdp.async_register_callback(
                self.hass,
                self.async_ssdp_callback,
                {"_udn": self.udn, "NTS": NotificationSubType.SSDP_BYEBYE},
            )
        )

        if not self._device:
            if self.hass.state is CoreState.running:
                await self._async_setup()
            else:
                self._background_setup_task = self.hass.async_create_background_task(
                    self._async_setup(), f"audiopro_linkplay {self.name} setup"
                )

    async def _async_setup(self) -> None:
        # Try to connect to the last known location, but don't worry if not available
        try:
            await self._device_connect(self.location)
        except UpnpError as err:
            _LOGGER.debug("Couldn't connect immediately: %r", err)

    async def async_will_remove_from_hass(self) -> None:
        """Handle removal."""
        if self._background_setup_task:
            self._background_setup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._background_setup_task
            self._background_setup_task = None

        await self._device_disconnect()

    async def async_ssdp_callback(
        self, info: SsdpServiceInfo, change: ssdp.SsdpChange
    ) -> None:
        """Handle notification from SSDP of device state change."""
        _LOGGER.debug(
            "SSDP %s notification of device %s at %s",
            change,
            info.ssdp_usn,
            info.ssdp_location,
        )

        try:
            bootid_str = info.ssdp_headers[ssdp.ATTR_SSDP_BOOTID]
            bootid: int | None = int(bootid_str, 10)
        except (KeyError, ValueError):
            bootid = None

        if change == ssdp.SsdpChange.UPDATE:
            # This is an announcement that bootid is about to change
            if self._bootid is not None and self._bootid == bootid:
                # Store the new value (because our old value matches) so that we
                # can ignore subsequent ssdp:alive messages
                with contextlib.suppress(KeyError, ValueError):
                    next_bootid_str = info.ssdp_headers[ssdp.ATTR_SSDP_NEXTBOOTID]
                    self._bootid = int(next_bootid_str, 10)
            # Nothing left to do until ssdp:alive comes through
            return

        if self._bootid is not None and self._bootid != bootid:
            # Device has rebooted
            # Maybe connection will succeed now
            self._ssdp_connect_failed = False
            if self._device:
                # Drop existing connection and maybe reconnect
                await self._device_disconnect()
        self._bootid = bootid

        if change == ssdp.SsdpChange.BYEBYE:
            # Device is going away
            if self._device:
                # Disconnect from gone device
                await self._device_disconnect()
            # Maybe the next alive message will result in a successful connection
            self._ssdp_connect_failed = False

        if (
            change == ssdp.SsdpChange.ALIVE
            and not self._device
            and not self._ssdp_connect_failed
        ):
            assert info.ssdp_location
            location = info.ssdp_location
            try:
                await self._device_connect(location)
            except UpnpError as err:
                self._ssdp_connect_failed = True
                _LOGGER.warning(
                    "Failed connecting to recently alive device at %s: %r",
                    location,
                    err,
                )

        # Device could have been de/re-connected, state probably changed
        self.async_write_ha_state()

    async def async_config_update_listener(
        self, hass: HomeAssistant, entry: config_entries.ConfigEntry
    ) -> None:
        """Handle options update by modifying self in-place."""
        _LOGGER.debug(
            "Updating: %s with data=%s and options=%s",
            self.name,
            entry.data,
            entry.options,
        )
        self.location = entry.data[CONF_URL]
        self.poll_availability = entry.options.get(CONF_POLL_AVAILABILITY, False)
        self._attr_should_poll = bool(self.poll_availability)
        self.browse_unfiltered = entry.options.get(CONF_BROWSE_UNFILTERED, False)

        new_mac_address = entry.data.get(CONF_MAC)
        if new_mac_address != self.mac_address:
            self.mac_address = new_mac_address
            self._update_device_registry(set_mac=True)

        new_host = entry.options.get(CONF_LISTEN_HOST)
        new_port = entry.options.get(CONF_LISTEN_PORT) or 0
        new_callback_url = entry.options.get(CONF_CALLBACK_URL_OVERRIDE)

        if (
            new_host == self._event_addr.host
            and new_port == self._event_addr.port
            and new_callback_url == self._event_addr.callback_url
        ):
            return

        # Changes to eventing requires a device reconnect for it to update correctly
        await self._device_disconnect()
        # Update _event_addr after disconnecting, to stop the right event listener
        self._event_addr = self._event_addr._replace(
            host=new_host, port=new_port, callback_url=new_callback_url
        )
        try:
            await self._device_connect(self.location)
        except UpnpError as err:
            _LOGGER.warning("Couldn't (re)connect after config change: %r", err)

        # Device was de/re-connected, state might have changed
        self.async_write_ha_state()

    def async_write_ha_state(self) -> None:
        """Write the state."""
        super().async_write_ha_state()


    async def _device_connect(self, location: str) -> None:
        """Connect to the device now that it's available."""
        _LOGGER.debug("Connecting to device at %s", location)

        async with self._device_lock:
            if self._device:
                _LOGGER.debug("Trying to connect when device already connected")
                return

            domain_data = get_domain_data(self.hass)

            # Connect to the base UPNP device
            upnp_device = await domain_data.upnp_factory.async_create_device(location)

            # Create/get event handler that is reachable by the device, using
            # the connection's local IP to listen only on the relevant interface
            if not self._event_addr.host:
                _, event_ip = await async_get_local_ip(location, self.hass.loop)
                self._event_addr = self._event_addr._replace(host=event_ip)
            event_handler = await domain_data.async_get_event_notifier(
                self._event_addr, self.hass
            )
            # Keep a reference so we can subscribe to vendor services too.
            self._event_handler = event_handler

            # Create profile wrapper
            self._device = DmrDevice(upnp_device, event_handler)

            self.location = location

            # Subscribe to event notifications
            try:
                self._device.on_event = self._on_event
                await self._device.async_subscribe_services(auto_resubscribe=True)
                await self._async_subscribe_playqueue()
            except UpnpResponseError as err:
                # Device rejected subscription request. This is OK, variables
                # will be polled instead.
                _LOGGER.debug("Device rejected subscription: %r", err)
            except UpnpError as err:
                # Don't leave the device half-constructed
                self._device.on_event = None
                self._device = None
                await domain_data.async_release_event_notifier(self._event_addr)
                _LOGGER.debug("Error while subscribing during device connect: %r", err)
                raise

        # We intentionally avoid httpapi.asp for status and rely on UPnP
        # eventing (GENA NOTIFY/LastChange) for all updates.

        self._update_device_registry()

    def _update_device_registry(self, set_mac: bool = False) -> None:
        """Update the device registry with new information about the DMR."""
        if (
            # Can't get all the required information without a connection
            not self._device
            or
            # No new information
            (not set_mac and self._updated_registry)
        ):
            return

        # Connections based on the root device's UDN, and the DMR embedded
        # device's UDN. They may be the same, if the DMR is the root device.
        connections = {
            (
                dr.CONNECTION_UPNP,
                self._device.profile_device.root_device.udn,
            ),
            (dr.CONNECTION_UPNP, self._device.udn),
            (
                dr.CONNECTION_UPNP,
                self.udn,
            ),
        }

        if self.mac_address:
            # Connection based on MAC address, if known
            connections.add(
                # Device MAC is obtained from the config entry, which uses getmac
                (dr.CONNECTION_NETWORK_MAC, self.mac_address)
            )

        device_info = dr.DeviceInfo(
            connections=connections,
            default_manufacturer=self._device.manufacturer,
            default_model=self._device.model_name,
            default_name=self._device.name,
        )
        self._attr_device_info = device_info

        self._updated_registry = True
        # Create linked HA DeviceEntry now the information is known.
        device_entry = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self._config_entry.entry_id, **device_info
        )

        # Update entity registry to link to the device
        er.async_get(self.hass).async_get_or_create(
            MEDIA_PLAYER_DOMAIN,
            DOMAIN,
            self.unique_id,
            device_id=device_entry.id,
            config_entry=self._config_entry,
        )

    async def _device_disconnect(self) -> None:
        """Destroy connections to the device now that it's not available.

        Also call when removing this entity from hass to clean up connections.
        """
        async with self._device_lock:
            if not self._device:
                _LOGGER.debug("Disconnecting from device that's not connected")
                return

            _LOGGER.debug("Disconnecting from %s", self._device.name)

            # Event-driven state; nothing to stop here beyond UPnP subscriptions.

            self._device.on_event = None
            old_device = self._device
            self._device = None
            await old_device.async_unsubscribe_services()

        domain_data = get_domain_data(self.hass)
        await domain_data.async_release_event_notifier(self._event_addr)

    async def async_update(self) -> None:
        """Retrieve the latest data."""
        if self._background_setup_task:
            await self._background_setup_task
            self._background_setup_task = None

        if not self._device:
            if not self.poll_availability:
                return
            try:
                await self._device_connect(self.location)
            except UpnpError:
                return

        assert self._device is not None

        if self._resubscribe_reconnect:
            self._resubscribe_reconnect = False
            try:
                await self._device_disconnect()
                await self._device_connect(self.location)
            except UpnpError:
                return

        try:
            do_ping = self.poll_availability or self.check_available
            await self._device.async_update(do_ping=do_ping)
        except UpnpError as err:
            _LOGGER.debug("Device unavailable: %r", err)
            await self._device_disconnect()
            return
        finally:
            self.check_available = False

        # Best-effort fallback: some firmwares surface CurrentTrackURI on the
        # profile object even when eventing is flaky.
        if isinstance(getattr(self._device, "current_track_uri", None), str):
            self._linkplay_current_track_uri = self._device.current_track_uri

    def _on_event(
        self, service: UpnpService, state_variables: Sequence[UpnpStateVariable]
    ) -> None:
        """State variable(s) changed, let home-assistant know."""
        if not state_variables:
            # Indicates a failure to resubscribe, check if device is still available
            self.check_available = True
            self._resubscribe_reconnect = True

        force_refresh = False

        svc_id = (getattr(service, "service_id", "") or "")
        svc_type = (getattr(service, "service_type", "") or "")
        # Some LinkPlay firmwares use non-standard service_id values; match by substring too.


        # LinkPlay/WiiM state is primarily driven by AVTransport LastChange.
        # We prefer PlaybackStorageMedium and fall back to CurrentTrackURI/
        # AVTransportURI.
        if "AVTransport" in svc_type or "AVTransport" in svc_id:
            for state_variable in state_variables:
                # Force a state refresh when player begins or pauses playback
                # to update the position info.
                if state_variable.name == "TransportState" and state_variable.value in (
                    TransportState.PLAYING,
                    TransportState.PAUSED_PLAYBACK,
                ):
                    force_refresh = True

                # Some firmwares surface these fields directly.
                if state_variable.name == "PlaybackStorageMedium":
                    self._linkplay_playback_storage_medium = self._normalize_lastchange_value(
                        state_variable.value
                    )
                    self._maybe_update_linkplay_source()
                    continue

                if state_variable.name == "TrackSource":
                    self._linkplay_track_source = self._normalize_lastchange_value(
                        state_variable.value
                    )
                    continue

                if state_variable.name in ("CurrentTrackMetaData", "TrackMetaData", "AVTransportURIMetaData"):
                    # Some firmwares expose metadata directly (not only inside LastChange).
                    self._update_audio_quality_from_metadata(
                        state_variable.value if isinstance(state_variable.value, str) else None
                    )
                    continue

                if state_variable.name in ("AVTransportURI", "CurrentTrackURI"):
                    if isinstance(state_variable.value, str):
                        if state_variable.name == "CurrentTrackURI":
                            self._linkplay_current_track_uri = self._normalize_lastchange_value(
                                state_variable.value
                            )
                        else:
                            self._linkplay_avtransport_uri = self._normalize_lastchange_value(
                                state_variable.value
                            )
                        self._maybe_update_linkplay_source()

                if state_variable.name == "LastChange" and isinstance(
                    state_variable.value, str
                ):
                    raw_lastchange = state_variable.value or ""
                    # Don't over-unescape LastChange; async_upnp_client often already decoded one layer.
                    unescaped = html.unescape(raw_lastchange)
                    try:
                        root = ET.fromstring(unescaped)
                    except Exception:  # noqa: BLE001
                        continue

                    saw_meta = False
                    track_metadata: str | None = None
                    av_metadata: str | None = None

                    for el in root.iter():
                        name = self._localname(el.tag)

                        if name == "PlaybackStorageMedium":
                            self._linkplay_playback_storage_medium = self._normalize_lastchange_value(
                                el.attrib.get("val") or el.text
                            )
                        elif name == "TrackSource":
                            self._linkplay_track_source = self._normalize_lastchange_value(
                                el.attrib.get("val") or el.text
                            )
                        elif name == "AVTransportURI":
                            self._linkplay_avtransport_uri = self._normalize_lastchange_value(
                                el.attrib.get("val") or el.text
                            )
                        elif name == "CurrentTrackURI":
                            self._linkplay_current_track_uri = self._normalize_lastchange_value(
                                el.attrib.get("val") or el.text
                            )
                        elif name == "CurrentTrackMetaData":
                            saw_meta = True
                            track_metadata = el.attrib.get("val") or el.text
                        elif name == "AVTransportURIMetaData":
                            saw_meta = True
                            av_metadata = el.attrib.get("val") or el.text
                        elif name == "LoopMode":
                            self._update_cached_loop_mode(el.attrib.get("val") or el.text)

                    if saw_meta:
                        # Prefer the metadata blob that actually contains LinkPlay audio tags.
                        def _looks_useful(meta: str | None) -> bool:
                            if not meta:
                                return False
                            s = str(meta)
                            return any(k in s for k in ("rate_hz", "format_s", "bitrate", "DIDL-Lite"))

                        chosen: str | None = None
                        if _looks_useful(track_metadata):
                            chosen = track_metadata
                        elif _looks_useful(av_metadata):
                            chosen = av_metadata
                        else:
                            chosen = track_metadata or av_metadata

                        self._update_audio_quality_from_metadata(chosen)

                    # Update inferred source after applying raw values.
                    self._maybe_update_linkplay_source()


        elif (
            "PlayQueue" in (getattr(service, "service_type", "") or "")
            or "PlayQueue" in (getattr(service, "service_id", "") or "")
            or "PlayQueue" in (str(getattr(service, "event_sub_url", "") or ""))
        ):
            # LinkPlay/WiiM devices encode shuffle+repeat in LoopMode via the
            # vendor PlayQueue service.
            for state_variable in state_variables:
                # Some firmwares (Audio Pro) misspell LoopMode as "LoopMpde" in
                # the LastChange payload; accept both.
                if state_variable.name in ("LoopMode", "LoopMpde"):
                    self._update_cached_loop_mode(state_variable.value)
                    continue

                if state_variable.name == "LastChange" and isinstance(
                    state_variable.value, str
                ):
                    raw = html.unescape(state_variable.value)
                    try:
                        root = ET.fromstring(raw)
                    except Exception:  # noqa: BLE001
                        # Some firmwares send LastChange fragments; fall back to
                        # a simple regex extract.
                        import re
                        m = re.search(r"<(?:LoopMode|LoopMpde)[^>]*>(\d+)</(?:LoopMode|LoopMpde)>", raw)
                        if not m:
                            m = re.search(r"(?:LoopMode|LoopMpde)[^v]*val=\"(\d+)\"", raw)
                        if m:
                            self._update_cached_loop_mode(m.group(1))
                        continue

                    for el in root.iter():
                        if self._localname(el.tag) not in ("LoopMode", "LoopMpde"):
                            continue
                        self._update_cached_loop_mode(el.attrib.get("val") or el.text)

        elif "RenderingControl" in svc_type or "RenderingControl" in svc_id:
            def _coerce_volume(val: Any) -> float | None:
                """Convert a device-reported volume to 0..1."""
                try:
                    f = float(val)
                except Exception:  # noqa: BLE001
                    return None
                # Many LinkPlay/AudioPro devices report 0..100 (integer).
                if f > 1.0:
                    if f <= 100.0:
                        f = f / 100.0
                    else:
                        # Unknown scale; clamp.
                        f = 1.0
                if f < 0.0:
                    f = 0.0
                if f > 1.0:
                    f = 1.0
                return f

            for state_variable in state_variables:
                # Some devices send Mute/Volume as normal state variables; others
                # only in LastChange.
                if state_variable.name == "Mute":
                    try:
                        self._cached_is_muted = bool(int(state_variable.value))
                    except Exception:  # noqa: BLE001
                        pass

                if state_variable.name == "Volume":
                    if (v := _coerce_volume(state_variable.value)) is not None:
                        self._cached_volume_level = v

                if state_variable.name == "LastChange" and isinstance(
                    state_variable.value, str
                ):
                    raw_lastchange = state_variable.value or ""
                    # Don't over-unescape LastChange; async_upnp_client often already decoded one layer.
                    unescaped = html.unescape(raw_lastchange)
                    try:
                        root = ET.fromstring(unescaped)
                    except Exception:  # noqa: BLE001
                        continue

                    for el in root.iter():
                        name = self._localname(el.tag)

                        if name not in ("Mute", "Volume"):
                            continue

                        # Prefer Master channel if specified.
                        channel = (
                            (el.attrib.get("channel") or el.attrib.get("Channel") or "")
                            .lower()
                        )
                        if channel and channel != "master":
                            continue

                        val = el.attrib.get("val")
                        if val is None:
                            continue

                        if name == "Mute":
                            try:
                                self._cached_is_muted = bool(int(val))
                            except Exception:  # noqa: BLE001
                                pass
                        elif name == "Volume":
                            if (v := _coerce_volume(val)) is not None:
                                self._cached_volume_level = v


        if self.state == MediaPlayerState.PLAYING:
            self._start_position_polling()
        else:
            self._stop_position_polling()

        if force_refresh:
            self.async_schedule_update_ha_state(force_refresh)
        else:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Device is available when we have a connection to it."""
        return self._device is not None and self._device.profile_device.available

    @property
    def unique_id(self) -> str:
        """Report the UDN (Unique Device Name) as this entity's unique ID."""
        return self.udn

    @property
    def usn(self) -> str:
        """Get the USN based on the UDN (Unique Device Name) and device type."""
        return f"{self.udn}::{self.device_type}"

    @property
    def state(self) -> MediaPlayerState | None:
        """State of the player."""
        if not self._device:
            return MediaPlayerState.OFF
        return _TRANSPORT_STATE_TO_MEDIA_PLAYER_STATE.get(
            self._device.transport_state, MediaPlayerState.IDLE
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose LinkPlay/WiiM diagnostics from AVTransport LastChange."""
        attrs: dict[str, Any] = dict(super().extra_state_attributes or {})

        # Raw fields used for source detection.
        attrs["linkplay_playback_storage_medium"] = (
            self._linkplay_playback_storage_medium or "-"
        )
        attrs["linkplay_current_track_uri"] = self._linkplay_current_track_uri or "-"
        attrs["linkplay_avtransport_uri"] = self._linkplay_avtransport_uri or "-"
        attrs["linkplay_track_source"] = self._linkplay_track_source or "-"

        # Audio quality (best-effort).
        attrs["linkplay_audio_rate_hz"] = (
            self._linkplay_audio_rate_hz if self._linkplay_audio_rate_hz is not None else "-"
        )
        attrs["linkplay_audio_format_bits"] = (
            self._linkplay_audio_format_bits if self._linkplay_audio_format_bits is not None else "-"
        )
        attrs["linkplay_audio_bitrate_kbps"] = (
            self._linkplay_audio_bitrate_kbps if self._linkplay_audio_bitrate_kbps is not None else "-"
        )

        return attrs


    def _linkplay_host(self) -> str | None:
        """Best-effort host extraction for LinkPlay HTTP API."""
        try:
            return urlparse(self.location).hostname
        except Exception:  # noqa: BLE001
            return None

    async def _async_send_httpapi_command(self, command: str) -> None:
        """Send a LinkPlay httpapi.asp command (best-effort)."""
        host = self._linkplay_host()
        if not host:
            _LOGGER.debug("No host for LinkPlay HTTP API (location=%s)", self.location)
            return

        cmd_q = quote(command, safe=":")  # keep colons readable
        url = f"https://{host}{LINKPLAY_HTTPAPI_PATH}?command={cmd_q}"

        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(LINKPLAY_HTTP_TIMEOUT):
                async with session.get(url, ssl=False) as resp:
                    await resp.read()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("LinkPlay HTTP API call failed (%s): %s", url, err)

    def _start_position_polling(self) -> None:
        """Poll position every 5s while playing (fallback for devices with flaky eventing)."""
        if self._unsub_position_poll is not None:
            return

        async def _poll(_: datetime) -> None:
            if self.state != MediaPlayerState.PLAYING:
                return
            try:
                await self._device.async_update()
            except UpnpError:
                return
            self.async_write_ha_state()

        self._unsub_position_poll = async_track_time_interval(
            self.hass, _poll, LINKPLAY_POSITION_POLL_INTERVAL
        )

    def _stop_position_polling(self) -> None:
        if self._unsub_position_poll is None:
            return
        self._unsub_position_poll()
        self._unsub_position_poll = None



    # Intentionally no getPlayerStatus/httpapi polling: all status is derived
    # from UPnP (GENA NOTIFY/LastChange) events.

    @staticmethod
    def _localname(tag: str) -> str:
        """Strip XML namespace from a tag."""
        return tag.split("}", 1)[-1] if "}" in tag else tag

    def _update_cached_play_mode_from_val(self, val: str | None) -> None:
        """Update cached shuffle/repeat from a UPnP CurrentPlayMode value."""
        if not val:
            return
        v = str(val).strip().upper()

        # Shuffle is commonly reported as SHUFFLE or RANDOM.
        self._cached_shuffle = ("SHUFFLE" in v) or (
            v in ("RANDOM", "RANDOM_REPEAT", "RANDOM_ALL")
        )

        # Repeat is often encoded as NORMAL/REPEAT_ALL/REPEAT_ONE (or variants).
        if "REPEAT" in v and "ONE" in v:
            self._cached_repeat = RepeatMode.ONE
        elif "REPEAT" in v and ("ALL" in v or v in ("REPEAT", "REPEAT_ALL")):
            self._cached_repeat = RepeatMode.ALL
        else:
            self._cached_repeat = RepeatMode.OFF

    @staticmethod
    def _normalize_lastchange_value(val: Any) -> str | None:
        """Normalize a UPnP/LastChange value to a stripped string.

        Treat None/empty/"NONE" (case-insensitive) as missing.
        """
        if val is None:
            return None
        s = str(val).strip()
        if not s:
            return None
        if s.strip().upper() in ("NONE", "UNKNOWN"):
            return None
        return s

    def _update_audio_quality_from_metadata(self, metadata: str | None) -> None:
        """Parse DIDL metadata for basic audio quality fields.

        LinkPlay/WiiM firmwares embed DIDL-Lite XML inside CurrentTrackMetaData /
        AVTransportURIMetaData inside LastChange. The XML is commonly HTML-escaped.

        We keep this best-effort and robust: failures simply leave fields as None.
        """
        raw = self._normalize_lastchange_value(metadata)
        if raw is None:
            # Only clear if metadata was explicitly provided as empty/none.
            self._linkplay_last_metadata = None
            self._linkplay_audio_rate_hz = None
            self._linkplay_audio_format_bits = None
            self._linkplay_audio_bitrate_kbps = None
            return

        # Avoid repeated parsing if metadata hasn't changed.
        if raw == self._linkplay_last_metadata:
            return
        self._linkplay_last_metadata = raw

        # Some firmwares use placeholders.
        if raw.lower() in ("not_implemented", "not implemented", "un_known", "unknown"):
            self._linkplay_audio_rate_hz = None
            self._linkplay_audio_format_bits = None
            self._linkplay_audio_bitrate_kbps = None
            return

        # Unescape a couple of layers; different firmwares escape differently.
        decoded = raw
        for _ in range(2):
            new_decoded = html.unescape(decoded)
            if new_decoded == decoded:
                break
            decoded = new_decoded
        decoded = decoded.strip()

        # Try to parse as XML. If it fails, try to extract the DIDL-Lite fragment.
        root: ET.Element | None = None
        try:
            root = ET.fromstring(decoded)
        except Exception:  # noqa: BLE001
            # Extract DIDL-Lite...
            start = decoded.find("<DIDL-Lite")
            end = decoded.rfind("</DIDL-Lite>")
            if start != -1 and end != -1:
                frag = decoded[start : end + len("</DIDL-Lite>")]
                try:
                    root = ET.fromstring(frag)
                except Exception:  # noqa: BLE001
                    root = None


        if root is None:
            # Regex fallback: some firmwares embed malformed XML but still include the audio tags.
            def _re_int(pat: str) -> int | None:
                mm = re.search(pat, decoded, flags=re.IGNORECASE | re.DOTALL)
                if not mm:
                    return None
                try:
                    return int(mm.group(1))
                except Exception:
                    return None

            rate_hz = _re_int(r"rate_hz[^0-9]{0,50}([0-9]{4,6})")
            bits = _re_int(r"format_s[^0-9]{0,50}([0-9]{1,3})")
            bitrate = _re_int(r"bitrate[^0-9]{0,50}([0-9]{2,5})")

            self._linkplay_audio_rate_hz = rate_hz
            self._linkplay_audio_format_bits = bits
            self._linkplay_audio_bitrate_kbps = bitrate
            return

        rate_hz: int | None = None
        bits: int | None = None
        bitrate: int | None = None
        for el in root.iter():
            name = self._localname(el.tag)
            if name == "rate_hz" and el.text:
                with contextlib.suppress(Exception):
                    rate_hz = int(str(el.text).strip())
            elif name == "format_s" and el.text:
                with contextlib.suppress(Exception):
                    bits = int(str(el.text).strip())
            elif name == "bitrate" and el.text:
                with contextlib.suppress(Exception):
                    bitrate = int(str(el.text).strip())

        self._linkplay_audio_rate_hz = rate_hz
        self._linkplay_audio_format_bits = bits
        self._linkplay_audio_bitrate_kbps = bitrate

    def _infer_source_from_playback_storage_medium(self, medium: str | None) -> str | None:
        """Infer UI source from PlaybackStorageMedium."""
        m = self._normalize_lastchange_value(medium)
        if not m:
            return None

        key = m.upper()
        if key in LINKPLAY_PLAYBACK_STORAGE_MEDIUM_TO_SOURCE_UI:
            return LINKPLAY_PLAYBACK_STORAGE_MEDIUM_TO_SOURCE_UI[key]

        # Treat any *-NETWORK token as Wi-Fi (covers additional firmwares).
        if key.endswith("-NETWORK"):
            return "Wi-Fi"
        if any(key.startswith(p) for p in LINKPLAY_NETWORK_TOKEN_PREFIXES):
            return "Wi-Fi"

        return None

    def _infer_source_from_uri(self, uri: str | None) -> str | None:
        """Infer the UI-visible source name from a LinkPlay/WiiM URI/token."""
        u = self._normalize_lastchange_value(uri)
        if not u:
            return None

        ul = u.lower()
        uu = u.upper()

        # AirPlay uses a special token/track URI.
        if "wiimu_airplay" in ul:
            return "AirPlay"

        # Some firmwares mirror PlaybackStorageMedium into URI fields.
        if uu in LINKPLAY_PLAYBACK_STORAGE_MEDIUM_TO_SOURCE_UI:
            return LINKPLAY_PLAYBACK_STORAGE_MEDIUM_TO_SOURCE_UI[uu]

        # Treat any *-NETWORK token as Wi-Fi.
        if uu.endswith("-NETWORK"):
            return "Wi-Fi"
        if any(uu.startswith(p) for p in LINKPLAY_NETWORK_TOKEN_PREFIXES):
            return "Wi-Fi"

        # Network streams sometimes show up as full URLs.
        if ul.startswith("http://") or ul.startswith("https://"):
            return "Wi-Fi"

        # Some firmwares report the switchmode token directly.
        if ul in LINKPLAY_SOURCE_TOKEN_TO_UI:
            return LINKPLAY_SOURCE_TOKEN_TO_UI[ul]

        return None

    def _infer_current_linkplay_source(self) -> str | None:
        """Infer the current source using LastChange-preferred signals."""
        # 1) Prefer PlaybackStorageMedium.
        if (src := self._infer_source_from_playback_storage_medium(self._linkplay_playback_storage_medium)) is not None:
            return src

        # 2) Fall back to URI fields.
        if (src := self._infer_source_from_uri(self._linkplay_current_track_uri)) is not None:
            return src
        if (src := self._infer_source_from_uri(self._linkplay_avtransport_uri)) is not None:
            return src

        # 3) Best-effort: profile current_track_uri (still derived from the same AVTransport state).
        if self._device:
            uri = getattr(self._device, "current_track_uri", None)
            if isinstance(uri, str):
                if (src := self._infer_source_from_uri(uri)) is not None:
                    return src

        return None

    def _maybe_update_linkplay_source(self) -> None:
        """Update cached linkplay source name based on latest raw signals."""
        src = self._infer_current_linkplay_source()
        if not src:
            return

        if src != self._linkplay_source_name:
            self._linkplay_source_name = src
            # Source switch affects artwork.
            self._cached_fallback_image = None
            self._cached_media_image = None

    @property
    def source_list(self) -> list[str] | None:
        """Return available inputs (LinkPlay/WiiM)."""
        return LINKPLAY_SOURCE_LIST

    @property
    def source(self) -> str | None:
        """Return current input/source."""
        if (src := self._infer_current_linkplay_source()) is not None:
            self._linkplay_source_name = src
            return src

        return self._linkplay_source_name

    async def async_select_source(self, source: str) -> None:
        """Select input/source via LinkPlay HTTP API.

        NOTE: "AirPlay" and "Multiroom (Secondary)" are visibility-only and are
        treated as no-ops here.
        """
        if source not in LINKPLAY_SOURCE_LIST:
            raise HomeAssistantError(f"Unknown source: {source}")

        token = LINKPLAY_SOURCE_UI_TO_TOKEN.get(source)
        if token is None:
            # Visibility-only, don't try to force an input.
            _LOGGER.debug("Ignoring select_source(%s) for %s", source, self.name)
            return

        await self._async_send_httpapi_command(f"setPlayerCmd:switchmode:{token}")
        # Optimistically update; UPnP NOTIFY/LastChange will confirm.
        self._linkplay_source_name = source
        self._cached_fallback_image = None
        self._cached_media_image = None
        self.async_write_ha_state()

    @property
    def volume_level(self) -> float | None:
        """Volume level of the media player (0..1)."""
        # Prefer cached level updated from UPnP events.
        if self._cached_volume_level is not None:
            return self._cached_volume_level
        if not self._device or not self._device.has_volume_level:
            return None
        return self._device.volume_level

    @catch_request_errors
    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        assert self._device is not None
        vol = max(0.0, min(1.0, float(volume)))
        await self._device.async_set_volume_level(vol)
        # Optimistically update; event will confirm.
        self._cached_volume_level = vol
        self.async_write_ha_state()

    def _get_volume_step(self) -> float:
        """Return the configured volume step (0.01..0.05).

        Stored in options as an integer percent (1..5). Be forgiving if the
        value is stored as a float (0.02) or a string ("2%", "0.02").
        """
        raw = self._config_entry.options.get(CONF_VOLUME_STEP_PCT, DEFAULT_VOLUME_STEP_PCT)
        pct: int
        if raw is None:
            pct = DEFAULT_VOLUME_STEP_PCT
        elif isinstance(raw, str):
            s = raw.strip()
            if s.endswith("%"):
                s = s[:-1].strip()
            try:
                f = float(s)
            except ValueError:
                pct = DEFAULT_VOLUME_STEP_PCT
            else:
                pct = round(f * 100) if f <= 0.5 else round(f)
        else:
            try:
                f = float(raw)
            except (TypeError, ValueError):
                pct = DEFAULT_VOLUME_STEP_PCT
            else:
                pct = round(f * 100) if f <= 0.5 else round(f)

        pct = int(max(MIN_VOLUME_STEP_PCT, min(MAX_VOLUME_STEP_PCT, pct)))
        return pct / 100.0

    async def async_volume_up(self) -> None:
        """Increase volume by a configured step (default 2%)."""
        current = self.volume_level if self.volume_level is not None else 0.0
        step = self._get_volume_step()
        target = round(min(1.0, current + step), 2)
        await self.async_set_volume_level(target)

    async def async_volume_down(self) -> None:
        """Decrease volume by a configured step (default 2%)."""
        current = self.volume_level if self.volume_level is not None else 0.0
        step = self._get_volume_step()
        target = round(max(0.0, current - step), 2)
        await self.async_set_volume_level(target)

    @property
    def is_volume_muted(self) -> bool | None:
        """Boolean if volume is currently muted."""
        if not self._device:
            return self._cached_is_muted

        muted = self._device.is_volume_muted
        # Prefer cached mute state which is updated via UPnP events (and set
        # optimistically on write operations).
        if self._cached_is_muted is not None:
            return self._cached_is_muted

        if muted is not None:
            self._cached_is_muted = muted
        return muted

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute/unmute the device."""
        if self._device is None:
            raise HomeAssistantError('Device not connected')
        if not self._device.has_volume_mute:
            raise HomeAssistantError('Device does not support volume mute')

        desired_mute = bool(mute)
        # Optimistically update HA state so the UI toggle works even if the
        # device does not event mute status reliably.
        self._cached_is_muted = desired_mute
        self.async_write_ha_state()

        rendering_service = "urn:schemas-upnp-org:service:RenderingControl:1"

        # Prefer UPnP when possible; fall back to LinkPlay HTTP (which most
        # firmwares implement consistently).
        try:
            # Profile helper (if available) is typically the cleanest.
            await self._device.async_mute_volume(desired_mute)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("UPnP mute via profile failed: %s", err)
            try:
                await self._async_call_action(
                    rendering_service,
                    "SetMute",
                    InstanceID=0,
                    Channel="Master",
                    DesiredMute=1 if desired_mute else 0,
                )
            except Exception as err2:  # noqa: BLE001
                _LOGGER.debug("UPnP SetMute failed: %s", err2)
                raise

    @catch_request_errors
    async def async_media_play(self) -> None:
        """Send play command."""
        assert self._device is not None
        await self._device.async_play()

    @catch_request_errors
    async def async_media_pause(self) -> None:
        """Send pause command."""
        assert self._device is not None
        await self._device.async_pause()

    @catch_request_errors
    async def async_media_stop(self) -> None:
        """Send stop command."""
        assert self._device is not None
        await self._device.async_stop()


    @catch_request_errors
    async def async_turn_off(self) -> None:
        """Shut down the device immediately.

        LinkPlay/WiiM devices typically do not expose a reliable power state via UPnP.
        This implements HA's turn_off as a best-effort shutdown command.
        """
        await self._async_send_httpapi_command("setShutdown:0")

    @catch_request_errors
    async def async_media_seek(self, position: float) -> None:
        """Send seek command."""
        assert self._device is not None
        time = timedelta(seconds=position)
        await self._device.async_seek_rel_time(time)

    @catch_request_errors
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a piece of media."""
        _LOGGER.debug("Playing media: %s, %s, %s", media_type, media_id, kwargs)
        assert self._device is not None

        didl_metadata: str | None = None
        title: str = ""

        # If media is media_source, resolve it to url and MIME type, and maybe metadata
        if media_source.is_media_source_id(media_id):
            sourced_media = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_type = sourced_media.mime_type
            media_id = sourced_media.url
            _LOGGER.debug("sourced_media is %s", sourced_media)
            if sourced_metadata := getattr(sourced_media, "didl_metadata", None):
                didl_metadata = didl_lite.to_xml_string(sourced_metadata).decode(
                    "utf-8"
                )
                title = sourced_metadata.title

        # If media ID is a relative URL, we serve it from HA.
        media_id = async_process_play_media_url(self.hass, media_id)

        # LinkPlay/WiiM extension: enqueue/play a URI via httpapi.asp.
        # This matches the common controller behavior for adding internet radio
        # and other streams without relying on UPnP SetAVTransportURI.
        if isinstance(media_id, str) and (
            media_id.startswith("http://") or media_id.startswith("https://")
        ):
            await self._async_send_httpapi_command(f"setPlayerCmd:play:{media_id}")
            return

        extra: dict[str, Any] = kwargs.get(ATTR_MEDIA_EXTRA) or {}
        metadata: dict[str, Any] = extra.get("metadata") or {}

        if not title:
            title = extra.get("title") or metadata.get("title") or "Home Assistant"
        if thumb := extra.get("thumb"):
            metadata["album_art_uri"] = thumb

        # Translate metadata keys from HA names to DIDL-Lite names
        for hass_key, didl_key in MEDIA_METADATA_DIDL.items():
            if hass_key in metadata:
                metadata[didl_key] = metadata.pop(hass_key)

        if not didl_metadata:
            # Create metadata specific to the given media type; different fields are
            # available depending on what the upnp_class is.
            upnp_class = MEDIA_UPNP_CLASS_MAP.get(media_type)
            didl_metadata = await self._device.construct_play_media_metadata(
                media_url=media_id,
                media_title=title,
                override_upnp_class=upnp_class,
                meta_data=metadata,
            )

        # Stop current playing media
        if self._device.can_stop:
            await self.async_media_stop()

        # Queue media
        await self._device.async_set_transport_uri(media_id, title, didl_metadata)

        # If already playing, or don't want to autoplay, no need to call Play
        autoplay = extra.get("autoplay", True)
        if self._device.transport_state == TransportState.PLAYING or not autoplay:
            return

        # Play it
        await self._device.async_wait_for_can_play()
        await self.async_media_play()

    async def _async_avtransport_next_prev(self, action: str) -> None:
        """Call AVTransport Next/Previous matching the WiiM app.

        Your capture shows the app includes a vendor-specific ControlSource
        argument (ControlSource=WiiMApp). Some firmwares require it; others
        don't define it. We try with ControlSource first, then retry without it
        only if the device rejects unknown arguments.
        """
        assert self._device is not None

        service_type = "urn:schemas-upnp-org:service:AVTransport:1"

        args_with_cs = {"InstanceID": 0, "ControlSource": "WiiMApp"}
        args_no_cs = {"InstanceID": 0}

        try:
            await self._async_call_action(service_type, action, **args_with_cs)
            return
        except Exception as err:  # noqa: BLE001
            msg = str(err)
            if "ControlSource" in msg or "Unknown argument" in msg or "unexpected" in msg:
                # Retry without ControlSource for firmwares which don't define it.
                await self._async_call_action(service_type, action, **args_no_cs)
                return
            raise

    @catch_request_errors
    async def async_media_previous_track(self) -> None:
        """Send previous track command (UPnP SOAP, app-compatible)."""
        # Only expose/attempt if AVTransport declares the action.
        if not self._has_avtransport_action("Previous"):
            raise HomeAssistantError("Previous track is not supported by this device")

        await self._async_avtransport_next_prev("Previous")

    @catch_request_errors
    async def async_media_next_track(self) -> None:
        """Send next track command (UPnP SOAP, app-compatible)."""
        if not self._has_avtransport_action("Next"):
            raise HomeAssistantError("Next track is not supported by this device")

        await self._async_avtransport_next_prev("Next")


    @property
    def shuffle(self) -> bool | None:
        """Boolean if shuffle is enabled.

        For LinkPlay/WiiM devices this is derived from the PlayQueue LoopMode
        reported via UPnP events.
        """
        return self._cached_shuffle

    @catch_request_errors
    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Enable/disable shuffle mode (PlayQueue LoopMode)."""
        assert self._device is not None

        if not self._has_playqueue_action("SetQueueLoopMode"):
            raise HomeAssistantError("Device does not support shuffle")

        repeat = self.repeat or RepeatMode.OFF
        loop_mode = _STATE_TO_LOOPMODE.get((bool(shuffle), repeat), 4)

        # Optimistic update: events will confirm/correct.
        self._update_cached_loop_mode(loop_mode)
        self.async_write_ha_state()

        await self._async_call_action(
            PLAYQUEUE_SERVICE_TYPE, "SetQueueLoopMode", LoopMode=loop_mode
        )

    @property
    def repeat(self) -> RepeatMode | None:
        """Return current repeat mode (PlayQueue LoopMode)."""
        return self._cached_repeat

    @catch_request_errors
    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        """Set repeat mode (PlayQueue LoopMode)."""
        assert self._device is not None

        if not self._has_playqueue_action("SetQueueLoopMode"):
            raise HomeAssistantError("Device does not support repeat")

        shuffle = self.shuffle if self.shuffle is not None else False
        loop_mode = _STATE_TO_LOOPMODE.get((bool(shuffle), repeat), 4)

        self._update_cached_loop_mode(loop_mode)
        self.async_write_ha_state()

        await self._async_call_action(
            PLAYQUEUE_SERVICE_TYPE, "SetQueueLoopMode", LoopMode=loop_mode
        )

    def _linkplay_preset_names(self) -> list[str]:
        """Return preset names for LinkPlay devices.

        We keep this minimal and return an empty list so Home Assistant shows
        the fixed Preset 1-6 options (LINKPLAY_PRESET_MODES) without vendor
        specific entries like 'FactoryDefaults'.
        """
        return []


    @property
    def sound_mode_list(self) -> list[str] | None:
        """Return sound mode list."""
        preset_names = self._linkplay_preset_names()
        if not preset_names:
            return LINKPLAY_PRESET_MODES

        # Hide the LinkPlay internal default from the UI.
        cleaned = [
            name
            for name in preset_names
            if name and name.strip() and name.strip().lower() != 'factorydefaults'
        ]
        return cleaned + LINKPLAY_PRESET_MODES



    def _calc_supported_features(self) -> int:
        """Compute the supported features.

        We compute this dynamically (instead of caching a private method) to
        avoid setup issues if Home Assistant core changes how supported
        features are represented.
        """
        features = (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.SEEK
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.SELECT_SOURCE
            | MediaPlayerEntityFeature.TURN_OFF
        )

        if self.sound_mode_list:
            features |= MediaPlayerEntityFeature.SELECT_SOUND_MODE

        # PlayQueue controls loop/shuffle for LinkPlay/WiiM/AudioPro devices.
        if self._has_playqueue_action("SetQueueLoopMode"):
            features |= MediaPlayerEntityFeature.REPEAT_SET
            features |= MediaPlayerEntityFeature.SHUFFLE_SET

        if self._has_playqueue_action("DeleteQueue"):
            features |= MediaPlayerEntityFeature.CLEAR_PLAYLIST

        if self._can_browse_media:
            features |= MediaPlayerEntityFeature.BROWSE_MEDIA

        return features

    @property
    def supported_features(self) -> int:
        """Return supported features."""
        return self._calc_supported_features()

    @catch_request_errors
    async def async_select_sound_mode(self, sound_mode: str) -> None:
        """Select sound mode.

        Also supports LinkPlay/WiiM "Preset 1".."Preset 6" via HTTP.
        """
        assert self._device is not None

        if sound_mode in LINKPLAY_PRESET_MODES:
            preset_num = int(sound_mode.split(" ", 1)[1])
            await self._async_send_httpapi_command(f"MCUKeyShortClick:{preset_num}")
            return

        await self._device.async_select_preset(sound_mode)

    @catch_request_errors
    async def async_clear_playlist(self) -> None:
        """Clear the active queue/playlist.

        For LinkPlay/WiiM-based renderers this maps to the vendor PlayQueue
        service action DeleteQueue(...). If the device doesn't expose the
        service, the call is ignored.
        """
        if not self._device:
            return

        service_type = "urn:schemas-wiimu-com:service:PlayQueue:1"
        if self._find_service(service_type) is None:
            _LOGGER.debug("PlayQueue service not present; clear_playlist ignored")
            return

        await self._async_call_action(service_type, "DeleteQueue", QueueName="CurrentQueue")
        await self._device.async_update()
        self.async_write_ha_state()

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Implement the websocket media browsing helper.

        Browses all available media_sources by default. Filters content_type
        based on the DMR's sink_protocol_info.
        """
        _LOGGER.debug(
            "async_browse_media(%s, %s)", media_content_type, media_content_id
        )

        # media_content_type is ignored; it's the content_type of the current
        # media_content_id, not the desired content_type of whomever is calling.

        if self.browse_unfiltered:
            content_filter = None
        else:
            content_filter = self._get_content_filter()

        return await media_source.async_browse_media(
            self.hass, media_content_id, content_filter=content_filter
        )

    def _get_content_filter(self) -> Callable[[BrowseMedia], bool]:
        """Return a function that filters media based on what the renderer can play.

        The filtering is pretty loose; it's better to show something that can't
        be played than hide something that can.
        """
        if not self._device or not self._device.sink_protocol_info:
            # Nothing is specified by the renderer, so show everything
            _LOGGER.debug("Get content filter with no device or sink protocol info")
            return lambda _: True

        _LOGGER.debug("Get content filter for %s", self._device.sink_protocol_info)
        if self._device.sink_protocol_info[0] == "*":
            # Renderer claims it can handle everything, so show everything
            return lambda _: True

        # Convert list of things like "http-get:*:audio/mpeg;codecs=mp3:*"
        # to just "audio/mpeg"
        content_types = set[str]()
        for protocol_info in self._device.sink_protocol_info:
            protocol, _, content_format, _ = protocol_info.split(":", 3)
            # Transform content_format for better generic matching
            content_format = content_format.lower().replace("/x-", "/", 1)
            content_format = content_format.partition(";")[0]

            if protocol in STREAMABLE_PROTOCOLS:
                content_types.add(content_format)

        def _content_filter(item: BrowseMedia) -> bool:
            """Filter media items by their media_content_type."""
            content_type = item.media_content_type
            content_type = content_type.lower().replace("/x-", "/", 1).partition(";")[0]
            return content_type in content_types

        return _content_filter

    @property
    def media_title(self) -> str | None:
        """Title of current playing media."""
        if not self._device:
            return None
        # Use the best available title
        return self._device.media_program_title or self._device.media_title

    @property
    def media_image_url(self) -> str | None:
        """Image url of current playing media.

        Home Assistant will only render entity_picture if this is non-null.
        When the device doesn't provide artwork (inputs/idle), we return a
        sentinel URL to force HA to call async_get_media_image(), where we
        serve a local icon.png (or SVG fallback).
        """
        if not self._device:
            return None
        url = self._linkplay_media_image_url or self._device.media_image_url
        return url or "audiopro_linkplay://fallback"

    async def async_get_media_image(self) -> tuple[bytes, str] | None:
        """Return bytes of the current media artwork for the HA proxy.

        This integration prefers fully event-driven state, but HA's media player
        proxy expects an image fetcher. We fetch the URL advertised by the device
        (often a local http/https URL). Some LinkPlay-based devices advertise
        https URLs with self-signed certificates; for those we disable SSL
        verification for the fetch.
        """
        if not self.hass:
            return None

        url = self.media_image_url
        if url and url.startswith("audiopro_linkplay://"):
            url = None

        if url:
            cached = self._cached_media_image
            if cached and cached[0] == url:
                return cached[1], cached[2]

            session = async_get_clientsession(self.hass)
            ssl = False if url.startswith("https://") else None
            try:
                async with session.get(url, ssl=ssl, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        content_type = resp.headers.get("Content-Type", "image/jpeg")
                        # Strip charset, etc.
                        content_type = content_type.split(";")[0].strip()
                        self._cached_media_image = (url, data, content_type)
                        return data, content_type
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Failed fetching media artwork from %s: %s", url, err)

        # Fallback: prefer a local integration icon (icon.png) if present.
        local_icon = await self._async_get_local_icon_image()
        if local_icon is not None:
            return local_icon

        # Otherwise return a tiny SVG badge so the UI always has something.
        label = (self.source or "Idle").strip() or "Idle"
        fb_cached = self._cached_fallback_image
        if fb_cached and fb_cached[0] == label:
            return fb_cached[1], "image/svg+xml"

        svg = self._build_fallback_svg(label)
        self._cached_fallback_image = (label, svg)
        return svg, "image/svg+xml"


    async def _async_get_local_icon_image(self) -> tuple[bytes, str] | None:
        """Return (bytes, content_type) for local icon.png fallback, if present."""
        if self._cached_icon_image is not None:
            return self._cached_icon_image, "image/png"

        icon_path = Path(__file__).with_name("icon.png")
        if not icon_path.exists():
            return None

        if not self.hass:
            return None

        try:
            data: bytes = await self.hass.async_add_executor_job(icon_path.read_bytes)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed reading icon.png fallback: %s", err)
            return None

        self._cached_icon_image = data
        return data, "image/png"


    @staticmethod
    def _build_fallback_svg(label: str) -> bytes:
        """Build a simple SVG placeholder image."""
        # Keep it safe for XML.
        safe = (
            label.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )
        # Simple icon + label. Avoid relying on external fonts.
        svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="640" height="640" viewBox="0 0 640 640">
  <rect width="640" height="640" rx="48" ry="48" fill="#222"/>
  <g fill="#fff" opacity="0.9">
    <path d="M210 250h80l90-70v280l-90-70h-80z"/>
    <path d="M420 250c35 30 35 110 0 140" fill="none" stroke="#fff" stroke-width="18" stroke-linecap="round"/>
    <path d="M465 220c60 55 60 145 0 200" fill="none" stroke="#fff" stroke-width="14" stroke-linecap="round" opacity="0.7"/>
  </g>
  <text x="320" y="560" text-anchor="middle" font-size="44" fill="#fff" opacity="0.95">{safe}</text>
</svg>"""
        return svg.encode("utf-8")


    @property
    def media_content_id(self) -> str | None:
        """Content ID of current playing media."""
        if not self._device:
            return None
        return self._device.current_track_uri

    @property
    def media_content_type(self) -> MediaType | None:
        """Content type of current playing media."""
        if not self._device or not self._device.media_class:
            return None
        return MEDIA_TYPE_MAP.get(self._device.media_class)

    @property
    def media_duration(self) -> int | None:
        """Duration of current playing media in seconds."""
        if not self._device:
            return None
        return self._device.media_duration

    @property
    def media_position(self) -> int | None:
        """Position of current playing media in seconds."""
        if not self._device:
            return None
        return self._device.media_position

    @property
    def media_position_updated_at(self) -> datetime | None:
        """When was the position of the current playing media valid.

        Returns value from homeassistant.util.dt.utcnow().
        """
        if not self._device:
            return None
        return self._device.media_position_updated_at

    @property
    def media_artist(self) -> str | None:
        """Artist of current playing media, music track only."""
        if not self._device:
            return None
        return self._device.media_artist

    @property
    def media_album_name(self) -> str | None:
        """Album name of current playing media, music track only."""
        if not self._device:
            return None
        return self._device.media_album_name

    @property
    def media_album_artist(self) -> str | None:
        """Album artist of current playing media, music track only."""
        if not self._device:
            return None
        return self._device.media_album_artist

    @property
    def media_track(self) -> int | None:
        """Track number of current playing media, music track only."""
        if not self._device:
            return None
        return self._device.media_track_number

    @property
    def media_series_title(self) -> str | None:
        """Title of series of current playing media, TV show only."""
        if not self._device:
            return None
        return self._device.media_series_title

    @property
    def media_season(self) -> str | None:
        """Season number, starting at 1, of current playing media, TV show only."""
        if not self._device:
            return None
        # Some DMRs, like Kodi, leave this as 0 and encode the season & episode
        # in the episode_number metadata, as {season:d}{episode:02d}
        if (
            not self._device.media_season_number
            or self._device.media_season_number == "0"
        ) and self._device.media_episode_number:
            with contextlib.suppress(ValueError):
                episode = int(self._device.media_episode_number, 10)
                if episode > 100:
                    return str(episode // 100)
        return self._device.media_season_number

    @property
    def media_episode(self) -> str | None:
        """Episode number of current playing media, TV show only."""
        if not self._device:
            return None
        # Complement to media_season math above
        if (
            not self._device.media_season_number
            or self._device.media_season_number == "0"
        ) and self._device.media_episode_number:
            with contextlib.suppress(ValueError):
                episode = int(self._device.media_episode_number, 10)
                if episode > 100:
                    return str(episode % 100)
        return self._device.media_episode_number

    @property
    def media_channel(self) -> str | None:
        """Channel name currently playing."""
        if not self._device:
            return None
        return self._device.media_channel_name

    @property
    def media_playlist(self) -> str | None:
        """Title of Playlist currently playing."""
        if not self._device:
            return None
        return self._device.media_playlist_title
