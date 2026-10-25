"""DALI frame helpers (IEC 62386-102, -103, -301, -303 and -304)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

BROADCAST = 0xFF

# IEC 62386-102 control gear commands (16-bit forward frames)
CMD_GO_TO_SCENE = 0x10

# IEC 62386-103 device commands (24-bit forward frames, instance byte 0xFE)
INSTANCE_BYTE_DEVICE = 0xFE
QUERY_NUMBER_OF_INSTANCES = 0x35

# IEC 62386-103 instance commands
QUERY_INSTANCE_TYPE = 0x80
QUERY_RESOLUTION = 0x81
QUERY_INSTANCE_ENABLED = 0x86
QUERY_EVENT_SCHEME = 0x8B
QUERY_INPUT_VALUE = 0x8C
QUERY_INPUT_VALUE_LATCH = 0x8D

YES = 0xFF

INSTANCE_TYPE_PUSH_BUTTON = 1
INSTANCE_TYPE_OCCUPANCY = 3
INSTANCE_TYPE_LIGHT = 4

INSTANCE_TYPE_NAMES = {
    INSTANCE_TYPE_PUSH_BUTTON: "Push button",
    2: "Absolute input",
    INSTANCE_TYPE_OCCUPANCY: "Occupancy sensor",
    INSTANCE_TYPE_LIGHT: "Light sensor",
}

EVENT_INFO_BITS = 10

MAX_SHORT_ADDRESS = 63
MAX_GROUP = 15
MAX_SCENE = 15
MAX_INSTANCES = 32


class EventScheme(IntEnum):
    """Event addressing schemes of IEC 62386-103 input devices."""

    INSTANCE = 0
    DEVICE = 1
    DEVICE_INSTANCE = 2
    DEVICE_GROUP = 3
    INSTANCE_GROUP = 4


# Schemes whose events tell which device sent them
ADDRESSED_SCHEMES = (EventScheme.DEVICE, EventScheme.DEVICE_INSTANCE)

# IEC 62386-301 push button event information
BUTTON_EVENTS: dict[int, str] = {
    0x00: "released",
    0x01: "pressed",
    0x02: "short_press",
    0x05: "double_press",
    0x09: "long_press_start",
    0x0B: "long_press_repeat",
    0x0C: "long_press_stop",
    0x0E: "button_free",
    0x0F: "button_stuck",
}


@dataclass(frozen=True, slots=True)
class OccupancyState:
    """State reported by an IEC 62386-303 occupancy sensor."""

    occupied: bool
    movement: bool
    repeat: bool = False


def parse_occupancy_info(info: int) -> OccupancyState | None:
    """Decode occupancy event information.

    Bit 0 is movement, bit 1 occupied, bit 2 a repeated report and bit 3 the
    sensor type (movement or presence sensor).
    """
    if info & ~0x0F:
        return None
    return OccupancyState(
        occupied=bool(info & 0x02), movement=bool(info & 0x01), repeat=bool(info & 0x04)
    )


def occupancy_from_input_value(value: int) -> OccupancyState:
    """Decode the input value of an occupancy sensor.

    The two most significant bits are 00 vacant, 10 occupied and 11 movement.
    """
    return OccupancyState(occupied=bool(value & 0x80), movement=bool(value & 0x40))


def scale_light_info(info: int, resolution: int | None) -> int:
    """Return a 10-bit light event at the resolution of the input value."""
    if resolution is None or resolution == EVENT_INFO_BITS:
        return info
    if resolution > EVENT_INFO_BITS:
        return info << (resolution - EVENT_INFO_BITS)
    return info >> (EVENT_INFO_BITS - resolution)


def input_value(data: Sequence[int], resolution: int) -> int:
    """Combine the bytes of QUERY INPUT VALUE (LATCH), MSB first."""
    value = 0
    for byte in data:
        value = (value << 8) | byte
    return value >> (len(data) * 8 - resolution)


def input_value_bytes(resolution: int) -> int:
    """Return how many bytes an input value of this resolution has."""
    return max(1, (resolution + 7) // 8)


def short_address(address: int) -> int:
    """Return the address byte for a short address."""
    if not 0 <= address <= MAX_SHORT_ADDRESS:
        raise ValueError(f"Short address {address} out of range 0-{MAX_SHORT_ADDRESS}")
    return (address << 1) | 1


def group_address(group: int) -> int:
    """Return the address byte for a control gear group."""
    if not 0 <= group <= MAX_GROUP:
        raise ValueError(f"Group {group} out of range 0-{MAX_GROUP}")
    return 0x80 | (group << 1) | 1


def go_to_scene(address_byte: int, scene: int) -> list[int]:
    """Return a GO TO SCENE frame."""
    if not 0 <= scene <= MAX_SCENE:
        raise ValueError(f"Scene {scene} out of range 0-{MAX_SCENE}")
    return [address_byte, CMD_GO_TO_SCENE | scene]


def device_command(address: int, opcode: int) -> list[int]:
    """Return a 24-bit device command for a control device."""
    return [short_address(address), INSTANCE_BYTE_DEVICE, opcode]


def instance_command(address: int, instance: int, opcode: int) -> list[int]:
    """Return a 24-bit instance command for a control device."""
    if not 0 <= instance < MAX_INSTANCES:
        raise ValueError(f"Instance {instance} out of range 0-{MAX_INSTANCES - 1}")
    return [short_address(address), instance, opcode]


@dataclass(frozen=True, slots=True)
class InputEvent:
    """An event message sent by an input device instance."""

    scheme: EventScheme
    info: int
    short_address: int | None = None
    instance_number: int | None = None
    instance_type: int | None = None
    device_group: int | None = None
    instance_group: int | None = None


def parse_input_event(data: Sequence[int]) -> InputEvent | None:
    """Decode a 24-bit event message, or return None for other frames.

    Bit 16 is set in every 24-bit command, so a cleared bit 16 marks an event.
    Bit 15 tells whether bits 14-10 hold an instance number or an instance type.
    """
    if len(data) != 3:
        return None
    first, second, third = data
    if first & 0x01:
        return None
    info = ((second & 0x03) << 8) | third
    field = (second >> 2) & 0x1F
    by_number = bool(second & 0x80)

    if not first & 0x80:
        address = (first >> 1) & 0x3F
        if by_number:
            return InputEvent(
                EventScheme.DEVICE_INSTANCE,
                info,
                short_address=address,
                instance_number=field,
            )
        return InputEvent(
            EventScheme.DEVICE, info, short_address=address, instance_type=field
        )

    value = (first >> 1) & 0x1F
    if not first & 0x40:
        if by_number:
            return InputEvent(
                EventScheme.INSTANCE,
                info,
                instance_type=value,
                instance_number=field,
            )
        return InputEvent(
            EventScheme.DEVICE_GROUP, info, device_group=value, instance_type=field
        )
    if by_number:
        return None
    return InputEvent(
        EventScheme.INSTANCE_GROUP, info, instance_group=value, instance_type=field
    )


def _gear_target(address_byte: int) -> dict[str, Any] | None:
    """Describe the target of a 16-bit forward frame, None for special commands."""
    if address_byte in (0xFE, 0xFF):
        return {"target": "broadcast"}
    if address_byte in (0xFC, 0xFD):
        return {"target": "broadcast_unaddressed"}
    if address_byte < 0x80:
        return {"target": "address", "address": address_byte >> 1}
    if address_byte < 0xA0:
        return {"target": "group", "group": (address_byte >> 1) & 0x0F}
    return None


def describe_frame(bits: int, data: Sequence[int]) -> dict[str, Any]:
    """Return a JSON friendly description of a monitored frame."""
    if bits == 16 and len(data) == 2:
        address_byte, opcode = data
        target = _gear_target(address_byte)
        if target is None:
            return {"frame": "special_command"}
        if not address_byte & 0x01:
            return {"frame": "arc_power", **target, "level": opcode}
        description: dict[str, Any] = {"frame": "command", **target, "opcode": opcode}
        if CMD_GO_TO_SCENE <= opcode <= CMD_GO_TO_SCENE | MAX_SCENE:
            description["scene"] = opcode - CMD_GO_TO_SCENE
        return description
    if bits == 24 and len(data) == 3:
        if (event := parse_input_event(data)) is None:
            return {"frame": "device_command"}
        description = {
            "frame": "event",
            "scheme": event.scheme.name.lower(),
            "info": event.info,
        }
        for key in (
            "short_address",
            "instance_number",
            "instance_type",
            "device_group",
            "instance_group",
        ):
            if (value := getattr(event, key)) is not None:
                description[key] = value
        return description
    return {"frame": "unknown"}
