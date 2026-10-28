"""Lights for DALI control gear and groups."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.color import brightness_to_value, value_to_brightness

from .const import DOMAIN
from .dali import (
    CMD_OFF,
    CMD_RECALL_MAX_LEVEL,
    MAX_ARC_LEVEL,
    MIN_ARC_LEVEL,
    direct_arc_power,
    gear_command,
    group_address,
)
from .gateway import GatewayError
from .hub import ControlGear, LunatoneConfigEntry, LunatoneHub

# Arc levels are logarithmic, mapping them linearly to brightness gives
# steps that look even.
LEVEL_RANGE = (MIN_ARC_LEVEL, MAX_ARC_LEVEL)


def _brightness_to_level(brightness: int) -> int:
    return max(MIN_ARC_LEVEL, round(brightness_to_value(LEVEL_RANGE, brightness)))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LunatoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a light for every control gear and every group with gear."""
    hub = entry.runtime_data

    @callback
    def add_gear(gear: ControlGear) -> None:
        async_add_entities([LunatoneLight(hub, gear)])

    @callback
    def add_group(group: int) -> None:
        async_add_entities([LunatoneGroupLight(hub, group)])

    async_add_entities(LunatoneLight(hub, gear) for gear in hub.gear.values())
    async_add_entities(LunatoneGroupLight(hub, group) for group in sorted(hub.groups))
    entry.async_on_unload(async_dispatcher_connect(hass, hub.signal_new_gear, add_gear))
    entry.async_on_unload(
        async_dispatcher_connect(hass, hub.signal_new_group, add_group)
    )


class LunatoneBaseLight(LightEntity):
    """A light dimmed with direct arc power commands."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, hub: LunatoneHub) -> None:
        """Initialize the light."""
        self._hub = hub
        self._attr_available = hub.gateway.connected

    @property
    def _members(self) -> list[int]:
        """Return the addresses of the gear the light controls."""
        raise NotImplementedError

    async def _send(self, frame: list[int], level: int | None) -> None:
        """Send a level command, None for RECALL MAX LEVEL."""
        try:
            await self._hub.gateway.send_frame(frame)
        except GatewayError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="send_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self._hub.async_level_sent(self._members, level)

    async def async_added_to_hass(self) -> None:
        """Subscribe to the gateway connection."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal_connection, self._handle_connection
            )
        )

    @callback
    def _handle_connection(self, connected: bool) -> None:
        self._attr_available = connected
        self.async_write_ha_state()


class LunatoneLight(LunatoneBaseLight):
    """A DALI control gear."""

    def __init__(self, hub: LunatoneHub, gear: ControlGear) -> None:
        """Initialize the light."""
        super().__init__(hub)
        self._gear = gear
        self._attr_unique_id = hub.gear_unique_id(gear.address)
        self._attr_device_info = hub.gear_device_info(gear.address)
        mode = ColorMode.ONOFF if gear.is_switch else ColorMode.BRIGHTNESS
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    @property
    def available(self) -> bool:
        """Return False while the gateway or the gear does not answer.

        Gear that was not read yet is available with an unknown state.
        """
        states = self._hub.gear_states
        address = self._gear.address
        return self._attr_available and (
            address not in states or states[address] is not None
        )

    @property
    def _members(self) -> list[int]:
        return [self._gear.address]

    @property
    def _level(self) -> int | None:
        state = self._hub.gear_states.get(self._gear.address)
        return None if state is None else state.level

    @property
    def is_on(self) -> bool | None:
        """Return True while the gear is above level 0."""
        return None if (level := self._level) is None else level > 0

    @property
    def brightness(self) -> int | None:
        """Return the arc level as brightness."""
        if not (level := self._level):
            return None
        return value_to_brightness(LEVEL_RANGE, level)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return how the gear is configured on the bus."""
        state = self._hub.gear_states.get(self._gear.address)
        return {
            "address": self._gear.address,
            "groups": self._gear.group_numbers,
            "level": self._level,
            "lamp_failure": None if state is None else state.lamp_failure,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Dim to a brightness, or back to the last level."""
        address = self._gear.address
        if (brightness := kwargs.get(ATTR_BRIGHTNESS)) is not None:
            level: int | None = _brightness_to_level(brightness)
        else:
            level = self._hub.last_level(address)
        if level is None:
            await self._send(gear_command(address, CMD_RECALL_MAX_LEVEL), None)
            return
        level = self._gear.clamp(level)
        await self._send(direct_arc_power(address, level), level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the gear off."""
        await self._send(gear_command(self._gear.address, CMD_OFF), 0)

    async def async_added_to_hass(self) -> None:
        """Subscribe to level changes and the gateway connection."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._hub.signal_gear(self._gear.address),
                self.async_write_ha_state,
            )
        )


class LunatoneGroupLight(LunatoneBaseLight):
    """A DALI group, switched with one group command so all gear fades together.

    It is on while any member is on, its brightness is the mean of the
    members that are on.
    """

    def __init__(self, hub: LunatoneHub, group: int) -> None:
        """Initialize the light."""
        super().__init__(hub)
        self._group = group
        self._last_level: int | None = None
        self._attr_unique_id = hub.group_unique_id(group)
        self._attr_device_info = hub.group_device_info(group)
        self._attr_color_mode = ColorMode.BRIGHTNESS
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    @property
    def _members(self) -> list[int]:
        return self._hub.group_members(self._group)

    @property
    def _levels(self) -> list[int]:
        states = self._hub.gear_states
        return [
            state.level
            for address in self._members
            if (state := states.get(address)) is not None and state.level is not None
        ]

    @property
    def _level(self) -> int | None:
        if not (levels := self._levels):
            return None
        on = [level for level in levels if level]
        return round(sum(on) / len(on)) if on else 0

    @property
    def available(self) -> bool:
        """Return False when no member answers."""
        states = self._hub.gear_states
        return self._attr_available and any(
            address not in states or states[address] is not None
            for address in self._members
        )

    @property
    def is_on(self) -> bool | None:
        """Return True while any member is on."""
        return None if (level := self._level) is None else level > 0

    @property
    def brightness(self) -> int | None:
        """Return the mean level of the members that are on."""
        if not (level := self._level):
            return None
        return value_to_brightness(LEVEL_RANGE, level)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the group number and the member lights."""
        registry = er.async_get(self.hass)
        return {
            "group": self._group,
            "entity_id": [
                entity_id
                for address in self._members
                if (
                    entity_id := registry.async_get_entity_id(
                        "light", DOMAIN, self._hub.gear_unique_id(address)
                    )
                )
            ],
            "level": self._level,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Dim all members to a brightness, or back to the last level."""
        address_byte = group_address(self._group)
        if (brightness := kwargs.get(ATTR_BRIGHTNESS)) is not None:
            level: int | None = _brightness_to_level(brightness)
        else:
            level = self._last_level
        if level is None:
            await self._send([address_byte, CMD_RECALL_MAX_LEVEL], None)
            return
        await self._send([address_byte & 0xFE, level], level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch all members off."""
        await self._send([group_address(self._group), CMD_OFF], 0)

    async def async_added_to_hass(self) -> None:
        """Subscribe to level changes of the members."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal_gear_changed, self._handle_gear
            )
        )

    @callback
    def _handle_gear(self, address: int) -> None:
        if address not in self._members:
            return
        # Restored when turned on without brightness
        if level := self._level:
            self._last_level = level
        self.async_write_ha_state()
