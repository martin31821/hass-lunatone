"""Tests for DALI frame encoding and decoding.

The frames are taken from a capture of the real installation.
"""

from __future__ import annotations

import pytest

from custom_components.lunatone_wlan.dali import (
    BROADCAST,
    BUTTON_EVENTS,
    EventScheme,
    InputEvent,
    OccupancyState,
    describe_frame,
    device_command,
    direct_arc_power,
    gear_command,
    go_to_scene,
    group_address,
    input_value,
    input_value_bytes,
    instance_command,
    level_command_target,
    occupancy_from_input_value,
    parse_input_event,
    parse_occupancy_info,
    scale_light_info,
    short_address,
)


@pytest.mark.parametrize(
    ("address_byte", "scene", "frame"),
    [
        (BROADCAST, 1, [255, 17]),
        (BROADCAST, 2, [255, 18]),
        (group_address(0), 1, [129, 17]),
        (group_address(6), 0, [141, 16]),
        (group_address(6), 2, [141, 18]),
        (short_address(3), 15, [7, 31]),
    ],
)
def test_go_to_scene(address_byte: int, scene: int, frame: list[int]) -> None:
    """Scene commands match what the Lunatone software sends."""
    assert go_to_scene(address_byte, scene) == frame


@pytest.mark.parametrize(
    "call",
    [
        lambda: short_address(64),
        lambda: group_address(16),
        lambda: go_to_scene(BROADCAST, 16),
        lambda: instance_command(0, 32, 0x80),
    ],
)
def test_out_of_range(call) -> None:
    """Values outside the DALI ranges are rejected."""
    with pytest.raises(ValueError):
        call()


def test_control_device_commands() -> None:
    """Device and instance commands address control devices."""
    assert device_command(3, 0x35) == [0x07, 0xFE, 0x35]
    assert instance_command(1, 2, 0x8B) == [0x03, 0x02, 0x8B]


@pytest.mark.parametrize(
    ("frame", "event"),
    [
        # Event scheme 0 (instance): A3 instance 0 before reconfiguration
        (
            [0x82, 0x80, 0x02],
            InputEvent(EventScheme.INSTANCE, 2, instance_type=1, instance_number=0),
        ),
        # Event scheme 2 (device/instance)
        (
            [0x06, 0x80, 0x02],
            InputEvent(
                EventScheme.DEVICE_INSTANCE, 2, short_address=3, instance_number=0
            ),
        ),
        (
            [0x04, 0x8C, 0x05],
            InputEvent(
                EventScheme.DEVICE_INSTANCE, 5, short_address=2, instance_number=3
            ),
        ),
        (
            [0x00, 0x8C, 0x05],
            InputEvent(
                EventScheme.DEVICE_INSTANCE, 5, short_address=0, instance_number=3
            ),
        ),
        (
            [0x02, 0x84, 0x02],
            InputEvent(
                EventScheme.DEVICE_INSTANCE, 2, short_address=1, instance_number=1
            ),
        ),
        # Event scheme 1 (device), 3 (device group), 4 (instance group)
        (
            [0x06, 0x04, 0x01],
            InputEvent(EventScheme.DEVICE, 1, short_address=3, instance_type=1),
        ),
        (
            [0x8A, 0x04, 0x09],
            InputEvent(EventScheme.DEVICE_GROUP, 9, device_group=5, instance_type=1),
        ),
        (
            [0xC4, 0x04, 0x0C],
            InputEvent(
                EventScheme.INSTANCE_GROUP, 12, instance_group=2, instance_type=1
            ),
        ),
        # 10 bit event information
        (
            [0x06, 0x83, 0xFF],
            InputEvent(
                EventScheme.DEVICE_INSTANCE, 0x3FF, short_address=3, instance_number=0
            ),
        ),
    ],
)
def test_parse_input_event(frame: list[int], event: InputEvent) -> None:
    """Event messages decode into their source and information."""
    assert parse_input_event(frame) == event


@pytest.mark.parametrize(
    "frame",
    [
        [0xFF, 0xFE, 0x1D],  # broadcast START QUIESCENT MODE
        [0xC1, 0x30, 0x5A],  # DTR0
        [0x07, 0x00, 0x86],  # QUERY INSTANCE ENABLED
        [0xC2, 0x80, 0x00],  # reserved: instance group by number
        [255, 17],
    ],
)
def test_parse_input_event_ignores_commands(frame: list[int]) -> None:
    """Commands are not mistaken for events."""
    assert parse_input_event(frame) is None


def test_button_event_codes() -> None:
    """The push button codes seen on the bus map to names."""
    assert [BUTTON_EVENTS[code] for code in (1, 0, 2, 5, 9, 11, 12)] == [
        "pressed",
        "released",
        "short_press",
        "double_press",
        "long_press_start",
        "long_press_repeat",
        "long_press_stop",
    ]


@pytest.mark.parametrize(
    ("bits", "data", "description"),
    [
        (
            16,
            [255, 17],
            {"frame": "command", "target": "broadcast", "opcode": 17, "scene": 1},
        ),
        (
            16,
            [141, 16],
            {
                "frame": "command",
                "target": "group",
                "group": 6,
                "opcode": 16,
                "scene": 0,
            },
        ),
        (
            16,
            [7, 5],
            {"frame": "command", "target": "address", "address": 3, "opcode": 5},
        ),
        (
            16,
            [6, 254],
            {"frame": "arc_power", "target": "address", "address": 3, "level": 254},
        ),
        (16, [0xFE, 0], {"frame": "arc_power", "target": "broadcast", "level": 0}),
        (16, [0xA5, 0], {"frame": "special_command"}),
        (24, [0xFF, 0xFE, 0x1D], {"frame": "device_command"}),
        (
            24,
            [0x06, 0x80, 0x02],
            {
                "frame": "event",
                "scheme": "device_instance",
                "info": 2,
                "short_address": 3,
                "instance_number": 0,
            },
        ),
        (8, [1], {"frame": "unknown"}),
    ],
)
def test_describe_frame(bits: int, data: list[int], description: dict) -> None:
    """Monitored frames get a readable description."""
    assert describe_frame(bits, data) == description


@pytest.mark.parametrize(
    ("info", "state"),
    [
        # Captured from a Lunatone CS Mini (movement sensor, bit 3 set)
        (0b1011, OccupancyState(occupied=True, movement=True)),
        (0b1010, OccupancyState(occupied=True, movement=False)),
        (0b1000, OccupancyState(occupied=False, movement=False)),
        (0b1111, OccupancyState(occupied=True, movement=True, repeat=True)),
        (0b0010, OccupancyState(occupied=True, movement=False)),
    ],
)
def test_parse_occupancy_info(info: int, state: OccupancyState) -> None:
    """Occupancy events decode into occupied, movement and repeat."""
    assert parse_occupancy_info(info) == state


def test_parse_occupancy_info_invalid() -> None:
    """Only the low 4 bits are used by occupancy events."""
    assert parse_occupancy_info(0x10) is None


@pytest.mark.parametrize(
    ("value", "state"),
    [
        (0xFF, OccupancyState(occupied=True, movement=True)),
        (0xAA, OccupancyState(occupied=True, movement=False)),
        (0x00, OccupancyState(occupied=False, movement=False)),
    ],
)
def test_occupancy_from_input_value(value: int, state: OccupancyState) -> None:
    """Input values captured from the CS Mini decode like events."""
    assert occupancy_from_input_value(value) == state


@pytest.mark.parametrize(
    ("info", "resolution", "value"),
    [
        (19, 11, 38),  # CS Mini: 10-bit event, 2 lx per step
        (1023, 11, 2046),
        (19, 10, 19),
        (100, 8, 25),
        (19, None, 19),
    ],
)
def test_scale_light_info(info: int, resolution: int | None, value: int) -> None:
    """Light events are scaled to the resolution of the input value."""
    assert scale_light_info(info, resolution) == value


@pytest.mark.parametrize(
    ("data", "resolution", "value"),
    [
        # QUERY INPUT VALUE and LATCH answers captured from the CS Mini
        ([5, 0], 11, 40),
        ([4, 224], 11, 39),
        ([26, 227], 11, 215),
        ([0, 32], 11, 1),
        ([0xFF], 2, 3),
    ],
)
def test_input_value(data: list[int], resolution: int, value: int) -> None:
    """Input value bytes are combined MSB first."""
    assert input_value(data, resolution) == value
    assert input_value_bytes(resolution) == len(data)


def test_gear_frames() -> None:
    """Direct arc power clears the selector bit, commands set it."""
    assert direct_arc_power(3, 128) == [6, 128]
    assert gear_command(3, 0x00) == [7, 0]
    with pytest.raises(ValueError):
        direct_arc_power(3, 255)


@pytest.mark.parametrize(
    ("frame", "target"),
    [
        ([0x81, 0x11], {"target": "group", "group": 0}),
        ([0x8D, 0x12], {"target": "group", "group": 6}),
        ([0xFF, 0x00], {"target": "broadcast"}),
        ([0xFE, 0x80], {"target": "broadcast"}),
        ([0x06, 0x80], {"target": "address", "address": 3}),
        ([0x07, 0x05], {"target": "address", "address": 3}),
        # Queries, configuration and special commands keep the level
        ([0x07, 0xA0], None),
        ([0x07, 0x2A], None),
        ([0xC1, 0x08], None),
        ([0xFD, 0x00], None),
        ([0x07, 0xFE, 0x35], None),
    ],
)
def test_level_command_target(frame: list[int], target: dict | None) -> None:
    """Only frames that change light levels have a target."""
    assert level_command_target(frame) == target
