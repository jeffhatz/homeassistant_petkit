"""GATT diagnostics for Petkit Bluetooth devices."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth

from .bluetooth_diagnostics import (
    PETKIT_EVERSWEET_3_PRO_UVC_NAME,
    PETKIT_LOCAL_NAME_PREFIX,
)
from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.device import BLEDevice
    from home_assistant_bluetooth import BluetoothServiceInfoBleak

    from homeassistant.core import HomeAssistant

DATA_GATT_DIAGNOSTIC_LOCK = "gatt_diagnostic_lock"
DEFAULT_MAX_READ_BYTES = 128


def _normalize_address(address: str) -> str:
    """Normalize a Bluetooth address for comparisons."""
    return address.upper()


def _format_bytes(value: bytes, max_read_bytes: int) -> tuple[int, str, bool]:
    """Return length, hex string, and truncation state for diagnostic logging."""
    if max_read_bytes == 0:
        return len(value), "", bool(value)

    truncated = len(value) > max_read_bytes
    display_value = value[:max_read_bytes] if truncated else value
    return len(value), display_value.hex(" "), truncated


def _is_readable(characteristic: BleakGATTCharacteristic) -> bool:
    """Return True if the characteristic advertises read support."""
    return "read" in characteristic.properties


def _petkit_service_infos(
    hass: HomeAssistant, address: str | None, local_name: str | None
) -> list[BluetoothServiceInfoBleak]:
    """Find matching Petkit service infos from Home Assistant Bluetooth history."""
    if address:
        service_info = bluetooth.async_last_service_info(
            hass, _normalize_address(address), connectable=True
        )
        return [service_info] if service_info is not None else []

    matches = []
    for service_info in bluetooth.async_discovered_service_info(
        hass, connectable=True
    ):
        name = service_info.name or ""
        if local_name and name != local_name:
            continue
        if not local_name and name != PETKIT_EVERSWEET_3_PRO_UVC_NAME:
            continue
        if name.startswith(PETKIT_LOCAL_NAME_PREFIX):
            matches.append(service_info)

    if matches or local_name is not None:
        return matches

    return [
        service_info
        for service_info in bluetooth.async_discovered_service_info(
            hass, connectable=True
        )
        if (service_info.name or "").startswith(PETKIT_LOCAL_NAME_PREFIX)
    ]


async def async_inspect_petkit_gatt(
    hass: HomeAssistant,
    *,
    address: str | None = None,
    local_name: str | None = None,
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
) -> None:
    """Connect to a Petkit device and log GATT services and characteristics."""
    bluetooth.async_get_scanner(hass)

    hass.data.setdefault(DOMAIN, {})
    lock = hass.data[DOMAIN].setdefault(DATA_GATT_DIAGNOSTIC_LOCK, asyncio.Lock())

    async with lock:
        service_infos = _petkit_service_infos(hass, address, local_name)

        if not service_infos and address is None:
            LOGGER.warning(
                "PETKIT HA Bluetooth GATT diagnostics: no connectable Petkit "
                "advertisements found for local_name=%s",
                local_name or PETKIT_EVERSWEET_3_PRO_UVC_NAME,
            )
            return

        if address:
            addresses = [_normalize_address(address)]
        else:
            addresses = [info.address for info in service_infos]

        for device_address in addresses:
            await _async_inspect_address(
                hass,
                device_address,
                service_info=next(
                    (
                        info
                        for info in service_infos
                        if info.address == device_address
                    ),
                    None,
                ),
                max_read_bytes=max_read_bytes,
            )


async def _async_inspect_address(
    hass: HomeAssistant,
    address: str,
    *,
    service_info: BluetoothServiceInfoBleak | None,
    max_read_bytes: int,
) -> None:
    """Inspect one Bluetooth address."""
    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )

    if ble_device is None:
        reason = bluetooth.async_address_reachability_diagnostics(
            hass, address, bluetooth.BluetoothReachabilityIntent.CONNECTION
        )
        LOGGER.warning(
            "PETKIT HA Bluetooth GATT diagnostics: address=%s is not reachable "
            "for connection: %s",
            address,
            reason,
        )
        return

    name = (
        (service_info.name if service_info else None)
        or ble_device.name
        or address
    )
    source = service_info.source if service_info else None

    LOGGER.debug(
        "PETKIT HA Bluetooth GATT diagnostics: connecting address=%s "
        "local_name=%s source=%s",
        address,
        name,
        source,
    )

    client: BleakClient | None = None
    try:
        client = await _async_connect(ble_device, name)
        await _async_log_services(client, max_read_bytes)
    except Exception as err:
        LOGGER.warning(
            "PETKIT HA Bluetooth GATT diagnostics: inspection failed for "
            "address=%s local_name=%s: %s",
            address,
            name,
            err,
        )
    finally:
        if client is not None and client.is_connected:
            await client.disconnect()
            LOGGER.debug(
                "PETKIT HA Bluetooth GATT diagnostics: disconnected address=%s",
                address,
            )


async def _async_connect(ble_device: BLEDevice, name: str) -> BleakClient:
    """Connect using the Home Assistant Bluetooth-resolved BLEDevice."""
    return await establish_connection(
        BleakClient,
        ble_device,
        name,
        max_attempts=1,
    )


async def _async_log_services(client: BleakClient, max_read_bytes: int) -> None:
    """Log services, characteristics, and readable characteristic values."""
    services = client.services
    LOGGER.debug(
        "PETKIT HA Bluetooth GATT diagnostics: discovered %s services",
        len(services),
    )

    for service in services:
        LOGGER.debug(
            "PETKIT HA Bluetooth GATT diagnostics: service uuid=%s description=%s",
            service.uuid,
            service.description,
        )

        for characteristic in service.characteristics:
            LOGGER.debug(
                "PETKIT HA Bluetooth GATT diagnostics: characteristic uuid=%s "
                "handle=%s properties=%s description=%s",
                characteristic.uuid,
                characteristic.handle,
                characteristic.properties,
                characteristic.description,
            )

            if not _is_readable(characteristic):
                continue

            try:
                async with asyncio.timeout(10):
                    value = bytes(await client.read_gatt_char(characteristic))
            except Exception as err:
                LOGGER.debug(
                    "PETKIT HA Bluetooth GATT diagnostics: read failed uuid=%s: %s",
                    characteristic.uuid,
                    err,
                )
                continue

            length, value_hex, truncated = _format_bytes(value, max_read_bytes)
            LOGGER.debug(
                "PETKIT HA Bluetooth GATT diagnostics: read uuid=%s len=%s "
                "truncated=%s hex=%s",
                characteristic.uuid,
                length,
                truncated,
                value_hex,
            )
