"""DataUpdateCoordinator for INIM Alarm."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import InimApi, InimApiError, InimAuthError
from .websocket import InimWebSocketClient
from .const import (
    CHANGED_BY_EXTERNAL,
    CHANGED_BY_HOME_ASSISTANT,
    CHANGED_BY_UNKNOWN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_ALARM_TRIGGERED,
    EVENT_STATE_CHANGED,
)

_LOGGER = logging.getLogger(__name__)


class InimDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching INIM data."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: InimApi,
        update_interval: timedelta = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.api = api
        self._ws_client = InimWebSocketClient(api, self._on_websocket_update)
        self._devices: list[dict[str, Any]] = []
        self._previous_alarm_states: dict[tuple[int, int], bool] = {}
        self._previous_armed_states: dict[tuple[int, int], int] = {}
        self._pending_ha_commands: dict[tuple[int, int | None], datetime] = {}
        self._last_changed_by: dict[str, str] = {}
        self._last_changed_at: dict[str, datetime] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from INIM API."""
        try:
            devices = await self.api.get_devices()

            if not devices:
                _LOGGER.warning("No devices found in INIM Cloud")
                return {"devices": []}

            self._devices = devices

            data: dict[str, Any] = {
                "devices": [],
            }

            for device in devices:
                device_data = {
                    "device_id": device.get("DeviceId"),
                    "name": device.get("Name", "INIM Alarm"),
                    "serial_number": device.get("SerialNumber"),
                    "model": f"{device.get('ModelFamily', '')} {device.get('ModelNumber', '')}".strip(),
                    "firmware": f"{device.get('FirmwareVersionMajor', '')}.{device.get('FirmwareVersionMinor', '')}",
                    "voltage": device.get("Voltage"),
                    "active_scenario": device.get("ActiveScenario"),
                    "network_status": device.get("NetworkStatus"),
                    "faults": device.get("Faults", 0),
                    "areas": device.get("Areas", []),
                    "zones": device.get("Zones", []),
                    "scenarios": device.get("Scenarios", []),
                    "peripherals": device.get("Peripherals", []),
                    "thermostats": device.get("Thermostats", []),
                    "blinds": device.get("Blinds", []),
                }
                data["devices"].append(device_data)

            _LOGGER.debug("Updated data for %d devices", len(data["devices"]))

            self._check_alarm_triggered(data)

            return data

        except InimAuthError as err:
            _LOGGER.error("Authentication error: %s", err)
            raise UpdateFailed(f"Authentication error: {err}") from err
        except InimApiError as err:
            _LOGGER.error("API error: %s", err)
            raise UpdateFailed(f"API error: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error updating INIM data")
            raise UpdateFailed(f"Unexpected error: {err}") from err

    def get_device(self, device_id: int) -> dict[str, Any] | None:
        """Get a specific device by ID."""
        if not self.data:
            return None
        for device in self.data.get("devices", []):
            if device.get("device_id") == device_id:
                return device
        return None

    def get_zone(self, device_id: int, zone_id: int) -> dict[str, Any] | None:
        """Get a specific zone by device and zone ID."""
        device = self.get_device(device_id)
        if not device:
            return None
        for zone in device.get("zones", []):
            if zone.get("ZoneId") == zone_id:
                return zone
        return None

    def get_area(self, device_id: int, area_id: int) -> dict[str, Any] | None:
        """Get a specific area by device and area ID."""
        device = self.get_device(device_id)
        if not device:
            return None
        for area in device.get("areas", []):
            if area.get("AreaId") == area_id:
                return area
        return None

    def get_scenario(self, device_id: int, scenario_id: int) -> dict[str, Any] | None:
        """Get a specific scenario by device and scenario ID."""
        device = self.get_device(device_id)
        if not device:
            return None
        for scenario in device.get("scenarios", []):
            if scenario.get("ScenarioId") == scenario_id:
                return scenario
        return None

    def get_active_scenario(self, device_id: int) -> dict[str, Any] | None:
        """Get the currently active scenario for a device."""
        device = self.get_device(device_id)
        if not device:
            return None
        active_id = device.get("active_scenario")
        if active_id is not None:
            return self.get_scenario(device_id, active_id)
        return None

    def _check_alarm_triggered(self, data: dict[str, Any]) -> None:
        """Check for alarm state changes and fire events."""
        for device in data.get("devices", []):
            device_id = device.get("device_id")
            if not device_id:
                continue

            for area in device.get("areas", []):
                area_id = area.get("AreaId")
                area_name = area.get("Name", f"Area {area_id}")
                current_alarm = area.get("Alarm", False)
                current_armed = area.get("Armed", 4)

                key = (device_id, area_id)
                previous_alarm = self._previous_alarm_states.get(key, False)
                previous_armed = self._previous_armed_states.get(key)

                if current_alarm and not previous_alarm:
                    _LOGGER.warning(
                        "ALARM TRIGGERED! Device: %s, Area: %s (%s)",
                        device_id,
                        area_id,
                        area_name,
                    )
                    self.hass.bus.async_fire(
                        EVENT_ALARM_TRIGGERED,
                        {
                            "device_id": device_id,
                            "device_name": device.get("name", "INIM Alarm"),
                            "area_id": area_id,
                            "area_name": area_name,
                        },
                    )

                if previous_armed is not None and current_armed != previous_armed:
                    self._handle_armed_state_change(
                        device_id,
                        area_id,
                        area_name,
                        device.get("name", "INIM Alarm"),
                        previous_armed,
                        current_armed,
                    )

                self._previous_alarm_states[key] = current_alarm
                self._previous_armed_states[key] = current_armed

    def _handle_armed_state_change(
        self,
        device_id: int,
        area_id: int,
        area_name: str,
        device_name: str,
        previous_armed: int,
        current_armed: int,
    ) -> None:
        """Handle armed state change and determine source."""
        now = dt_util.now()

        entity_key_area = f"{device_id}_area_{area_id}"
        entity_key_main = f"{device_id}_alarm"

        pending_key_area = (device_id, area_id)
        pending_key_main = (device_id, None)

        is_ha_command = False
        pending_time = None

        if pending_key_area in self._pending_ha_commands:
            pending_time = self._pending_ha_commands[pending_key_area]
            if (now - pending_time).total_seconds() < 60:
                is_ha_command = True
                del self._pending_ha_commands[pending_key_area]

        if not is_ha_command and pending_key_main in self._pending_ha_commands:
            pending_time = self._pending_ha_commands[pending_key_main]
            if (now - pending_time).total_seconds() < 60:
                is_ha_command = True

        changed_by = CHANGED_BY_HOME_ASSISTANT if is_ha_command else CHANGED_BY_EXTERNAL

        self._last_changed_by[entity_key_area] = changed_by
        self._last_changed_at[entity_key_area] = now
        self._last_changed_by[entity_key_main] = changed_by
        self._last_changed_at[entity_key_main] = now

        state_from = "armed" if previous_armed != 4 else "disarmed"
        state_to = "armed" if current_armed != 4 else "disarmed"

        _LOGGER.info(
            "Alarm state changed: %s -> %s (Area: %s, Device: %s, Source: %s)",
            state_from,
            state_to,
            area_name,
            device_name,
            changed_by,
        )

        self.hass.bus.async_fire(
            EVENT_STATE_CHANGED,
            {
                "device_id": device_id,
                "device_name": device_name,
                "area_id": area_id,
                "area_name": area_name,
                "previous_state": state_from,
                "new_state": state_to,
                "changed_by": changed_by,
                "changed_at": now.isoformat(),
            },
        )

    def register_ha_command(self, device_id: int, area_id: int | None = None) -> None:
        """Register that a command was sent from Home Assistant."""
        key = (device_id, area_id)
        self._pending_ha_commands[key] = dt_util.now()
        _LOGGER.debug("Registered HA command for device %s, area %s", device_id, area_id)

    def clear_main_panel_pending(self, device_id: int) -> None:
        """Clear the pending command for main panel after all areas processed."""
        key = (device_id, None)
        if key in self._pending_ha_commands:
            del self._pending_ha_commands[key]

    def get_last_changed_by(self, entity_key: str) -> str:
        """Get the last changed by value for an entity."""
        return self._last_changed_by.get(entity_key, CHANGED_BY_UNKNOWN)

    def get_last_changed_at(self, entity_key: str) -> datetime | None:
        """Get the last changed at timestamp for an entity."""
        return self._last_changed_at.get(entity_key)

    async def async_start_websocket(self) -> None:
        """Start the WebSocket client for real-time updates."""
        await self._ws_client.start()

    async def async_stop_websocket(self) -> None:
        """Stop the WebSocket client."""
        await self._ws_client.stop()

    def _on_websocket_update(self, event_data: dict[str, Any]) -> None:
        """Handle real-time updates from WebSocket."""
        if not isinstance(event_data, dict):
            _LOGGER.debug("WS event is not a dict, requesting poll for fresh state")
            self.hass.async_create_task(self.async_request_refresh())
            return

        if not self.data or "devices" not in self.data:
            return

        has_changes = False

        def find_device(dev_id: int) -> dict[str, Any] | None:
            for d in self.data.get("devices", []):
                if d.get("device_id") == dev_id:
                    return d
            return None

        for zone_update in event_data.get("ZoneList") or []:
            device_id = zone_update.get("Device_Id")
            zone_id = zone_update.get("ZoneId")
            if not device_id or zone_id is None:
                continue
            device = find_device(device_id)
            if device:
                for idx, zone in enumerate(device.get("zones", [])):
                    if zone.get("ZoneId") == zone_id:
                        device["zones"][idx].update(zone_update)
                        has_changes = True
                        break

        for area_update in event_data.get("AreaList") or []:
            device_id = area_update.get("Device_Id")
            area_id = area_update.get("AreaId")
            if not device_id or area_id is None:
                continue
            device = find_device(device_id)
            if device:
                for idx, area in enumerate(device.get("areas", [])):
                    if area.get("AreaId") == area_id:
                        device["areas"][idx].update(area_update)
                        has_changes = True
                        break

        if has_changes:
            _LOGGER.debug("Applying partial updates from WebSocket")
            self._check_alarm_triggered(self.data)
            self.async_set_updated_data(self.data)

    @staticmethod
    def _sia_zone_id_candidates(zone_id: int) -> list[int]:
        """Return possible API ZoneId candidates for a SIA zone id.

        The SIA parser already converts the reported SIA number to zero-based
        form before calling this method, so:
          BA15 -> zone_id 14

        Normal INIM zones usually map directly:
          BA15 -> ZoneId 14

        Some INIM double zones / second terminal channels can be represented
        differently between SIA and the API. This fallback supports both common
        cases seen on INIM Prime systems:

          SIA BA2015 -> parsed zone_id 2014 -> API ZoneId 1019
          SIA BA20   -> parsed zone_id 19   -> API ZoneId 1019

        Exact match is always tried first. Fallbacks are only used if exact
        ZoneId is not found.
        """
        candidates: list[int] = [zone_id]

        if zone_id >= 1000:
            # SIA 20xx Contact ID style.
            # Example: BA2015 -> parsed 2014 -> API ZoneId 1019.
            candidates.append(zone_id - 995)
        else:
            # Short Contact ID manually assigned to a double/high API zone.
            # Example: BA20 -> parsed 19 -> API ZoneId 1019.
            candidates.append(zone_id + 1000)

        # Preserve order and remove duplicates.
        return list(dict.fromkeys(candidates))

    @callback
    def async_on_sia_update(self, zone_id: int, status_update: dict[str, Any]) -> None:
        """Handle real-time zone updates from SIA-IP."""
        if not self.data or "devices" not in self.data:
            return

        candidate_zone_ids = self._sia_zone_id_candidates(zone_id)

        has_changes = False
        for device in self.data.get("devices", []):
            for candidate_zone_id in candidate_zone_ids:
                for idx, zone in enumerate(device.get("zones", [])):
                    if zone.get("ZoneId") == candidate_zone_id:
                        device["zones"][idx].update(status_update)
                        has_changes = True
                        _LOGGER.debug(
                            "SIA update zone %s: %s",
                            zone.get("Name", candidate_zone_id),
                            status_update,
                        )
                        if candidate_zone_id != zone_id:
                            _LOGGER.debug(
                                "SIA zone id %s mapped to API ZoneId %s",
                                zone_id,
                                candidate_zone_id,
                            )
                        break
                if has_changes:
                    break
            if has_changes:
                break

        if has_changes:
            self._check_alarm_triggered(self.data)
            self.async_set_updated_data(self.data)
        else:
            _LOGGER.debug(
                "SIA zone update ignored: no matching ZoneId found for %s "
                "(candidates: %s)",
                zone_id,
                candidate_zone_ids,
            )

    @callback
    def async_on_sia_area_update(
        self, area_id: int, status_update: dict[str, Any]
    ) -> None:
        """Handle real-time area updates from SIA-IP."""
        if not self.data or "devices" not in self.data:
            return

        has_changes = False
        for device in self.data.get("devices", []):
            for idx, area in enumerate(device.get("areas", [])):
                if area.get("AreaId") == area_id:
                    device["areas"][idx].update(status_update)
                    has_changes = True
                    _LOGGER.debug(
                        "SIA update area %s: %s", area.get("Name", area_id), status_update
                    )
                    break
            if has_changes:
                break

        if has_changes:
            self._check_alarm_triggered(self.data)
            self.async_set_updated_data(self.data)

    @property
    def devices(self) -> list[dict[str, Any]]:
        """Return all devices."""
        if not self.data:
            return []
        return self.data.get("devices", [])
