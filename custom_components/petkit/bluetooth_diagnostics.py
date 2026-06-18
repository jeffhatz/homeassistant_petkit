"""Bluetooth advertisement diagnostics for Petkit devices."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from homeassistant.components import bluetooth
from homeassistant.core import CALLBACK_TYPE, callback

from .const import LOGGER

if TYPE_CHECKING:
    from home_assistant_bluetooth import BluetoothServiceInfoBleak

    from homeassistant.core import HomeAssistant

PETKIT_LOCAL_NAME_PREFIX = "Petkit_"
PETKIT_LOCAL_NAME_MATCHER = f"{PETKIT_LOCAL_NAME_PREFIX}*"
PETKIT_EVERSWEET_3_PRO_UVC_NAME = "Petkit_W4XUVC"


def _sorted_keys(data: dict[Any, Any]) -> list[Any]:
    """Return stable keys for diagnostic logging."""
    return sorted(data)


def _is_petkit_advertisement(service_info: BluetoothServiceInfoBleak) -> bool:
    """Return True if the advertisement local name looks like a Petkit device."""
    return (service_info.name or "").startswith(PETKIT_LOCAL_NAME_PREFIX)


def _log_petkit_advertisement(
    service_info: BluetoothServiceInfoBleak, path: str
) -> None:
    """Log the diagnostic details from a Petkit Bluetooth advertisement."""
    LOGGER.debug(
        "PETKIT HA Bluetooth advertisement discovery path (%s): matched "
        "address=%s local_name=%s rssi=%s source=%s connectable=%s "
        "service_uuids=%s manufacturer_data_keys=%s service_data_keys=%s",
        path,
        service_info.address,
        service_info.name,
        service_info.rssi,
        service_info.source,
        getattr(service_info, "connectable", None),
        service_info.service_uuids,
        _sorted_keys(service_info.manufacturer_data),
        _sorted_keys(service_info.service_data),
    )

    if service_info.name == PETKIT_EVERSWEET_3_PRO_UVC_NAME:
        LOGGER.debug(
            "PETKIT HA Bluetooth advertisement discovery path (%s): "
            "Eversweet 3 Pro UVC candidate visible as %s at %s",
            path,
            service_info.name,
            service_info.address,
        )


def _iter_petkit_service_info(
    service_infos: Iterable[BluetoothServiceInfoBleak],
) -> Iterable[BluetoothServiceInfoBleak]:
    """Yield only Petkit-looking advertisements."""
    for service_info in service_infos:
        if _is_petkit_advertisement(service_info):
            yield service_info


@callback
def async_log_petkit_bluetooth_cache(hass: HomeAssistant) -> None:
    """Log Petkit advertisements already known to Home Assistant Bluetooth."""
    bluetooth.async_get_scanner(hass)

    found = False
    seen: set[tuple[str, str, bool | None]] = set()

    for connectable in (True, False):
        service_infos = bluetooth.async_discovered_service_info(
            hass, connectable=connectable
        )
        for service_info in _iter_petkit_service_info(service_infos):
            dedupe_key = (
                service_info.address,
                service_info.source,
                getattr(service_info, "connectable", connectable),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            found = True
            _log_petkit_advertisement(service_info, "cache")

    if not found:
        LOGGER.debug(
            "PETKIT HA Bluetooth advertisement discovery path (cache): "
            "no Petkit_ advertisements currently known to Home Assistant Bluetooth"
        )


@callback
def async_register_petkit_bluetooth_diagnostics(
    hass: HomeAssistant,
) -> CALLBACK_TYPE:
    """Register passive diagnostics for Petkit Bluetooth advertisements."""
    bluetooth.async_get_scanner(hass)

    @callback
    def _async_bluetooth_advertisement(
        service_info: BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Log matching Petkit Bluetooth advertisements."""
        _log_petkit_advertisement(service_info, f"callback:{change.name.lower()}")

    LOGGER.debug(
        "PETKIT HA Bluetooth advertisement discovery path: registering passive "
        "diagnostics for local_name=%s",
        PETKIT_LOCAL_NAME_MATCHER,
    )

    return bluetooth.async_register_callback(
        hass,
        _async_bluetooth_advertisement,
        {"local_name": PETKIT_LOCAL_NAME_MATCHER, "connectable": False},
        bluetooth.BluetoothScanningMode.PASSIVE,
    )
