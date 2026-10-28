"""Bus state for a Lunatone DALI WLAN gateway."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    ATTR_ADDRESS,
    ATTR_DATA,
    ATTR_GATEWAY,
    ATTR_INSTANCE,
    DOMAIN,
    EVENT_BUTTON,
    EVENT_ILLUMINANCE,
    EVENT_MONITOR,
    EVENT_OCCUPANCY,
    MANUFACTURER,
)
from .dali import (
    ADDRESSED_SCHEMES,
    BUTTON_EVENTS,
    DEVICE_TYPE_MULTIPLE,
    DEVICE_TYPE_NAMES,
    DEVICE_TYPE_NONE,
    DEVICE_TYPE_SWITCHING,
    INSTANCE_TYPE_LIGHT,
    INSTANCE_TYPE_NAMES,
    INSTANCE_TYPE_OCCUPANCY,
    INSTANCE_TYPE_PUSH_BUTTON,
    MASK,
    MAX_ARC_LEVEL,
    MAX_GROUP,
    MAX_INSTANCES,
    MAX_SHORT_ADDRESS,
    MIN_ARC_LEVEL,
    QUERY_ACTUAL_LEVEL,
    QUERY_CONTROL_GEAR_PRESENT,
    QUERY_DEVICE_TYPE,
    QUERY_EVENT_SCHEME,
    QUERY_GROUPS_0_7,
    QUERY_GROUPS_8_15,
    QUERY_INPUT_VALUE,
    QUERY_INPUT_VALUE_LATCH,
    QUERY_INSTANCE_ENABLED,
    QUERY_INSTANCE_TYPE,
    QUERY_MAX_LEVEL,
    QUERY_MIN_LEVEL,
    QUERY_NEXT_DEVICE_TYPE,
    QUERY_NUMBER_OF_INSTANCES,
    QUERY_RESOLUTION,
    QUERY_STATUS,
    STATUS_FADE_RUNNING,
    STATUS_GEAR_FAILURE,
    STATUS_LAMP_FAILURE,
    YES,
    EventScheme,
    InputEvent,
    OccupancyState,
    describe_frame,
    device_command,
    gear_command,
    input_value,
    input_value_bytes,
    instance_command,
    level_command_target,
    occupancy_from_input_value,
    parse_input_event,
    parse_occupancy_info,
    scale_light_info,
)
from .gateway import GatewayConnectionError, GatewayError, LunatoneGateway, MonitorFrame

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY = 1

# Control gear levels are read again after commands on the bus, every
# FADE_POLL_INTERVAL while a fade runs, and every GEAR_POLL_INTERVAL to notice
# gear that was switched on or off at the mains.
GEAR_REFRESH_DELAY = 0.3
FADE_POLL_INTERVAL = 0.5
MAX_FADE_POLLS = 60
GEAR_POLL_INTERVAL = timedelta(seconds=60)
MAX_DEVICE_TYPES = 8

type LunatoneConfigEntry = ConfigEntry[LunatoneHub]


@dataclass(slots=True)
class InputInstance:
    """An instance of a DALI-2 input device, e.g. one MC2 push button."""

    address: int
    number: int
    instance_type: int | None = None
    enabled: bool | None = None
    event_scheme: int | None = None
    resolution: int | None = None

    @property
    def is_button(self) -> bool:
        """Return True for push buttons."""
        return self.instance_type == INSTANCE_TYPE_PUSH_BUTTON

    @property
    def is_occupancy(self) -> bool:
        """Return True for occupancy sensors."""
        return self.instance_type == INSTANCE_TYPE_OCCUPANCY

    @property
    def is_light(self) -> bool:
        """Return True for light sensors."""
        return self.instance_type == INSTANCE_TYPE_LIGHT


@dataclass(slots=True)
class ControlDevice:
    """A DALI-2 control device on the bus."""

    address: int
    instances: dict[int, InputInstance] = field(default_factory=dict)

    def count(self, instance_type: int | None) -> int:
        """Return how many instances of a type the device has."""
        return sum(
            1
            for instance in self.instances.values()
            if instance.instance_type == instance_type
        )

    @property
    def model(self) -> str:
        """Describe the device by its instance types."""
        names = sorted(
            {
                INSTANCE_TYPE_NAMES.get(
                    instance.instance_type, f"Type {instance.instance_type}"
                )
                for instance in self.instances.values()
                if instance.instance_type is not None
            }
        )
        return " + ".join(names) or "Input device"


@dataclass(slots=True)
class ControlGear:
    """A DALI control gear (ballast, LED driver, ...) on the bus."""

    address: int
    device_types: list[int] = field(default_factory=list)
    min_level: int = MIN_ARC_LEVEL
    max_level: int = MAX_ARC_LEVEL
    groups: int = 0

    @property
    def group_numbers(self) -> list[int]:
        """Return the groups the gear belongs to."""
        return [group for group in range(MAX_GROUP + 1) if self.groups & (1 << group)]

    def clamp(self, level: int) -> int:
        """Return the level the gear goes to for a requested level."""
        return level and min(max(level, self.min_level), self.max_level)

    @property
    def is_switch(self) -> bool:
        """Return True for gear that can only switch, e.g. relays."""
        return self.device_types == [DEVICE_TYPE_SWITCHING]

    @property
    def model(self) -> str:
        """Describe the gear by its device types."""
        names = [
            DEVICE_TYPE_NAMES.get(device_type, f"Device type {device_type}")
            for device_type in self.device_types
        ]
        return " + ".join(names) or "Control gear"


@dataclass(frozen=True, slots=True)
class GearState:
    """Level and status read from a control gear."""

    level: int | None
    status: int = 0

    @property
    def lamp_failure(self) -> bool:
        """Return True when the gear reports a lamp or gear failure."""
        return bool(self.status & (STATUS_LAMP_FAILURE | STATUS_GEAR_FAILURE))


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
        self.gear: dict[int, ControlGear] = {}
        # None when the gear did not answer the last query
        self.gear_states: dict[int, GearState | None] = {}
        self.unique_id = entry.unique_id or entry.entry_id
        self.signal_new_instance = f"{DOMAIN}_{entry.entry_id}_new_instance"
        self.signal_connection = f"{DOMAIN}_{entry.entry_id}_connection"
        self.signal_updated = f"{DOMAIN}_{entry.entry_id}_updated"
        self.signal_new_gear = f"{DOMAIN}_{entry.entry_id}_new_gear"
        self.signal_new_group = f"{DOMAIN}_{entry.entry_id}_new_group"
        self.signal_gear_changed = f"{DOMAIN}_{entry.entry_id}_gear_changed"
        self.groups: set[int] = set()
        self.values: dict[tuple[int, int], Any] = {}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.initial_scan: asyncio.Task[Any] | None = None
        self._device_ids: dict[int, str] = {}
        self._gateway_device_id: str | None = None
        self._identifying: set[tuple[int, int]] = set()
        self._unsubscribe: list[Any] = []
        self._pending_gear: set[int] = set()
        self._gear_refresh: asyncio.Task[None] | None = None
        self._gear_poll: asyncio.Task[None] | None = None
        self._last_levels: dict[int, int] = {}

    def signal_input_event(self, address: int, instance: int) -> str:
        """Return the dispatcher signal for events of one instance."""
        return f"{DOMAIN}_{self.entry.entry_id}_event_{address}_{instance}"

    def signal_gear(self, address: int) -> str:
        """Return the dispatcher signal for state changes of a control gear."""
        return f"{DOMAIN}_{self.entry.entry_id}_gear_{address}"

    def gear_unique_id(self, address: int) -> str:
        """Return the unique id of a control gear's light."""
        return f"{self.unique_id}_cg{address}"

    def group_unique_id(self, group: int) -> str:
        """Return the unique id of a group's light."""
        return f"{self.unique_id}_group{group}"

    def group_members(self, group: int) -> list[int]:
        """Return the addresses of the gear in a group."""
        bit = 1 << group
        return sorted(
            address for address, gear in self.gear.items() if gear.groups & bit
        )

    def last_level(self, address: int) -> int | None:
        """Return the last level a control gear was on at."""
        return self._last_levels.get(address)

    def instance_unique_id(self, instance: InputInstance) -> str:
        """Return the unique id prefix of an instance's entities."""
        return f"{self.unique_id}_cd{instance.address}_i{instance.number}"

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
        """Return device info linking entities to a registered control device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._control_device_identifier(address))}
        )

    def gear_device_info(self, address: int) -> DeviceInfo:
        """Return device info linking a light to a registered control gear."""
        return DeviceInfo(identifiers={(DOMAIN, self.gear_unique_id(address))})

    def group_device_info(self, group: int) -> DeviceInfo:
        """Return device info for a DALI group."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.group_unique_id(group))},
            manufacturer="DALI",
            model="Group",
            name=f"DALI group {group}",
            via_device=(DOMAIN, self.unique_id),
        )

    def address_for_device(self, device: dr.DeviceEntry) -> int | None:
        """Return the short address of a control device registry entry."""
        return self._address_for_device(device, f"{self.unique_id}_cd")

    def gear_address_for_device(self, device: dr.DeviceEntry) -> int | None:
        """Return the short address of a control gear registry entry."""
        return self._address_for_device(device, f"{self.unique_id}_cg")

    def _address_for_device(self, device: dr.DeviceEntry, prefix: str) -> int | None:
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
        for address_key, gear_data in stored.get("gear", {}).items():
            self.gear[int(address_key)] = ControlGear(**gear_data)

        registry = dr.async_get(self.hass)
        self._gateway_device_id = registry.async_get_or_create(
            config_entry_id=self.entry.entry_id, **self.gateway_device_info
        ).id
        for address in self.devices:
            self._register_device(address)
        for address in self.gear:
            self._register_gear(address)
        self._sync_groups()

        self._unsubscribe.append(self.gateway.add_monitor_listener(self._handle_frame))
        self._unsubscribe.append(
            self.gateway.add_connection_listener(self._handle_connection)
        )
        self._unsubscribe.append(
            async_track_time_interval(
                self.hass,
                self._poll_gear,
                GEAR_POLL_INTERVAL,
                name=f"{DOMAIN} poll control gear",
            )
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
        if device := self.devices.pop(address, None):
            for number in device.instances:
                self.values.pop((address, number), None)
        self._device_ids.pop(address, None)
        self._schedule_save()

    @callback
    def async_remove_gear(self, address: int) -> None:
        """Forget a control gear."""
        self.gear.pop(address, None)
        self.gear_states.pop(address, None)
        self._last_levels.pop(address, None)
        self._sync_groups()
        self._schedule_save()

    async def async_scan(self) -> dict[str, Any]:
        """Scan the bus for control devices and control gear."""
        devices = await self._scan_devices()
        gear = await self._scan_gear()
        await self.async_refresh_values()
        return {"devices": devices, "control_gear": gear}

    async def _scan_devices(self) -> dict[str, Any]:
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
            count = min(count, MAX_INSTANCES)
            configs = {
                number: await self._query_instance(address, number)
                for number in range(count)
            }
            found[f"A{address}"] = {
                str(number): config for number, config in configs.items()
            }
            unaddressed.extend(
                f"A{address} instance {number}"
                for number, config in configs.items()
                if config["enabled"] and config["event_scheme"] not in ADDRESSED_SCHEMES
            )
            self._remove_instances(address, keep=range(count))
            self._update_instances(address, configs)
            self._register_device(address)
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
        """Scan the bus after setup."""
        try:
            await self.async_scan()
        except GatewayError as err:
            _LOGGER.warning("Bus scan failed, press Scan bus to retry: %s", err)

    async def _scan_gear(self) -> dict[str, Any]:
        """Query all short addresses for control gear.

        Gear that does not answer is kept, it may be switched off at the mains.
        """
        _LOGGER.debug("Scanning DALI bus for control gear")
        found: dict[str, Any] = {}
        for address in range(MAX_SHORT_ADDRESS + 1):
            present = await self._query(
                gear_command(address, QUERY_CONTROL_GEAR_PRESENT)
            )
            if present != YES:
                if address in self.gear:
                    self._update_gear_state(address, None)
                continue
            config = await self._query_gear(address)
            found[f"A{address}"] = config
            gear = self.gear.get(address)
            if gear is None:
                gear = self.gear[address] = ControlGear(address, **config)
                self._register_gear(address)
                async_dispatcher_send(self.hass, self.signal_new_gear, gear)
            else:
                for key, value in config.items():
                    setattr(gear, key, value)
                self._register_gear(address)
        self._sync_groups()
        self._schedule_save()
        _LOGGER.debug("Gear scan found %s", found)
        return found

    async def _query_gear(self, address: int) -> dict[str, Any]:
        async def query(opcode: int) -> int | None:
            return await self._query(gear_command(address, opcode))

        device_types: list[int] = []
        device_type = await query(QUERY_DEVICE_TYPE)
        if device_type == DEVICE_TYPE_MULTIPLE:
            # DALI-2 gear lists its types one by one, ending with "none"
            for _ in range(MAX_DEVICE_TYPES):
                next_type = await query(QUERY_NEXT_DEVICE_TYPE)
                if next_type is None or next_type >= DEVICE_TYPE_NONE:
                    break
                device_types.append(next_type)
        elif device_type is not None:
            device_types.append(device_type)
        groups_low = await query(QUERY_GROUPS_0_7) or 0
        groups_high = await query(QUERY_GROUPS_8_15) or 0
        return {
            "device_types": device_types,
            "min_level": await query(QUERY_MIN_LEVEL) or MIN_ARC_LEVEL,
            "max_level": await query(QUERY_MAX_LEVEL) or MAX_ARC_LEVEL,
            "groups": (groups_high << 8) | groups_low,
        }

    async def _refresh_gear(self, address: int) -> bool:
        """Read the level of a control gear, return True while it is fading.

        Levels during a fade are skipped so the light does not jump around.
        """
        status = await self._query(gear_command(address, QUERY_STATUS))
        if status is None:
            self._update_gear_state(address, None)
            return False
        if status & STATUS_FADE_RUNNING:
            return True
        level = await self._query(gear_command(address, QUERY_ACTUAL_LEVEL))
        self._update_gear_state(
            address, GearState(None if level in (None, MASK) else level, status)
        )
        return False

    @callback
    def async_level_sent(self, addresses: list[int], level: int | None) -> None:
        """Show the level just sent to gear, then read the level they reached.

        A level of None stands for RECALL MAX LEVEL.
        """
        for address in addresses:
            if (gear := self.gear.get(address)) is None:
                continue
            state = self.gear_states.get(address)
            self._update_gear_state(
                address,
                GearState(
                    gear.max_level if level is None else gear.clamp(level),
                    state.status if state else 0,
                ),
            )
        self._schedule_gear_refresh(set(addresses) & set(self.gear))

    @callback
    def _sync_groups(self) -> None:
        """Add lights for groups that got gear, remove those of empty groups."""
        groups = {group for gear in self.gear.values() for group in gear.group_numbers}
        registry = dr.async_get(self.hass)
        for group in self.groups - groups:
            if device := registry.async_get_device(
                identifiers={(DOMAIN, self.group_unique_id(group))}
            ):
                registry.async_remove_device(device.id)
        new = groups - self.groups
        self.groups = groups
        for group in sorted(new):
            async_dispatcher_send(self.hass, self.signal_new_group, group)

    @callback
    def _update_gear_state(self, address: int, state: GearState | None) -> None:
        if state is not None and state.level:
            self._last_levels[address] = state.level
        if self.gear_states.get(address, ...) == state:
            return
        self.gear_states[address] = state
        async_dispatcher_send(self.hass, self.signal_gear(address))
        async_dispatcher_send(self.hass, self.signal_gear_changed, address)

    def _gear_for_target(self, target: dict[str, Any]) -> set[int]:
        if target["target"] == "broadcast":
            return set(self.gear)
        if target["target"] == "group":
            bit = 1 << target["group"]
            return {address for address, gear in self.gear.items() if gear.groups & bit}
        return {target["address"]} & set(self.gear)

    @callback
    def _schedule_gear_refresh(self, addresses: set[int]) -> None:
        if not addresses:
            return
        self._pending_gear |= addresses
        if self._gear_refresh is None or self._gear_refresh.done():
            self._gear_refresh = self.entry.async_create_background_task(
                self.hass, self._async_refresh_pending_gear(), f"{DOMAIN} gear levels"
            )

    async def _async_refresh_pending_gear(self) -> None:
        """Read the gear touched by bus commands until their fades are done."""
        polls: dict[int, int] = {}
        delay = GEAR_REFRESH_DELAY
        try:
            while self._pending_gear:
                await asyncio.sleep(delay)
                delay = FADE_POLL_INTERVAL
                addresses = sorted(self._pending_gear)
                self._pending_gear.clear()
                for address in addresses:
                    if address not in self.gear:
                        continue
                    if await self._refresh_gear(address):
                        polls[address] = polls.get(address, 0) + 1
                        if polls[address] < MAX_FADE_POLLS:
                            self._pending_gear.add(address)
        except GatewayError as err:
            self._pending_gear.clear()
            _LOGGER.debug("Reading control gear levels failed: %s", err)

    @callback
    def _poll_gear(self, now: Any = None) -> None:
        if not self.gateway.connected or (
            self._gear_poll is not None and not self._gear_poll.done()
        ):
            return
        self._gear_poll = self.entry.async_create_background_task(
            self.hass, self._async_poll_gear(), f"{DOMAIN} poll gear"
        )

    async def _async_poll_gear(self) -> None:
        try:
            await self.async_refresh_gear()
        except GatewayError as err:
            _LOGGER.debug("Polling control gear failed: %s", err)

    async def async_refresh_gear(self) -> None:
        """Read the level of all control gear."""
        fading = {
            address for address in list(self.gear) if await self._refresh_gear(address)
        }
        self._schedule_gear_refresh(fading)

    async def async_refresh_values(self) -> None:
        """Read the current value of all sensor instances and control gear."""
        await self.async_refresh_gear()
        for instance in self.instances():
            if instance.is_occupancy:
                value = await self._query(
                    instance_command(
                        instance.address, instance.number, QUERY_INPUT_VALUE
                    )
                )
                if value is not None:
                    self._update_value(instance, occupancy_from_input_value(value))
            elif instance.is_light and instance.resolution:
                if (lux := await self._query_input_value(instance)) is not None:
                    self._update_value(instance, lux)

    async def _query(self, frame: list[int]) -> int | None:
        try:
            return await self.gateway.send_frame(frame, wait_for_answer=True)
        except GatewayConnectionError:
            raise
        except GatewayError as err:
            _LOGGER.debug("Query %s failed: %s", frame, err)
            return None

    async def _query_instance(self, address: int, number: int) -> dict[str, Any]:
        async def query(opcode: int) -> int | None:
            return await self._query(instance_command(address, number, opcode))

        return {
            "instance_type": await query(QUERY_INSTANCE_TYPE),
            "enabled": await query(QUERY_INSTANCE_ENABLED) == YES,
            "event_scheme": await query(QUERY_EVENT_SCHEME),
            "resolution": await query(QUERY_RESOLUTION),
        }

    async def _query_input_value(self, instance: InputInstance) -> int | None:
        assert instance.resolution
        data: list[int] = []
        for index in range(input_value_bytes(instance.resolution)):
            opcode = QUERY_INPUT_VALUE if index == 0 else QUERY_INPUT_VALUE_LATCH
            byte = await self._query(
                instance_command(instance.address, instance.number, opcode)
            )
            if byte is None:
                return None
            data.append(byte)
        return input_value(data, instance.resolution)

    def _register_gear(self, address: int) -> str:
        registry = dr.async_get(self.hass)
        gear = self.gear.get(address)
        device_entry = registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.gear_unique_id(address))},
            manufacturer="DALI",
            model=gear.model if gear else "Control gear",
            name=f"DALI light A{address}",
        )
        if device_entry.via_device_id != self._gateway_device_id:
            registry.async_update_device(
                device_entry.id, via_device_id=self._gateway_device_id
            )
        return device_entry.id

    def _control_device_identifier(self, address: int) -> str:
        return f"{self.unique_id}_cd{address}"

    def _register_device(self, address: int) -> str:
        registry = dr.async_get(self.hass)
        device = self.devices.get(address)
        device_entry = registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self._control_device_identifier(address))},
            manufacturer="DALI-2",
            model=device.model if device else "Input device",
            name=f"DALI input device A{address}",
        )
        if device_entry.via_device_id != self._gateway_device_id:
            registry.async_update_device(
                device_entry.id, via_device_id=self._gateway_device_id
            )
        self._device_ids[address] = device_entry.id
        return device_entry.id

    def _ensure_instance(
        self, address: int, number: int, **config: Any
    ) -> InputInstance:
        """Return an instance, creating it if needed."""
        return self._update_instances(address, {number: config})[0]

    def _update_instances(
        self, address: int, configs: dict[int, dict[str, Any]]
    ) -> list[InputInstance]:
        """Create or update instances of a device, then their entities.

        Entities are only created once the instance type is known, and only
        after all instances are updated so names can count instances per type.
        When the type changes, e.g. after re-addressing the bus, the old
        entities are removed.
        """
        device = self.devices.get(address)
        if device is None:
            device = self.devices[address] = ControlDevice(address)
            self._register_device(address)
        instances: list[InputInstance] = []
        changed: list[InputInstance] = []
        for number, config in configs.items():
            instance = device.instances.get(number)
            if instance is None:
                instance = device.instances[number] = InputInstance(
                    address, number, **config
                )
                changed.append(instance)
            else:
                old_type = instance.instance_type
                for key, value in config.items():
                    setattr(instance, key, value)
                if instance.instance_type != old_type:
                    _LOGGER.info(
                        "A%s instance %s changed from type %s to %s",
                        address,
                        number,
                        old_type,
                        instance.instance_type,
                    )
                    self._remove_entities(instance)
                    self.values.pop((address, number), None)
                    changed.append(instance)
            instances.append(instance)
        if changed:
            self._schedule_save()
        for instance in changed:
            if instance.instance_type is not None:
                async_dispatcher_send(self.hass, self.signal_new_instance, instance)
        return instances

    def _remove_instances(self, address: int, keep: range) -> None:
        if (device := self.devices.get(address)) is None:
            return
        for number in [number for number in device.instances if number not in keep]:
            self._remove_entities(device.instances.pop(number))
            self.values.pop((address, number), None)

    def _remove_entities(self, instance: InputInstance) -> None:
        registry = er.async_get(self.hass)
        prefix = self.instance_unique_id(instance)
        for entity in er.async_entries_for_config_entry(registry, self.entry.entry_id):
            if entity.unique_id == prefix or entity.unique_id.startswith(f"{prefix}_"):
                registry.async_remove(entity.entity_id)

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
        if connected and self.initial_scan is not None and self.initial_scan.done():
            self.entry.async_create_background_task(
                self.hass, self._async_refresh_safely(), f"{DOMAIN} refresh values"
            )

    async def _async_refresh_safely(self) -> None:
        try:
            await self.async_refresh_values()
        except GatewayError as err:
            _LOGGER.debug("Reading sensor values failed: %s", err)

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
        elif frame.bits == 16 and (target := level_command_target(frame.data)):
            self._schedule_gear_refresh(self._gear_for_target(target))

    @callback
    def _handle_input_event(self, event: InputEvent) -> None:
        instance = self._resolve_instance(event)
        instance_type = event.instance_type
        if instance_type is None and instance is not None:
            instance_type = instance.instance_type
        if instance_type is None:
            if instance is not None:
                self._identify(instance, event)
            return

        address = instance.address if instance else event.short_address
        number = instance.number if instance else event.instance_number
        data: dict[str, Any] = {
            ATTR_GATEWAY: self.unique_id,
            "device_id": None if address is None else self._device_ids.get(address),
            ATTR_ADDRESS: address,
            ATTR_INSTANCE: number,
        }

        if instance_type == INSTANCE_TYPE_PUSH_BUTTON:
            if (button_event := BUTTON_EVENTS.get(event.info)) is None:
                _LOGGER.debug("Unknown push button event %s", event)
                return
            self.hass.bus.async_fire(
                EVENT_BUTTON,
                {**data, "type": button_event, "scheme": event.scheme.name.lower()},
            )
            if instance is not None:
                async_dispatcher_send(
                    self.hass,
                    self.signal_input_event(instance.address, instance.number),
                    button_event,
                )
        elif instance_type == INSTANCE_TYPE_OCCUPANCY:
            if (state := parse_occupancy_info(event.info)) is None:
                _LOGGER.debug("Unknown occupancy event %s", event)
                return
            self.hass.bus.async_fire(
                EVENT_OCCUPANCY,
                {
                    **data,
                    "occupied": state.occupied,
                    "movement": state.movement,
                    "repeat": state.repeat,
                },
            )
            if instance is not None:
                self._update_value(instance, state)
        elif instance_type == INSTANCE_TYPE_LIGHT:
            resolution = instance.resolution if instance else None
            illuminance = scale_light_info(event.info, resolution)
            self.hass.bus.async_fire(
                EVENT_ILLUMINANCE, {**data, "illuminance": illuminance}
            )
            if instance is not None:
                self._update_value(instance, illuminance)

    @callback
    def _update_value(
        self, instance: InputInstance, value: OccupancyState | int
    ) -> None:
        self.values[(instance.address, instance.number)] = value
        async_dispatcher_send(
            self.hass, self.signal_input_event(instance.address, instance.number), value
        )

    @callback
    def _identify(self, instance: InputInstance, event: InputEvent) -> None:
        """Query an unknown instance, then handle the event that revealed it."""
        key = (instance.address, instance.number)
        if key in self._identifying:
            return
        self._identifying.add(key)

        async def identify() -> None:
            try:
                config = await self._query_instance(instance.address, instance.number)
            except GatewayError as err:
                _LOGGER.debug("Identifying %s failed: %s", key, err)
                return
            finally:
                self._identifying.discard(key)
            self._ensure_instance(instance.address, instance.number, **config)
            self._register_device(instance.address)
            if instance.instance_type is not None:
                self._handle_input_event(event)

        self.entry.async_create_background_task(
            self.hass, identify(), f"{DOMAIN} identify A{key[0]} instance {key[1]}"
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
            },
            "gear": {str(address): asdict(gear) for address, gear in self.gear.items()},
        }
