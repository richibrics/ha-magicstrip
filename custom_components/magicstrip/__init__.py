"""The ha-magicstrip integration."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakDBusError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed
from pymagicstrip import device_filter
from pymagicstrip import MagicStripDevice
from pymagicstrip import MagicStripState
from pymagicstrip.const import SERVICE_UUID
from pymagicstrip.errors import BleTimeoutError

from .const import DISPATCH_DETECTION
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.NUMBER]


@dataclass
class DeviceState:
    """Store state of a device."""

    device: MagicStripDevice
    coordinator: DataUpdateCoordinator[MagicStripState]
    light_device_info: DeviceInfo
    effect_speed_device_info: DeviceInfo
    light_extra_state_attributes: MutableMapping[str, Any]
    effect_speed_extra_state_attributes: MutableMapping[str, Any]


@dataclass
class EntryState:
    """Store state of config entry."""

    scanner: BleakScanner
    devices: dict[str, DeviceState]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ha-magicstrip from a config entry."""

    scanner = BleakScanner(filters={"UUIDs": [str(SERVICE_UUID)]})

    state = EntryState(scanner, {})
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = state

    # called when bleak finds a new device
    async def detection_callback(
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData,
    ) -> None:
        # if the found device is already registered in the state devices, update it
        if data := state.devices.get(ble_device.address):
            _LOGGER.debug(
                "Update: %s %s - %s", ble_device.name, ble_device, advertisement_data
            )

            await data.device.detection_callback(ble_device, advertisement_data)
            data.coordinator.async_set_updated_data(data.device.state)
        else:
            # device is not registered in the state devices, register it if it is a MagicStrip device
            if not device_filter(ble_device, advertisement_data):
                return  # not a MagicStrip device

            # New MagicStrip device found to register
            _LOGGER.debug(
                "Detected: %s %s - %s", ble_device.name, ble_device, advertisement_data
            )

            # Create the MagicStripDevice object for the new bluetooth device
            device = MagicStripDevice(ble_device)

            # Call its internal detection_callback to set the device state
            try:
                await device.detection_callback(ble_device, advertisement_data)
            except (BleTimeoutError, UpdateFailed) as exc:
                _LOGGER.error(
                    "Timed out when connecting to device. Will try again later. Error: %s",
                    exc,
                )
            except BleakDBusError as exc:
                _LOGGER.error(
                    "Error communicating with device. Is the device too far away from your Bluetooth controller?"
                )
                raise ConfigEntryNotReady from exc

            # define a method to ask to the device to update its state that we will pass to hass to call update on the device
            async def async_update_data():
                """Handle an explicit update request."""
                try:
                    _LOGGER.debug("Updating data...")
                    await device.update()
                    _LOGGER.debug("Updated data: %s", device.state)
                except (
                    asyncio.TimeoutError,
                    BleTimeoutError,
                    asyncio.exceptions.TimeoutError,
                ) as exc:
                    raise UpdateFailed(
                        f"Timeout communicating with device: {exc}"
                    ) from exc
                return device.state

            # register the device updater, passing the method to ask to the device to update its state
            coordinator: DataUpdateCoordinator[MagicStripState] = DataUpdateCoordinator(
                hass,
                logger=_LOGGER,
                name="MagicStrip Updater",
                update_interval=timedelta(seconds=120),
                update_method=async_update_data,
            )

            # register in the state new state of the device
            coordinator.async_set_updated_data(device.state)

            # register the device info
            light_device_info = DeviceInfo(
                identifiers={(DOMAIN, ble_device.address)},
                name=f"MagicStrip LED ({ble_device.address})",
            )

            # register the device info about the effect speed
            effect_speed_device_info = DeviceInfo(
                identifiers={(DOMAIN, ble_device.address)},
                name=f"MagicStrip LED Effect Speed ({ble_device.address})",
            )

            light_extra_state_attributes: MutableMapping[str, Any] = {
                "integration": DOMAIN,
                "signal_strength": device.state.connection_quality,
            }

            effect_speed_extra_state_attributes: MutableMapping[str, Any] = {
                "integration": DOMAIN
            }

            device_state = DeviceState(
                device=device,
                coordinator=coordinator,
                light_device_info=light_device_info,
                effect_speed_device_info=effect_speed_device_info,
                light_extra_state_attributes=light_extra_state_attributes,
                effect_speed_extra_state_attributes=effect_speed_extra_state_attributes,
            )
            state.devices[ble_device.address] = device_state
            async_dispatcher_send(
                hass, f"{DISPATCH_DETECTION}.{entry.entry_id}", device_state
            )

    scanner.register_detection_callback(detection_callback)
    await scanner.start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


@callback
def async_setup_entry_platform(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    constructor: Callable[[DeviceState], list[Entity]],
) -> None:
    """Set up a platform with added entities."""

    entry_state: EntryState = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        entity
        for device_state in entry_state.devices.values()
        for entity in constructor(device_state)
    )

    @callback
    def _detection(device_state: DeviceState) -> None:
        async_add_entities(constructor(device_state))

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DISPATCH_DETECTION}.{entry.entry_id}", _detection
        )
    )
