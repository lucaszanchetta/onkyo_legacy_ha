from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.onkyo_legacy.const import DOMAIN
from tests_ha.tcp_simulator import ReceiverSimulator

pytestmark = pytest.mark.enable_socket


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    assert predicate(), "timed out waiting for condition"


@pytest.mark.asyncio
async def test_public_entry_recovery_entity_updates_and_unload_during_io(
    hass, enable_custom_integrations
) -> None:
    import custom_components.onkyo_legacy.coordinator as coordinator_module

    assert Path(coordinator_module.__file__).resolve().parents[2] == Path.cwd().resolve()
    async with ReceiverSimulator() as receiver:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Test Receiver",
            unique_id="127.0.0.1",
            data={
                "host": "127.0.0.1",
                "port": receiver.port,
                "model": "PR-SC5507",
                "scan_interval": 3600,
                "max_volume": 80,
                "retries": 1,
            },
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        runtime = hass.data[DOMAIN][entry.entry_id]
        client = runtime.coordinator.client
        client._COOLDOWN_BASE = 0.02
        client._COOLDOWN_MAX = 0.05
        client._retries = 1
        query_once = client._query_once
        client._query_once = lambda command, timeout=5.0: query_once(  # type: ignore[method-assign]
            command, timeout=min(timeout, 0.15)
        )

        await runtime.coordinator.async_refresh()
        assert runtime.coordinator.last_update_success
        assert runtime.coordinator.data.volume == 20
        media_player_id = "media_player.onkyo_pr_sc5507"
        await wait_until(
            lambda: (state := hass.states.get(media_player_id)) is not None
            and state.state == "on"
            and state.attributes.get("volume_level") == 20 / 80
        )

        receiver.volume = 30
        await runtime.coordinator.async_refresh()
        assert runtime.coordinator.data.volume == 30
        await wait_until(
            lambda: (state := hass.states.get(media_player_id)) is not None
            and state.attributes.get("volume_level") == 30 / 80
        )

        receiver.mode = "silent"
        receiver.silent_request.clear()
        unavailable = asyncio.create_task(runtime.coordinator.async_refresh())
        await asyncio.wait_for(receiver.silent_request.wait(), 1)
        await unavailable
        assert not runtime.coordinator.last_update_success
        await wait_until(
            lambda: (state := hass.states.get(media_player_id)) is not None
            and state.state == "unavailable"
        )
        metrics = runtime.coordinator.metrics_snapshot()
        assert metrics["core_poll_duration_ms"]["count"] >= 2
        assert metrics["matched_response_latency_ms"]["core"]["count"] >= 8
        assert metrics["connect_attempts"] >= 1
        assert metrics["last_valid_frame_age_seconds"] is not None
        assert metrics["last_matched_response_age_seconds"] is not None

        receiver.mode = "normal"
        await asyncio.sleep(0.06)
        recovery_started = time.perf_counter()
        await runtime.coordinator.async_refresh()
        recovery_elapsed_ms = (time.perf_counter() - recovery_started) * 1000
        assert runtime.coordinator.last_update_success
        assert runtime.coordinator.data.volume == 30
        await wait_until(
            lambda: (state := hass.states.get(media_player_id)) is not None
            and state.state == "on"
            and state.attributes.get("volume_level") == 30 / 80
        )
        metrics = runtime.coordinator.metrics_snapshot()
        if os.environ.get("ONKYO_REPORT_TCP_TIMING") == "1":
            print(
                "ONKYO_SIM_RESULT "
                + json.dumps(
                    {
                        "recovery_elapsed_ms": round(recovery_elapsed_ms, 3),
                        "wire_commands_through_recovery": len(receiver.commands),
                        "connect_attempts": metrics["connect_attempts"],
                        "queue_latency_ms": metrics["queue_latency_ms"],
                        "matched_response_latency_ms": metrics[
                            "matched_response_latency_ms"
                        ],
                        "core_poll_duration_ms": metrics["core_poll_duration_ms"],
                        "optional_poll_duration_ms": metrics[
                            "optional_poll_duration_ms"
                        ],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        previous_runtime = runtime
        previous_client = client
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        runtime = hass.data[DOMAIN][entry.entry_id]
        client = runtime.coordinator.client
        assert client is not previous_client
        assert previous_client._closed
        old_poll_task = previous_runtime.coordinator._poll_task
        assert old_poll_task is None or old_poll_task.done()

        receiver.mode = "hold"
        receiver.hold_release.clear()
        in_flight = asyncio.create_task(runtime.coordinator.async_refresh())
        await asyncio.wait_for(receiver.held_request.wait(), 1)
        assert await hass.config_entries.async_unload(entry.entry_id)
        receiver.hold_release.set()
        await asyncio.gather(in_flight, return_exceptions=True)
        await hass.async_block_till_done()
        assert entry.entry_id not in hass.data[DOMAIN]
        assert client._closed
        assert client._listener_thread is None
        assert client._device is None
        poll_task = runtime.coordinator._poll_task
        optional_task = runtime.coordinator._optional_task
        assert poll_task is None or poll_task.done()
        assert optional_task is None or optional_task.done()
        count_after_unload = len(receiver.commands)
        await asyncio.sleep(0.05)
        assert len(receiver.commands) == count_after_unload
        await wait_until(lambda: receiver.active_connections == 0)
