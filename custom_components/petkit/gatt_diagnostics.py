"""GATT diagnostics for Petkit Bluetooth devices."""

from __future__ import annotations

import asyncio
from time import monotonic
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

DATA_GATT_DIAGNOSTIC_LOCKS = "gatt_diagnostic_locks"
DEFAULT_MAX_READ_BYTES = 128
DEFAULT_LISTEN_DURATION = 60
MAX_LISTEN_DURATION = 300

PETKIT_AAA1_CHARACTERISTIC_UUID = "0000aaa1-0000-1000-8000-00805f9b34fb"


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


def _async_get_address_lock(hass: HomeAssistant, address: str) -> asyncio.Lock:
    """Return the diagnostic lock for a Bluetooth address."""
    hass.data.setdefault(DOMAIN, {})
    locks = hass.data[DOMAIN].setdefault(DATA_GATT_DIAGNOSTIC_LOCKS, {})
    return locks.setdefault(_normalize_address(address), asyncio.Lock())


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
        async with _async_get_address_lock(hass, device_address):
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


async def async_listen_petkit_notifications(
    hass: HomeAssistant,
    *,
    address: str,
    duration: int = DEFAULT_LISTEN_DURATION,
) -> None:
    """Connect to a Petkit device and log AAA1 notifications."""
    bluetooth.async_get_scanner(hass)

    device_address = _normalize_address(address)
    lock = _async_get_address_lock(hass, device_address)

    await lock.acquire()
    try:
        service_info = bluetooth.async_last_service_info(
            hass, device_address, connectable=True
        )
        await _async_listen_address(
            hass,
            device_address,
            service_info=service_info,
            duration=duration,
        )
    finally:
        lock.release()
        LOGGER.debug(
            "PETKIT HA Bluetooth notification diagnostics: released "
            "listener lock address=%s",
            device_address,
        )


async def _async_inspect_address(
    hass: HomeAssistant,
    address: str,
    *,
    service_info: BluetoothServiceInfoBleak | None,
    max_read_bytes: int,
) -> None:
    """Inspect one Bluetooth address."""
    client: BleakClient | None = None
    try:
        client = await _async_connect_address(hass, address, service_info)
        if client is None:
            return
        await _async_log_services(client, max_read_bytes)
    except Exception as err:
        LOGGER.warning(
            "PETKIT HA Bluetooth GATT diagnostics: inspection failed for "
            "address=%s: %s",
            address,
            err,
        )
    finally:
        await _async_disconnect(client, address)


async def _async_listen_address(
    hass: HomeAssistant,
    address: str,
    *,
    service_info: BluetoothServiceInfoBleak | None,
    duration: int,
) -> None:
    """Listen for AAA1 notifications from one Bluetooth address."""
    client: BleakClient | None = None
    notify_started = False
    listener_started_at = monotonic()
    subscription_started_at = listener_started_at
    notification_count = 0

    def notification_handler(sender, data: bytearray) -> None:
        nonlocal notification_count

        notification_count += 1
        elapsed = monotonic() - subscription_started_at
        sender_uuid = getattr(sender, "uuid", sender)
        payload = bytes(data)
        LOGGER.debug(
            "PETKIT HA Bluetooth notification diagnostics: notification "
            "elapsed=%.3fs sender=%s len=%s hex=%s",
            elapsed,
            sender_uuid,
            len(payload),
            payload.hex(" "),
        )

    try:
        client = await _async_connect_address(hass, address, service_info)
        if client is None:
            return

        async with asyncio.timeout(10):
            initial_value = bytes(
                await client.read_gatt_char(PETKIT_AAA1_CHARACTERISTIC_UUID)
            )
        LOGGER.debug(
            "PETKIT HA Bluetooth notification diagnostics: initial read "
            "uuid=%s len=%s hex=%s",
            PETKIT_AAA1_CHARACTERISTIC_UUID,
            len(initial_value),
            initial_value.hex(" "),
        )

        LOGGER.debug(
            "PETKIT HA Bluetooth notification diagnostics: before "
            "start_notify address=%s uuid=%s",
            address,
            PETKIT_AAA1_CHARACTERISTIC_UUID,
        )
        subscription_started_at = monotonic()
        await client.start_notify(
            PETKIT_AAA1_CHARACTERISTIC_UUID,
            notification_handler,
        )
        notify_started = True
        LOGGER.debug(
            "PETKIT HA Bluetooth notification diagnostics: start_notify "
            "succeeded address=%s uuid=%s",
            address,
            PETKIT_AAA1_CHARACTERISTIC_UUID,
        )
        LOGGER.debug(
            "PETKIT HA Bluetooth notification diagnostics: entering timed "
            "wait address=%s uuid=%s duration=%ss",
            address,
            PETKIT_AAA1_CHARACTERISTIC_UUID,
            duration,
        )
        await asyncio.sleep(duration)
        LOGGER.debug(
            "PETKIT HA Bluetooth notification diagnostics: timed wait "
            "finished address=%s uuid=%s duration=%ss",
            address,
            PETKIT_AAA1_CHARACTERISTIC_UUID,
            duration,
        )
    except asyncio.CancelledError:
        LOGGER.debug(
            "PETKIT HA Bluetooth notification diagnostics: listener "
            "cancelled address=%s uuid=%s",
            address,
            PETKIT_AAA1_CHARACTERISTIC_UUID,
        )
        raise
    except Exception as err:
        LOGGER.warning(
            "PETKIT HA Bluetooth notification diagnostics: listener failed "
            "for address=%s uuid=%s: %s",
            address,
            PETKIT_AAA1_CHARACTERISTIC_UUID,
            err,
        )
    finally:
        try:
            if notify_started and client is not None and client.is_connected:
                LOGGER.debug(
                    "PETKIT HA Bluetooth notification diagnostics: before "
                    "stop_notify address=%s uuid=%s",
                    address,
                    PETKIT_AAA1_CHARACTERISTIC_UUID,
                )
                try:
                    async with asyncio.timeout(10):
                        await client.stop_notify(PETKIT_AAA1_CHARACTERISTIC_UUID)
                    LOGGER.debug(
                        "PETKIT HA Bluetooth notification diagnostics: after "
                        "stop_notify address=%s uuid=%s",
                        address,
                        PETKIT_AAA1_CHARACTERISTIC_UUID,
                    )
                except TimeoutError:
                    LOGGER.warning(
                        "PETKIT HA Bluetooth notification diagnostics: "
                        "stop_notify timed out address=%s uuid=%s",
                        address,
                        PETKIT_AAA1_CHARACTERISTIC_UUID,
                    )
                except Exception as err:
                    LOGGER.debug(
                        "PETKIT HA Bluetooth notification diagnostics: "
                        "stop_notify failed address=%s uuid=%s: %s",
                        address,
                        PETKIT_AAA1_CHARACTERISTIC_UUID,
                        err,
                    )
            LOGGER.debug(
                "PETKIT HA Bluetooth notification diagnostics: before "
                "disconnect address=%s",
                address,
            )
            await _async_disconnect(client, address)
            LOGGER.debug(
                "PETKIT HA Bluetooth notification diagnostics: after "
                "disconnect address=%s",
                address,
            )
        finally:
            elapsed = monotonic() - listener_started_at
            LOGGER.debug(
                "PETKIT HA Bluetooth notification diagnostics: listener "
                "complete address=%s uuid=%s notification_count=%s "
                "elapsed=%.3fs",
                address,
                PETKIT_AAA1_CHARACTERISTIC_UUID,
                notification_count,
                elapsed,
            )


async def _async_connect_address(
    hass: HomeAssistant,
    address: str,
    service_info: BluetoothServiceInfoBleak | None,
) -> BleakClient | None:
    """Resolve and connect to one Bluetooth address."""
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
        return None

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
    client = await _async_connect(ble_device, name)
    return client


async def _async_disconnect(client: BleakClient | None, address: str) -> None:
    """Disconnect a diagnostic Bluetooth client."""
    LOGGER.debug(
        "PETKIT HA Bluetooth GATT diagnostics: before disconnect address=%s",
        address,
    )

    if client is None:
        LOGGER.debug(
            "PETKIT HA Bluetooth GATT diagnostics: after disconnect "
            "address=%s skipped=client_none",
            address,
        )
        return

    if not client.is_connected:
        LOGGER.debug(
            "PETKIT HA Bluetooth GATT diagnostics: after disconnect "
            "address=%s skipped=already_disconnected",
            address,
        )
        return

    try:
        async with asyncio.timeout(10):
            await client.disconnect()
        LOGGER.debug(
            "PETKIT HA Bluetooth GATT diagnostics: after disconnect "
            "address=%s",
            address,
        )
    except TimeoutError:
        LOGGER.warning(
            "PETKIT HA Bluetooth GATT diagnostics: disconnect timed out "
            "address=%s",
            address,
        )
    except Exception as err:
        LOGGER.warning(
            "PETKIT HA Bluetooth GATT diagnostics: disconnect failed "
            "address=%s: %s",
            address,
            err,
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
        len(services.services),
    )

    for service in services.services.values():
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
