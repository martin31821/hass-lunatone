"""Tests for the websocket client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import aiohttp
import pytest

from custom_components.lunatone_wlan import gateway as gateway_module
from custom_components.lunatone_wlan.gateway import (
    GatewayConnectionError,
    GatewayError,
    LunatoneGateway,
    MonitorFrame,
)

from .fake_gateway import FakeGateway


@pytest.fixture
async def session() -> AsyncGenerator[aiohttp.ClientSession]:
    """Return a client session."""
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
async def client(
    session: aiohttp.ClientSession, fake_gateway: FakeGateway
) -> AsyncGenerator[LunatoneGateway]:
    """Return a connected client with its read loop running."""
    client = LunatoneGateway(session, fake_gateway.host)
    await client.connect()
    task = asyncio.create_task(client.run())
    yield client
    await client.close()
    await task


async def _wait_for(condition, timeout: float = 2) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_connect_reads_info_and_sets_filter(
    session: aiohttp.ClientSession, fake_gateway: FakeGateway
) -> None:
    """Connecting reads the info message and enables monitoring."""
    client = LunatoneGateway(session, fake_gateway.host)
    await client.connect()
    assert client.connected
    assert client.serial == "123456789012"
    assert client.info["version"] == "1.1.2/1.0.10"
    await _wait_for(lambda: fake_gateway.received)
    assert fake_gateway.received[0] == {
        "type": "filtering",
        "data": {"ping": True, "group": True},
    }
    await client.close()


async def test_connect_fails(session: aiohttp.ClientSession) -> None:
    """Unreachable gateways raise a connection error."""
    client = LunatoneGateway(session, "127.0.0.1:1")
    with pytest.raises(GatewayConnectionError):
        await client.connect()


async def test_send_frame_message_format(
    client: LunatoneGateway, fake_gateway: FakeGateway
) -> None:
    """Frames are sent in the format of the Lunatone software."""
    assert await client.send_frame([255, 17]) is None
    assert fake_gateway.received[-1] == {
        "data": {
            "daliData": [255, 17],
            "line": 0,
            "mode": {"priority": 4, "sendTwice": False, "waitForAnswer": False},
            "numberOfBits": 16,
        },
        "type": "daliFrame",
    }


async def test_send_twice_waits_for_both_acks(
    client: LunatoneGateway, fake_gateway: FakeGateway
) -> None:
    """A send twice frame completes after the second ack."""
    frames: list[MonitorFrame] = []
    client.add_monitor_listener(frames.append)
    await client.send_frame([0x07, 0x00, 0x67], send_twice=True)
    assert [frame.data for frame in frames] == [(0x07, 0x00, 0x67)] * 2
    assert fake_gateway.received[-1]["data"]["numberOfBits"] == 24


async def test_query_answer(client: LunatoneGateway) -> None:
    """Queries return the backward frame, or None without answer."""
    assert await client.send_frame([0x07, 0xFE, 0x35], wait_for_answer=True) == 2
    assert await client.send_frame([0x07, 0x00, 0x8B], wait_for_answer=True) == 2
    assert await client.send_frame([0x7F, 0xFE, 0x35], wait_for_answer=True) is None


async def test_concurrent_requests_are_serialized(
    client: LunatoneGateway, fake_gateway: FakeGateway
) -> None:
    """Answers are matched to the right query when sending concurrently."""
    answers = await asyncio.gather(
        *(
            client.send_frame([(address << 1) | 1, 0xFE, 0x35], wait_for_answer=True)
            for address in range(6)
        )
    )
    assert answers == [4, 4, 4, 2, None, None]


async def test_rejected_frame(
    client: LunatoneGateway, fake_gateway: FakeGateway
) -> None:
    """A non-zero ack result raises."""
    fake_gateway.ack_result = 3
    with pytest.raises(GatewayError, match="result 3"):
        await client.send_frame([255, 17])


async def test_request_timeout(
    client: LunatoneGateway, fake_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ack times out."""
    monkeypatch.setattr(gateway_module, "REQUEST_TIMEOUT", 0.1)
    fake_gateway.respond = False
    with pytest.raises(GatewayError, match="did not confirm"):
        await client.send_frame([255, 17])


async def test_invalid_frame(client: LunatoneGateway) -> None:
    """Frames must have 2 or 3 bytes."""
    with pytest.raises(ValueError):
        await client.send_frame([1])
    with pytest.raises(ValueError):
        await client.send_frame([256, 0])


async def test_monitor_frames(
    client: LunatoneGateway, fake_gateway: FakeGateway
) -> None:
    """Bus frames reach the monitor listeners."""
    frames: list[MonitorFrame] = []
    unsubscribe = client.add_monitor_listener(frames.append)
    await fake_gateway.push_monitor([0x06, 0x80, 0x02])
    await _wait_for(lambda: frames)
    assert frames == [MonitorFrame(bits=24, data=(0x06, 0x80, 0x02), line=0)]
    unsubscribe()
    await fake_gateway.push_monitor([141, 16])
    await client.send_frame([255, 17])
    assert len(frames) == 1


async def test_listener_errors_are_contained(
    client: LunatoneGateway, fake_gateway: FakeGateway
) -> None:
    """A failing listener does not stop the read loop."""
    frames: list[MonitorFrame] = []

    def broken(frame: MonitorFrame) -> None:
        raise RuntimeError

    client.add_monitor_listener(broken)
    client.add_monitor_listener(frames.append)
    await fake_gateway.push_monitor([141, 16])
    await _wait_for(lambda: frames)
    assert client.connected


async def test_reconnect(
    client: LunatoneGateway, fake_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client reconnects after the gateway drops the connection."""
    states: list[bool] = []
    client.add_connection_listener(states.append)
    monkeypatch.setattr(gateway_module, "RECONNECT_DELAYS", (0.01,))
    await fake_gateway.disconnect_clients()
    await _wait_for(lambda: states == [False, True])
    assert await client.send_frame([0x07, 0xFE, 0x35], wait_for_answer=True) == 2


async def test_send_while_disconnected(session: aiohttp.ClientSession) -> None:
    """Sending without connection raises."""
    client = LunatoneGateway(session, "127.0.0.1:1")
    with pytest.raises(GatewayConnectionError):
        await client.send_frame([255, 17])


async def test_pending_request_fails_on_disconnect(
    client: LunatoneGateway, fake_gateway: FakeGateway
) -> None:
    """A request waiting for an ack fails when the connection drops."""
    fake_gateway.respond = False
    request = asyncio.create_task(client.send_frame([255, 17]))
    await _wait_for(lambda: fake_gateway.frames)
    await fake_gateway.disconnect_clients()
    with pytest.raises(GatewayConnectionError):
        await request


async def test_answers_gateway_ping(
    client: LunatoneGateway, fake_gateway: FakeGateway
) -> None:
    """Pings of the gateway are answered."""
    fake_gateway.answer_pings = False
    await fake_gateway.disconnect_clients()
    await _wait_for(lambda: fake_gateway.clients)
    await fake_gateway.ping_clients(b"5s")
    await _wait_for(lambda: fake_gateway.pongs)
    assert fake_gateway.pongs == [b"5s"]


async def test_idle_connection_stays_up(
    session: aiohttp.ClientSession,
    fake_gateway: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quiet gateway that answers pings keeps the connection."""
    monkeypatch.setattr(gateway_module, "IDLE_TIMEOUT", 0.05)
    client = LunatoneGateway(session, fake_gateway.host)
    states: list[bool] = []
    client.add_connection_listener(states.append)
    await client.connect()
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.4)
    assert states == [True]
    await client.close()
    await task


async def test_unresponsive_gateway_reconnects(
    session: aiohttp.ClientSession,
    fake_gateway: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection is dropped when the gateway stops answering pings."""
    monkeypatch.setattr(gateway_module, "IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(gateway_module, "RECONNECT_DELAYS", (0.01,))
    fake_gateway.answer_pings = False
    client = LunatoneGateway(session, fake_gateway.host)
    states: list[bool] = []
    client.add_connection_listener(states.append)
    await client.connect()
    task = asyncio.create_task(client.run())
    await _wait_for(lambda: states[:3] == [True, False, True])
    await client.close()
    await task
