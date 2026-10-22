from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from custom_components.onkyo_legacy.coordinator import (
    CommandResponseTimeout,
    OnkyoLegacyClient,
)
from tests_ha.tcp_simulator import ReceiverSimulator

pytestmark = pytest.mark.enable_socket


def test_real_test_imports_live_checkout() -> None:
    import custom_components.onkyo_legacy.coordinator as coordinator

    assert Path(coordinator.__file__).resolve().parents[2] == Path.cwd().resolve()


@pytest.mark.asyncio
async def test_fragmented_response_updates_latency_and_connection_metrics() -> None:
    async with ReceiverSimulator() as receiver:
        receiver.mode = "fragmented"
        client = OnkyoLegacyClient("127.0.0.1", receiver.port, retries=1)
        try:
            assert await asyncio.to_thread(client.query_optional, "PWR", 0.3) == "PWR01"
            metrics = client.metrics.snapshot()
            assert metrics["connect_attempts"] == 1
            assert metrics["reconnect_attempts"] == 0
            assert metrics["optional_timeouts"] == 0
            assert metrics["matched_response_latency_ms"]["optional"]["count"] == 1
            assert metrics["last_valid_frame_age_seconds"] is not None
            assert metrics["last_matched_response_age_seconds"] is not None
            assert set(metrics["matched_response_latency_ms"]) == {"core", "optional"}
        finally:
            await asyncio.to_thread(client.close)


@pytest.mark.asyncio
async def test_push_frame_age_does_not_mask_missing_matched_response() -> None:
    async with ReceiverSimulator() as receiver:
        receiver.mode = "push_before_response"
        client = OnkyoLegacyClient("127.0.0.1", receiver.port, retries=1)
        pushes: list[str] = []
        push_seen = threading.Event()
        client.start_listener(lambda message: (pushes.append(message), push_seen.set()))
        try:
            assert await asyncio.to_thread(client.query_optional, "PWR", 0.3) == "PWR01"
            assert await asyncio.to_thread(push_seen.wait, 0.2)
            assert pushes == ["MVL2A"]
            receiver.mode = "silent"
            receiver.commands.clear()
            with pytest.raises(CommandResponseTimeout):
                await asyncio.to_thread(client.query_optional, "PWR", 0.04)
            metrics = client.metrics.snapshot()
            assert metrics["optional_timeouts"] == 1
            assert metrics["last_valid_frame_age_seconds"] is not None
            assert metrics["last_matched_response_age_seconds"] is not None
            assert metrics["matched_response_latency_ms"]["optional"]["count"] == 1
        finally:
            await asyncio.to_thread(client.close)


@pytest.mark.asyncio
async def test_eof_mid_frame_reconnects_and_counts_reconnect_attempt() -> None:
    async with ReceiverSimulator() as receiver:
        receiver.eof_once = True
        client = OnkyoLegacyClient("127.0.0.1", receiver.port, retries=1)
        client._COOLDOWN_BASE = 0.001
        client._COOLDOWN_MAX = 0.01
        try:
            with pytest.raises(ConnectionError):
                await asyncio.to_thread(client.query_optional, "PWR", 0.1)
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and client.cooldown_remaining():
                await asyncio.sleep(0.002)
            assert await asyncio.to_thread(client.query_optional, "PWR", 0.2) == "PWR01"
            metrics = client.metrics.snapshot()
            assert metrics["connect_attempts"] == 2
            assert metrics["reconnect_attempts"] == 1
        finally:
            await asyncio.to_thread(client.close)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and receiver.active_connections:
            await asyncio.sleep(0.005)
        assert receiver.active_connections == 0
        assert client._listener_thread is None
        assert client._device is None


@pytest.mark.asyncio
async def test_optional_timeout_is_counted_without_retaining_samples() -> None:
    async with ReceiverSimulator() as receiver:
        receiver.mode = "silent"
        client = OnkyoLegacyClient("127.0.0.1", receiver.port, retries=1)
        try:
            with pytest.raises(CommandResponseTimeout):
                await asyncio.to_thread(client.query_optional, "PWR", 0.03)
            snapshot = client.metrics.snapshot()
            assert snapshot["optional_timeouts"] == 1
            assert snapshot["last_matched_response_age_seconds"] is None
            assert "samples" not in snapshot
        finally:
            await asyncio.to_thread(client.close)


@pytest.mark.asyncio
async def test_delayed_response_latency_is_measured_end_to_end() -> None:
    async with ReceiverSimulator() as receiver:
        receiver.mode = "delayed"
        client = OnkyoLegacyClient("127.0.0.1", receiver.port, retries=1)
        try:
            assert await asyncio.to_thread(client.query_optional, "PWR", 0.3) == "PWR01"
            summary = client.metrics.snapshot()["matched_response_latency_ms"]["optional"]
            assert summary["count"] == 1
            assert summary["last_ms"] >= 25
        finally:
            await asyncio.to_thread(client.close)

