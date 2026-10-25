"""Bus state for a Lunatone DALI WLAN gateway."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    ATTR_ADDRESS,
    ATTR_DATA,
    ATTR_GATEWAY,
    ATTR_INSTANCE,
    DOMAIN,
    EVENT_BUTTON,
    EVENT_MONITOR,
    MANUFACTURER,
)
from .dali import (
    ADDRESSED_SCHEMES,
    BUTTON_EVENTS,
    INSTANCE_TYPE_PUSH_BUTTON,
    MAX_INSTANCES,
    MAX_SHORT_ADDRESS,
    QUERY_EVENT_SCHEME,
    QUERY_INSTANCE_ENABLED,
    QUERY_INSTANCE_TYPE,
    QUERY_NUMBER_OF_INSTANCES,
    YES,
    EventScheme,
    InputEvent,
    describe_frame,
    device_command,
    instance_command,
    parse_input_event,
)
from .gateway import GatewayConnectionError, GatewayError, LunatoneGateway, MonitorFrame

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY = 1

type LunatoneConfigEntry = ConfigEntry[LunatoneHub]


@dataclass(slots=True)
class InputInstance:
    """An instance of a DALI-2 input device, e.g. one MC2 push button."""

    address: int
    number: int
    instance_type: int | None = None
    enabled: bool | None = None
    event_scheme: int | None = None

    @property
    def is_button(self) -> bool:
        """Return True for push buttons and instances of unknown type."""
        return self.instance_type in (None, INSTANCE_TYPE_PUSH_BUTTON)


@dataclass(slots=True)
class ControlDevice:
    """A DALI-2 control device on the bus."""

    address: int
    instances: dict[int, InputInstance] = field(default_factory=dict)


class LunatoneHub:
    """Tracks control devices and turns bus frames into Home Assistant events."""

    def __init__(
        self, hass: HomeAssistant, entry: LunatoneConfigEntry, gateway: LunatoneGateway
    ) -> None:
        """Initialize the hub."""
        self.hass = hass
        self.entry = entry
        self.gateway = gateway
        self.devices: dict[int, ControlDevice] = {}
        self.unique_id = entry.unique_id or entry.entry_id
        self.signal_new_instance = f"{DOMAIN}_{entry.entry_id}_new_instance"
        self.signal_connection = f"{DOMAIN}_{entry.entry_id}_connection"
        self.signal_updated = f"{DOMAIN}_{entry.entry_id}_updated"
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.initial_scan: asyncio.Task[Any] | None = None
        self._device_ids: dict[int, str] = {}
        self._unsubscribe: list[Any] = []

    def signal_input_event(self, address: int, instance: int) -> str:
        """Return the dispatcher signal for events of one instance."""
        return f"{DOMAIN}_{self.entry.entry_id}_event_{address}_{instance}"

    @property
    def has_devices(self) -> bool:
        """Return True once any control device is known."""
        return bool(self.devices)

    @property
    def gateway_device_info(self) -> DeviceInfo:
        """Return device info for the gateway."""
        info = self.gateway.info
        device = info.get("device", {})
        model = info.get("name") or "DALI WLAN"
        if article := device.get("articleNumber"):
            model = f"{model} ({article})"
        return DeviceInfo(
            identifiers={(DOMAIN, self.unique_id)},
            manufacturer=MANUFACTURER,
            model=model,
            name=info.get("name") or "Lunatone DALI WLAN",
            serial_number=self.gateway.serial,
            sw_version=info.get("version"),
            hw_version=device.get("pcb"),
            configuration_url=f"http://{self.gateway.host}/",
        )

    def control_device_info(self, address: int) -> DeviceInfo:
        """Return device info for a control device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._control_device_identifier(address))},
            manufacturer="DALI-2",
            model="Input device",
            name=f"DALI input device A{address}",
            via_device=(DOMAIN, self.unique_id),
        )

    def address_for_device(self, device: dr.DeviceEntry) -> int | None:
        """Return the short address of a control device registry entry."""
        prefix = f"{self.unique_id}_cd"
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier.startswith(prefix):
                return int(identifier.removeprefix(prefix))
        return None

    def instances(self) -> list[InputInstance]:
        """Return all known input instances."""
        return [
            instance
            for device in self.devices.values()
            for instance in device.instances.values()
        ]

    async def async_setup(self) -> None:
        """Restore known devices and start listening to the bus."""
        stored = await self._store.async_load() or {}
        for address_key, device_data in stored.get("devices", {}).items():
            device = ControlDevice(int(address_key))
            for number_key, instance_data in device_data.get("instances", {}).items():
                device.instances[int(number_key)] = InputInstance(**instance_data)
            self.devices[device.address] = device

        registry = dr.async_get(self.hass)
        registry.async_get_or_create(
            config_entry_id=self.entry.entry_id, **self.gateway_device_info
        )
        for address in self.devices:
            self._register_device(address)

        self._unsubscribe.append(self.gateway.add_monitor_listener(self._handle_frame))
        self._unsubscribe.append(
            self.gateway.add_connection_listener(self._handle_connection)
        )

    async def async_shutdown(self) -> None:
        """Stop listening and close the connection."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        await self.gateway.close()
        await self._store.async_save(self._data_to_save())

    @callback
    def async_remove_device(self, address: int) -> None:
        """Forget a control device."""
        self.devices.pop(address, None)
        self._device_ids.pop(address, None)
        self._schedule_save()

    async def async_scan(self) -> dict[str, Any]:
        """Query all short addresses for control devices and their instances."""
        _LOGGER.debug("Scanning DALI bus for control devices")
        found: dict[str, Any] = {}
        unaddressed: list[str] = []
        for address in range(MAX_SHORT_ADDRESS + 1):
            count = await self._query(
                device_command(address, QUERY_NUMBER_OF_INSTANCES)
            )
            if count is None:
                continue
            found[f"A{address}"] = instances = {}
            for number in range(min(count, MAX_INSTANCES)):
                instance_type = await self._query(
                    instance_command(address, number, QUERY_INSTANCE_TYPE)
                )
                enabled = (
                    await self._query(
                        instance_command(address, number, QUERY_INSTANCE_ENABLED)
                    )
                    == YES
                )
                scheme = await self._query(
                    instance_command(address, number, QUERY_EVENT_SCHEME)
                )
                self._ensure_instance(
                    address,
                    number,
                    instance_type=instance_type,
                    enabled=enabled,
                    event_scheme=scheme,
                )
                instances[str(number)] = {
                    "instance_type": instance_type,
                    "enabled": enabled,
                    "event_scheme": scheme,
                }
                if enabled and scheme not in ADDRESSED_SCHEMES:
                    unaddressed.append(f"A{address} instance {number}")
        self._schedule_save()
        async_dispatcher_send(self.hass, self.signal_updated)
        if unaddressed:
            _LOGGER.warning(
                "These inputs send events without their device address, so they"
                " can only be told apart if no other device uses the same"
                " instance number. Set their event scheme to device/instance: %s",
                ", ".join(unaddressed),
            )
        _LOGGER.debug("Scan found %s", found)
        return found

    async def async_initial_scan(self) -> None:
        """Scan the bus once after setup."""
        try:
            await self.async_scan()
        except GatewayError as err:
            _LOGGER.warning("Bus scan failed, press Scan bus to retry: %s", err)

    async def _query(self, frame: list[int]) -> int | None:
        try:
            return await self.gateway.send_frame(frame, wait_for_answer=True)
        except GatewayConnectionError:
            raise
        except GatewayError as err:
            _LOGGER.debug("Query %s failed: %s", frame, err)
            return None

    def _control_device_identifier(self, address: int) -> str:
        return f"{self.unique_id}_cd{address}"

    def _register_device(self, address: int) -> str:
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id, **self.control_device_info(address)
        )
        self._device_ids[address] = device.id
        return device.id

    def _ensure_instance(
        self, address: int, number: int, **config: Any
    ) -> InputInstance:
        device = self.devices.get(address)
        if device is None:
            device = self.devices[address] = ControlDevice(address)
            self._register_device(address)
        if (instance := device.instances.get(number)) is None:
            instance = device.instances[number] = InputInstance(
                address, number, **config
            )
            self._schedule_save()
            async_dispatcher_send(self.hass, self.signal_new_instance, instance)
        else:
            for key, value in config.items():
                setattr(instance, key, value)
        return instance

    def _resolve_instance(self, event: InputEvent) -> InputInstance | None:
        if event.short_address is not None and event.instance_number is not None:
            return self._ensure_instance(event.short_address, event.instance_number)
        if event.scheme in (EventScheme.DEVICE_GROUP, EventScheme.INSTANCE_GROUP):
            return None
        candidates = [
            instance
            for instance in self.instances()
            if (event.short_address is None or instance.address == event.short_address)
            and (
                event.instance_number is None
                or instance.number == event.instance_number
            )
            and (
                event.instance_type is None
                or instance.instance_type in (None, event.instance_type)
            )
            and instance.enabled is not False
        ]
        return candidates[0] if len(candidates) == 1 else None

    @callback
    def _handle_connection(self, connected: bool) -> None:
        async_dispatcher_send(self.hass, self.signal_connection, connected)

    @callback
    def _handle_frame(self, frame: MonitorFrame) -> None:
        self.hass.bus.async_fire(
            EVENT_MONITOR,
            {
                ATTR_GATEWAY: self.unique_id,
                "bits": frame.bits,
                ATTR_DATA: list(frame.data),
                "hex": "".join(f"{byte:02X}" for byte in frame.data),
                **describe_frame(frame.bits, frame.data),
            },
        )
        if frame.bits == 24 and (event := parse_input_event(frame.data)) is not None:
            self._handle_input_event(event)

    @callback
    def _handle_input_event(self, event: InputEvent) -> None:
        instance = self._resolve_instance(event)
        instance_type = event.instance_type
        if instance_type is None and instance is not None:
            instance_type = instance.instance_type
        if instance_type not in (None, INSTANCE_TYPE_PUSH_BUTTON):
            return
        if (button_event := BUTTON_EVENTS.get(event.info)) is None:
            _LOGGER.debug("Unknown push button event %s", event)
            return

        address = instance.address if instance else event.short_address
        number = instance.number if instance else event.instance_number
        self.hass.bus.async_fire(
            EVENT_BUTTON,
            {
                ATTR_GATEWAY: self.unique_id,
                "device_id": None if address is None else self._device_ids.get(address),
                ATTR_ADDRESS: address,
                ATTR_INSTANCE: number,
                "type": button_event,
                "scheme": event.scheme.name.lower(),
            },
        )
        if instance is not None:
            async_dispatcher_send(
                self.hass,
                self.signal_input_event(instance.address, instance.number),
                button_event,
            )

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {
            "devices": {
                str(address): {
                    "instances": {
                        str(number): asdict(instance)
                        for number, instance in device.instances.items()
                    }
                }
                for address, device in self.devices.items()
            }
        }
