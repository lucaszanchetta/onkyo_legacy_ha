from __future__ import annotations

import asyncio
import socket
import struct
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from tests.helpers import FakeHass, fresh_import


class FakeClient:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.sent: list[tuple[str, str]] = []
        self.queried: list[str] = []

    def query(self, command: str) -> str:
        self.queried.append(command)
        response = self.responses.get(command)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise ValueError(f"missing response for {command}")
        return response

    def query_optional(self, command: str, timeout: float = 1.5) -> str:
        return self.query(command)

    def query_batch(self, commands: tuple[str, ...]) -> dict[str, str]:
        self.queried.extend(commands)
        results: dict[str, str] = {}
        for command in commands:
            response = self.responses.get(command)
            if isinstance(response, Exception):
                continue
            if response is not None:
                results[command] = response
        return results

    def send(self, command: str, value: str) -> None:
        self.sent.append((command, value))

    def disconnect(self) -> None:
        return None


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = fresh_import("custom_components.onkyo_legacy.coordinator")
        self.hass = FakeHass()

    @staticmethod
    def demand(coordinator, *commands: str) -> None:
        coordinator.async_add_listener(lambda: None, frozenset(commands))

    async def test_build_runtime_data_accepts_secondary_source_aliases(self) -> None:
        runtime = self.module.build_runtime_data(
            self.hass,
            host="192.168.1.23",
            port=60128,
            name="Onkyo PR-SC5507",
            model="PR-SC5507",
            scan_interval=10,
            sources={"HTPC": "vcr", "Blu-ray": "dvd"},
            max_volume=80,
        )

        self.assertEqual(runtime.sources["VIDEO1"], "video1")
        self.assertEqual(runtime.sources["DVD -- BD/DVD"], "dvd")
        self.assertEqual(runtime.model, "PR-SC5507")
        self.assertEqual(runtime.device_info["identifiers"], {("onkyo_legacy", "192.168.1.23")})
        self.assertIn("auto", runtime.supported_audio_selectors)
        self.assertIn("off", runtime.supported_late_night_modes)
        self.assertIn("medium", runtime.supported_audyssey_volume_modes)
        self.assertIn("plii", runtime.supported_listening_modes)
        self.assertNotIn("query", runtime.supported_listening_modes)
        self.assertEqual(runtime.supported_dimmer_modes, ["bright", "dim", "dark"])
        self.assertEqual(tuple(zone.zone_key for zone in runtime.zones), ("main",))
        self.assertEqual(tuple(zone.zone_key for zone in runtime.candidate_zones), ("zone2", "zone3"))
        self.assertEqual(runtime.queryable_commands, (
            "LMD",
            "DIM",
            "SLA",
            "LTN",
            "RAS",
            "ADQ",
            "ADV",
            "MOT",
            "TGA",
            "TGB",
            "TGC",
            "SLP",
            "CTL",
            "SWL",
            "IFA",
            "IFV",
            "RES",
            "HDO",
        ))

    async def test_operation_gate_prioritizes_controls_and_bounds_background_wait(self) -> None:
        gate = self.module._OperationGate()
        gate.acquire(False)
        order: list[str] = []
        threads: list[threading.Thread] = []

        def queued(priority: int, name: str) -> None:
            gate.acquire(priority)
            order.append(name)
            gate.release()

        for index in range(5):
            thread = threading.Thread(target=queued, args=(2, f"control-{index}"))
            thread.start()
            threads.append(thread)
        core_poll = threading.Thread(target=queued, args=(1, "core"))
        core_poll.start()
        threads.append(core_poll)
        background = threading.Thread(target=queued, args=(0, "background"))
        background.start()
        threads.append(background)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with gate._condition:
                if len(gate._waiters) == 7:
                    break
            time.sleep(0.001)
        gate.release()
        for thread in threads:
            thread.join(timeout=1)
        self.assertEqual(order.index("core"), 4)
        self.assertEqual(order.index("background"), 6)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    async def test_client_metrics_use_fixed_latency_summaries(self) -> None:
        metrics = self.module.OnkyoLegacyClient("receiver", 60128).metrics
        workers = [
            threading.Thread(
                target=lambda: [
                    metrics.queue_latency["control"].record(2.5)
                    for _ in range(50)
                ]
            )
            for _ in range(4)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        summary = metrics.snapshot()["queue_latency_ms"]["control"]
        self.assertEqual(summary["count"], 200)
        self.assertEqual(summary["total_ms"], 500.0)
        self.assertEqual(summary["max_ms"], 2.5)
        self.assertEqual(summary["last_ms"], 2.5)
        self.assertNotIn("samples", summary)

    async def test_operation_gate_serves_background_during_sustained_control_and_core_load(self) -> None:
        gate = self.module._OperationGate()
        gate.acquire(0)
        order: list[str] = []
        threads: list[threading.Thread] = []

        def queued(priority: int, name: str) -> None:
            gate.acquire(priority)
            order.append(name)
            gate.release()

        for index in range(10):
            for priority, kind in ((2, "control"), (1, "core")):
                thread = threading.Thread(target=queued, args=(priority, f"{kind}-{index}"))
                thread.start()
                threads.append(thread)
        background = threading.Thread(target=queued, args=(0, "background"))
        background.start()
        threads.append(background)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with gate._condition:
                if len(gate._waiters) == 21:
                    break
            time.sleep(0.001)
        gate.release()
        for thread in threads:
            thread.join(timeout=1)
        self.assertLess(order.index("background"), 15)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    async def test_optional_refresh_cancellation_prevents_late_publish(self) -> None:
        client = FakeClient({
            "PWR": "PWR01", "MVL": "MVL20", "AMT": "AMT00", "SLI": "SLI00",
        })
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        coordinator.set_command_capability("LMD", True)
        self.demand(coordinator, "LMD")
        entered = threading.Event()
        release = threading.Event()

        def blocked_optional(command: str, timeout: float = 1.5) -> str:
            entered.set()
            release.wait(1)
            return "LMD00"

        client.query_optional = blocked_optional
        await coordinator.async_request_refresh()
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        latest = coordinator.data
        await coordinator.async_cancel_optional_refresh()
        release.set()
        coordinator._apply_push_message("MVL30")
        await asyncio.sleep(0)
        self.assertIs(coordinator.data, latest)

    async def test_optional_queries_follow_supported_active_entity_contexts(self) -> None:
        client = FakeClient({
            "PWR": "PWR01", "MVL": "MVL20", "AMT": "AMT00", "SLI": "SLI00",
            "LMD": "LMD00", "IFA": "IFAHDMI 2,PCM,   48kHz, 2.0ch,All Ch Stereo,,",
        })
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        coordinator.set_command_capability("LMD", True)
        coordinator.set_command_capability("IFA", True)

        await coordinator.async_request_refresh()
        if coordinator._optional_task:
            await coordinator._optional_task
        self.assertEqual(client.queried, ["PWR", "MVL", "AMT", "SLI"])

        listen_mode_listener = coordinator.async_add_listener(
            lambda: None, frozenset({"LMD", "IFA"})
        )
        audio_sensor_listener = coordinator.async_add_listener(
            lambda: None, frozenset({"IFA"})
        )
        await coordinator.async_request_refresh()
        if coordinator._optional_task:
            await coordinator._optional_task
        self.assertCountEqual(
            [command for command in client.queried if command in {"LMD", "IFA"}],
            ["LMD", "IFA"],
        )
        self.assertEqual(coordinator._optional_command_set(), {"LMD", "IFA"})

        listen_mode_listener()
        self.assertEqual(coordinator._optional_command_set(), {"IFA"})
        audio_sensor_listener()
        self.assertEqual(coordinator._optional_command_set(), set())

    async def test_never_pushed_optional_command_keeps_regular_polling_cadence(self) -> None:
        client = FakeClient({})
        response = ["DIM01"]
        client.query_optional = lambda command, timeout=1.5: (
            client.queried.append(command) or response[0]
        )
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        coordinator.set_command_capability("DIM", True)
        self.demand(coordinator, "DIM")

        await coordinator._async_optional_refresh()
        self.assertEqual(coordinator.data.dimmer_level, "dim")
        self.assertNotIn("DIM", coordinator._optional_reconcile_at)

        response[0] = "DIM02"
        await coordinator._async_optional_refresh()
        self.assertEqual(coordinator.data.dimmer_level, "dark")
        self.assertEqual(client.queried, ["DIM", "DIM"])

    async def test_optional_push_updates_only_changed_state_and_revisions(self) -> None:
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=FakeClient({}),
            host="receiver",
            name="Test",
            update_interval=10,
        )
        coordinator.data = self.module.OnkyoState(
            power=True, audio_information={"listening_mode": "STALE IFA MODE"}
        )
        initial_state = coordinator.data
        notifications = []
        observed_before_set = []
        original_set = coordinator.async_set_updated_data

        def capture(data):
            observed_before_set.append(coordinator.data)
            notifications.append(data)
            original_set(data)

        coordinator.async_set_updated_data = capture
        coordinator._apply_push_message("LMD00")
        self.assertEqual(coordinator.data.listening_mode, "stereo")
        self.assertNotIn("listening_mode", coordinator.data.audio_information)
        self.assertIs(observed_before_set[0], initial_state)
        self.assertEqual(coordinator._optional_push_revisions["LMD"], 1)
        coordinator._apply_push_message("LMD00")
        self.assertEqual(len(notifications), 1)

        coordinator._apply_push_message(
            "IFAHDMI 2,PCM,   48kHz, 2.0ch,All Ch Stereo,,"
        )
        self.assertIn("input_terminal", coordinator.data.audio_information)
        self.assertEqual(coordinator._optional_push_revisions["IFA"], 1)
        self.assertEqual(len(notifications), 2)
        before_optional_response = coordinator.data
        coordinator._apply_optional_response("SLP", "SLP1E")
        self.assertIs(observed_before_set[-1], before_optional_response)
        self.assertEqual(coordinator.data.sleep_minutes, 30)

        zone2 = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=FakeClient({}),
            host="receiver",
            name="Zone 2",
            zone=self.module.ZONE_DEFINITIONS["zone2"],
            update_interval=10,
        )
        zone2.data = self.module.OnkyoState(power=True, listening_mode="zone mode")
        zone2._apply_push_message("LMD01")
        self.assertEqual(zone2.data.listening_mode, "zone mode")

    async def test_optional_push_revision_wins_over_older_inflight_query(self) -> None:
        client = FakeClient({
            "PWR": "PWR01", "MVL": "MVL20", "AMT": "AMT00", "SLI": "SLI00",
        })
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        coordinator.set_command_capability("LMD", True)
        self.demand(coordinator, "LMD")
        coordinator.data = self.module.OnkyoState(power=True, listening_mode="stereo")
        notifications = []
        original_set = coordinator.async_set_updated_data

        def capture(data):
            notifications.append(data)
            original_set(data)

        coordinator.async_set_updated_data = capture
        entered = threading.Event()
        release = threading.Event()

        def blocked_query(_command, timeout=1.5):
            entered.set()
            release.wait(1)
            return "LMD01"

        client.query_optional = blocked_query
        await coordinator.async_request_refresh()
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        coordinator._apply_push_message("LMD00")
        await asyncio.sleep(0)
        pushed_mode = coordinator.data.listening_mode
        self.assertEqual(pushed_mode, "stereo")
        self.assertEqual(notifications, [])
        release.set()
        await coordinator._optional_task
        self.assertEqual(coordinator.data.listening_mode, pushed_mode)
        self.assertEqual(notifications, [])

    async def test_lmd_push_wins_over_inflight_ifa_display(self) -> None:
        client = FakeClient({
            "PWR": "PWR01", "MVL": "MVL20", "AMT": "AMT00", "SLI": "SLI00",
        })
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        coordinator.set_command_capability("LMD", True)
        coordinator.set_command_capability("IFA", True)
        self.demand(coordinator, "LMD", "IFA")
        entered = threading.Event()
        release = threading.Event()

        def blocked_query(command, timeout=1.5):
            if command == "IFA":
                entered.set()
                release.wait(1)
                return "IFAHDMI 2,PCM,   48kHz, 2.0ch,All Ch Stereo,,"
            return "LMD01"

        client.query_optional = blocked_query
        await coordinator.async_request_refresh()
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        coordinator._apply_push_message("LMD01")
        await asyncio.sleep(0)
        pushed_mode = coordinator.data.listening_mode
        release.set()
        await coordinator._optional_task
        self.assertEqual(coordinator.data.listening_mode, pushed_mode)
        self.assertNotIn("listening_mode", coordinator.data.audio_information)

    async def test_optional_refresh_rechecks_demand_between_transactions(self) -> None:
        client = FakeClient({})
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        for command in ("DIM", "LMD"):
            coordinator.set_command_capability(command, True)
        remove_demand = coordinator.async_add_listener(
            lambda: None, frozenset({"DIM", "LMD"})
        )
        entered = threading.Event()
        release = threading.Event()
        queried = []

        def blocked_query(command, timeout=1.5):
            queried.append(command)
            entered.set()
            release.wait(1)
            return command + "00"

        client.query_optional = blocked_query
        task = asyncio.create_task(coordinator._async_optional_refresh())
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        remove_demand()
        release.set()
        await task
        self.assertEqual(len(queried), 1)

    async def test_continuous_push_keeps_fixed_reconciliation_deadline(self) -> None:
        client = FakeClient({})
        client.query_optional = lambda command, timeout=1.5: (
            client.queried.append(command) or "DIM01"
        )
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        coordinator.set_command_capability("DIM", True)
        self.demand(coordinator, "DIM")
        coordinator._optional_reconcile_interval = 0.05
        self.assertEqual(coordinator._optional_command_set(), {"DIM"})
        coordinator._apply_push_message("DIM01")
        deadline = coordinator._optional_reconcile_at["DIM"]

        for _ in range(5):
            coordinator._apply_push_message("DIM01")
            self.assertEqual(coordinator._optional_reconcile_at["DIM"], deadline)
            await coordinator._async_optional_refresh()
            self.assertEqual(client.queried, [])
            await asyncio.sleep(0.01)

        await asyncio.sleep(max(0, deadline - time.monotonic()) + 0.005)
        await coordinator._async_optional_refresh()
        self.assertEqual(client.queried, ["DIM"])
        self.assertNotIn("DIM", coordinator._optional_reconcile_at)
        self.assertNotIn("DIM", coordinator._optional_last_push_at)
        await coordinator._async_optional_refresh()
        self.assertEqual(client.queried, ["DIM", "DIM"])

    async def test_fixed_deadline_refresh_is_independent_of_push_updates(self) -> None:
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=FakeClient({}),
            host="receiver",
            name="Test",
            update_interval=0.02,
        )
        calls = 0
        refreshed = asyncio.Event()

        async def refresh():
            nonlocal calls
            calls += 1
            refreshed.set()

        coordinator.async_refresh = refresh
        coordinator.async_start_periodic_refresh()
        self.assertIsNone(coordinator.update_interval)
        self.assertFalse(coordinator.always_update)
        coordinator.data = self.module.OnkyoState(power=True)
        for index in range(30):
            coordinator._apply_push_message(f"MVL{index:02X}")
            await asyncio.sleep(0.001)
        await asyncio.wait_for(refreshed.wait(), 0.2)
        self.assertGreaterEqual(calls, 1)
        await coordinator.async_cancel_optional_refresh()
        self.assertTrue(coordinator._poll_task.done())

    async def test_zero_scan_interval_stays_manual_only(self) -> None:
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=FakeClient({}),
            host="receiver",
            name="Test",
            update_interval=0,
        )
        coordinator.async_start_periodic_refresh()
        self.assertIsNone(coordinator._poll_task)

    async def test_core_batch_keeps_optional_result_published_while_it_waits(self) -> None:
        client = FakeClient({"PWR": "PWR01", "MVL": "MVL20", "AMT": "AMT00", "SLI": "SLI00"})
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        entered = threading.Event()
        release = threading.Event()
        original = client.query_batch

        def blocked_batch(commands):
            if commands != ("PWR",):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("core query barrier was not released")
            return original(commands)

        client.query_batch = blocked_batch
        task = asyncio.create_task(coordinator._async_update_data())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            coordinator._apply_optional_response("LMD", "LMD00")
            self.assertEqual(coordinator.data.listening_mode, "stereo")
        finally:
            release.set()
        state = await task
        self.assertEqual(state.listening_mode, "stereo")

    async def test_shared_multi_zone_gate_runs_control_before_queued_optional(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=1)
        entered = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def optional_query(command, timeout=1.5):
            order.append(f"optional:{command}:start")
            if command == "LMD":
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("optional barrier was not released")
            order.append(f"optional:{command}:done")
            return command + "00"

        client._query_once = optional_query
        client._send_once = lambda command, value: order.append(f"control:{command}{value}")
        main = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Main", update_interval=10,
        )
        zone2 = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Zone 2",
            zone=self.module.ZONE_DEFINITIONS["zone2"], update_interval=10,
        )
        main.set_command_capability("LMD", True)
        main.set_command_capability("SLA", True)
        self.demand(main, "LMD", "SLA")

        async def no_refresh():
            return None

        zone2.async_request_refresh = no_refresh
        optional_task = asyncio.create_task(main._async_optional_refresh())
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        control_task = asyncio.create_task(zone2.async_turn_off())
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(optional_task, control_task)
        self.assertLess(order.index("control:ZPW00"), order.index("optional:SLA:start"))

    async def test_control_can_use_gate_while_query_waits_between_retries(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=2)
        cooldown_waiting = threading.Event()
        release_retry = threading.Event()
        attempts: list[int] = []
        sends: list[tuple[str, str]] = []

        def query(command, timeout=5.0):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("simulated disconnect")
            return "PWR01"

        def wait_cooldown():
            cooldown_waiting.set()
            if not release_retry.wait(2):
                raise TimeoutError("retry barrier was not released")

        client._query_once = query
        client._send_once = lambda command, value: sends.append((command, value))
        client._wait_cooldown = wait_cooldown
        result: list[str] = []
        query_thread = threading.Thread(target=lambda: result.append(client.query("PWR")))
        query_thread.start()
        self.assertTrue(await asyncio.to_thread(cooldown_waiting.wait, 1))
        client._circuit_open_until = time.monotonic() - 1
        client.send("MVL", "UP")
        release_retry.set()
        query_thread.join(timeout=2)
        self.assertEqual(result, ["PWR01"])
        self.assertEqual(sends, [("MVL", "UP")])

    async def test_build_runtime_data_selects_tx8050_profile(self) -> None:
        runtime = self.module.build_runtime_data(
            self.hass,
            host="192.168.1.14",
            port=60128,
            name="Onkyo TX-8050",
            model="TX-8050",
            scan_interval=10,
            sources={"Tuner": "tuner", "CD": "cd"},
            max_volume=80,
        )

        self.assertEqual(runtime.model, "TX-8050")
        self.assertEqual(runtime.device_info["model"], "TX-8050")
        self.assertEqual(runtime.supported_audio_selectors, [])
        self.assertEqual(runtime.supported_late_night_modes, [])
        self.assertEqual(runtime.supported_audyssey_volume_modes, [])
        self.assertEqual(runtime.queryable_commands, ("LMD", "DIM", "SLP", "TUN"))

    async def test_build_runtime_data_uses_tx8050_manual_default_sources(self) -> None:
        runtime = self.module.build_runtime_data(
            self.hass,
            host="192.168.1.14",
            port=60128,
            name="Onkyo TX-8050",
            model="TX-8050",
            scan_interval=10,
            sources=self.module.PROFILES["TX-8050"].default_sources,
            max_volume=80,
        )

        self.assertEqual(
            runtime.sources,
            {
                "DVD -- BD/DVD": "dvd",
                "TAPE -- TV/TAPE": "tape-1",
                "PHONO": "phono",
                "CD -- TV/CD": "cd",
                "FM": "fm",
                "AM": "am",
                "INTERNET RADIO": "internet-radio",
                "NETWORK": "network",
            },
        )

    async def test_build_runtime_data_rejects_unknown_source_alias(self) -> None:
        with self.assertRaises(self.module.UpdateFailed):
            self.module.build_runtime_data(
                self.hass,
                host="192.168.1.23",
                port=60128,
                name="Onkyo PR-SC5507",
                model="PR-SC5507",
                scan_interval=10,
                sources={"Bad": "not-a-source"},
                max_volume=80,
            )

    async def test_build_runtime_data_warns_skips_unknown_source_in_permissive_mode(self) -> None:
        runtime = self.module.build_runtime_data(
            self.hass,
            host="192.168.1.23",
            port=60128,
            name="Onkyo PR-SC5507",
            model="PR-SC5507",
            scan_interval=10,
            sources={"Bad": "not-a-source", "Blu-ray": "dvd"},
            max_volume=80,
            strict_sources=False,
        )
        self.assertIn("DVD -- BD/DVD", runtime.sources)
        self.assertNotIn("Bad", runtime.sources)
        self.assertEqual(len(runtime.sources), 1)

    async def test_normalize_sources_returns_skipped_set(self) -> None:
        normalized, skipped = self.module._normalize_sources(
            {"Bad": "not-a-source", "TV": "tv"},
            strict=False,
        )
        self.assertEqual(skipped, {"not-a-source"})
        self.assertIn("TV", normalized)

    async def test_build_runtime_data_filters_zone_only_supported_sources(self) -> None:
        runtime = self.module.build_runtime_data(
            self.hass,
            host="192.168.1.23",
            port=60128,
            name="Onkyo PR-SC5507",
            model="PR-SC5507",
            scan_interval=10,
            sources={"Optical": "optical", "Blu-ray": "dvd", "TV": "tv"},
            max_volume=80,
        )

        self.assertEqual(runtime.sources, {"OPTICAL": "optical", "DVD -- BD/DVD": "dvd", "TV": "tv"})
        self.assertEqual(tuple(z.zone_key for z in runtime.candidate_zones), ("zone2", "zone3"))

    async def test_update_data_parses_prsc5507_queryable_state(self) -> None:
        client = FakeClient(
            {
                "PWR": "PWR01",
                "MVL": "MVL00",
                "AMT": "AMT00",
                "SLI": "SLI02",
                "SLA": "SLA00",
                "LMD": "LMD0C",
                "LTN": "LTN00",
                "RAS": "RAS01",
                "ADQ": "ADQ01",
                "ADV": "ADV02",
                "MOT": "MOT01",
                "TGA": "TGA00",
                "TGB": "TGB01",
                "TGC": "TGC01",
                "DIM": "DIM00",
                "SLP": "SLP00",
                "CTL": "CTL00",
                "SWL": "SWL+1",
                "IFA": "IFAHDMI 2,PCM,   48kHz, 2.0ch,All Ch Stereo,,",
                "IFV": "IFVV(VCR/DVR),UNKNOWN,,,Analog, 720x480i  60Hz,,,Custom,",
            }
        )
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="192.168.1.23",
            name="Onkyo PR-SC5507",
            update_interval=10,
        )
        for command in (
            "SLA",
            "LMD",
            "LTN",
            "RAS",
            "ADQ",
            "ADV",
            "MOT",
            "TGA",
            "TGB",
            "TGC",
            "DIM",
            "SLP",
            "CTL",
            "SWL",
            "IFA",
            "IFV",
        ):
            coordinator.set_command_capability(command, True)
        self.demand(
            coordinator,
            "SLA", "LMD", "LTN", "RAS", "ADQ", "ADV", "MOT", "TGA", "TGB",
            "TGC", "DIM", "SLP", "CTL", "SWL", "IFA", "IFV",
        )

        state = None
        for _ in range(10):
            state = await coordinator._async_update_data()
            coordinator.data = state
        if coordinator._optional_task:
            await coordinator._optional_task
        for _ in range(20):
            await coordinator._async_optional_refresh()
        state = coordinator.data

        self.assertTrue(state.power)
        self.assertEqual(state.volume, 0)
        self.assertFalse(state.muted)
        self.assertEqual(state.source, "video3")
        self.assertEqual(state.audio_selector, "auto")
        self.assertIsNotNone(state.listening_mode)
        self.assertTrue(state.audio_information)
        self.assertTrue(state.video_information)
        self.assertIn("IFV", client.queried)
        self.assertIn("IFA", client.queried)

    async def test_update_data_parses_tx8050_audio_only_state(self) -> None:
        client = FakeClient(
            {
                "PWR": "PWR01",
                "MVL": "MVL2A",
                "AMT": "AMT00",
                "SLI": "SLI2B",
                "LMD": "LMD00",
                "DIM": "DIM01",
                "SLP": "SLP1E",
                "TUN": "TUN10710",
            }
        )
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="192.168.1.14",
            name="Onkyo TX-8050",
            update_interval=10,
        )
        for command in ("LMD", "DIM", "SLP", "TUN"):
            coordinator.set_command_capability(command, True)
        self.demand(coordinator, "LMD", "DIM", "SLP", "TUN")

        coordinator.data = await coordinator._async_update_data()
        coordinator.data = await coordinator._async_update_data()
        state = await coordinator._async_update_data()
        coordinator.data = state
        if coordinator._optional_task:
            await coordinator._optional_task
        for _ in range(8):
            await coordinator._async_optional_refresh()
        state = coordinator.data

        self.assertTrue(state.power)
        self.assertEqual(state.volume, 42)
        self.assertFalse(state.muted)
        self.assertEqual(state.source, "network")
        self.assertEqual(state.listening_mode, "stereo")
        self.assertEqual(state.dimmer_level, "dim")
        self.assertEqual(state.sleep_minutes, 30)
        self.assertEqual(state.tuner_frequency, 10710)

    async def test_dimmer_service_accepts_raw_code_and_rejects_invalid_value(self) -> None:
        client = FakeClient({"PWR": "PWR00"})
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10
        )
        await coordinator.async_set_dimmer_level("01")
        self.assertIn(("DIM", "01"), client.sent)
        with self.assertRaises(self.module.HomeAssistantError):
            await coordinator.async_set_dimmer_level("not-a-level")

    async def test_optional_write_refresh_prioritizes_new_state(self) -> None:
        client = FakeClient(
            {
                "PWR": "PWR01",
                "MVL": "MVL20",
                "AMT": "AMT00",
                "SLI": "SLI00",
                "LMD": "LMD00",
                "DIM": "DIM01",
            }
        )
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10
        )
        for command in self.module.GENERIC_PROFILE.queryable_commands:
            coordinator.set_command_capability(command, True)
        self.demand(coordinator, *self.module.GENERIC_PROFILE.queryable_commands)
        coordinator._optional_failures["LMD"] = (3, time.monotonic() + 3600)
        coordinator._optional_failures["DIM"] = (3, time.monotonic() + 3600)
        coordinator._optional_reconcile_at["LMD"] = time.monotonic() + 3600
        coordinator._optional_reconcile_at["DIM"] = time.monotonic() + 3600

        await coordinator.async_set_listening_mode("stereo")
        if coordinator._optional_task:
            await coordinator._optional_task
        self.assertEqual(coordinator.data.listening_mode, "stereo")
        self.assertIn("LMD", client.queried)

        await coordinator.async_set_dimmer_level("01")
        if coordinator._optional_task:
            await coordinator._optional_task
        self.assertEqual(coordinator.data.dimmer_level, "dim")
        self.assertEqual(coordinator.data.listening_mode, "stereo")
        self.assertIn("DIM", client.queried)

    async def test_optional_write_requests_confirmation_without_entity_context(self) -> None:
        client = FakeClient({
            "PWR": "PWR01", "MVL": "MVL20", "AMT": "AMT00", "SLI": "SLI00",
            "DIM": "DIM01",
        })
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        coordinator.set_command_capability("DIM", True)
        self.assertEqual(coordinator._optional_command_set(), set())

        await coordinator.async_set_dimmer_level("dim")
        if coordinator._optional_task:
            await coordinator._optional_task

        self.assertIn("DIM", client.queried)
        self.assertEqual(coordinator.data.dimmer_level, "dim")

    async def test_video_information_merges_across_full_optional_rotation(self) -> None:
        client = FakeClient(
            {
                "PWR": "PWR01",
                "MVL": "MVL20",
                "AMT": "AMT00",
                "SLI": "SLI00",
                "IFV": "IFVnew-input,,,,new-output,,,,,",
            }
        )
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10
        )
        for command in self.module.GENERIC_PROFILE.queryable_commands:
            coordinator.set_command_capability(command, True)
        self.demand(coordinator, *self.module.GENERIC_PROFILE.queryable_commands)
        coordinator.data = self.module.OnkyoState(
            power=True,
            video_information={"video_input": "old-input", "custom": "preserve"},
        )

        for _ in range(10):
            coordinator.data = await coordinator._async_update_data()
            if coordinator._optional_task:
                await coordinator._optional_task

        self.assertEqual(coordinator.data.video_information["video_input"], "NEW-INPUT")
        self.assertEqual(coordinator.data.video_information["video_output"], "NEW-OUTPUT")
        self.assertEqual(coordinator.data.video_information["custom"], "preserve")

    async def test_update_data_keeps_previous_optional_values_when_queries_fail(self) -> None:
        client = FakeClient(
            {
                "PWR": "PWR01",
                "MVL": "MVL2A",
                "AMT": "AMT00",
                "SLI": "SLI02",
                "SLA": TimeoutError("temporary timeout"),
                "RAS": TimeoutError("temporary timeout"),
                "SLP": TimeoutError("temporary timeout"),
                "IFA": TimeoutError("temporary timeout"),
            }
        )
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="192.168.1.23",
            name="Onkyo PR-SC5507",
            update_interval=10,
        )
        for command in ("SLA", "RAS", "SLP", "IFA"):
            coordinator.set_command_capability(command, True)
        self.demand(coordinator, "SLA", "RAS", "SLP", "IFA")
        coordinator.data = self.module.OnkyoState(
            audio_selector="auto",
            cinema_filter=True,
            sleep_minutes=45,
            audio_information={"input_terminal": "HDMI 1"},
        )

        state = await coordinator._async_update_data()
        coordinator.data = state
        if coordinator._optional_task:
            await coordinator._optional_task
        state = coordinator.data

        self.assertEqual(state.audio_selector, "auto")
        self.assertTrue(state.cinema_filter)
        self.assertEqual(state.sleep_minutes, 45)
        self.assertEqual(state.audio_information, {"input_terminal": "HDMI 1"})

    async def test_live_truth_send_paths_cover_retained_controls(self) -> None:
        client = FakeClient({"PWR": "PWR01", "MVL": "MVL23", "AMT": "AMT00", "SLI": "SLI00"})
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="192.168.1.23",
            name="Onkyo PR-SC5507",
            update_interval=10,
        )
        coordinator.data = self.module.OnkyoState()

        await coordinator.async_set_trigger("TGA", True)
        await coordinator.async_set_sleep_minutes(30)
        await coordinator.async_set_level("CTL", -2)
        await coordinator.async_set_audio_selector("hdmi")
        await coordinator.async_set_late_night_mode("low-dolbydigital")
        await coordinator.async_set_boolean_option("RAS", True)
        await coordinator.async_set_boolean_option("ADQ", False)
        await coordinator.async_set_audyssey_dynamic_volume("heavy")
        await coordinator.async_set_boolean_option("MOT", True)

        self.assertIn(("TGA", "01"), client.sent)
        self.assertIn(("SLP", "1E"), client.sent)
        self.assertIn(("CTL", "-2"), client.sent)
        self.assertIn(("SLA", "04"), client.sent)
        self.assertIn(("LTN", "01"), client.sent)
        self.assertIn(("RAS", "01"), client.sent)
        self.assertIn(("ADQ", "00"), client.sent)
        self.assertIn(("ADV", "03"), client.sent)
        self.assertIn(("MOT", "01"), client.sent)

    async def test_zone2_update_and_send_paths_use_zone_specific_commands(self) -> None:
        client = FakeClient(
            {
                "ZPW": "ZPW01",
                "ZVL": "ZVL1E",
                "ZMT": "ZMT01",
                "SLZ": "SLZ10",
            }
        )
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="192.168.1.23",
            name="Onkyo PR-SC5507",
            zone=self.module.ZONE_DEFINITIONS["zone2"],
            update_interval=10,
        )

        state = await coordinator._async_update_data()

        self.assertTrue(state.power)
        self.assertEqual(state.volume, 30)
        self.assertTrue(state.muted)
        self.assertEqual(state.source, "dvd")

        coordinator.data = state
        await coordinator.async_turn_off()
        await coordinator.async_set_muted(False)
        await coordinator.async_volume_step(True)
        await coordinator.async_select_source("tv")

        self.assertIn(("ZPW", "00"), client.sent)
        self.assertIn(("ZMT", "00"), client.sent)
        self.assertIn(("ZVL", "UP1"), client.sent)
        self.assertIn(("SLZ", "12"), client.sent)

    async def test_query_batch_returns_all_results(self) -> None:
        client = self.module.OnkyoLegacyClient("test", 60128)
        device = object()
        client._connect = lambda: device  # type: ignore[method-assign]
        responses = {"PWR": "PWR01", "MVL": "MVL2A", "SLI": "SLI02"}
        client._query_locked = lambda _device, command, timeout=5.0: responses[command]  # type: ignore[method-assign]

        results = client.query_batch(("PWR", "MVL", "SLI"))

        self.assertEqual(results, {"PWR": "PWR01", "MVL": "MVL2A", "SLI": "SLI02"})

    async def test_query_batch_aborts_on_first_failure(self) -> None:
        client = self.module.OnkyoLegacyClient("test", 60128, retries=1)
        client._connect = lambda: object()  # type: ignore[method-assign]

        def query(_device, command, timeout=5.0):
            if command == "MVL":
                raise TimeoutError("no response")
            return f"{command}01"

        client._query_locked = query  # type: ignore[method-assign]
        with self.assertRaises(TimeoutError):
            client.query_batch(("PWR", "MVL", "SLI"))
        self.assertEqual(client._consecutive_failures, 1)

    async def test_update_data_raises_when_core_command_missing_from_batch(self) -> None:
        client = FakeClient({"PWR": "PWR01"})
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="192.168.1.23",
            name="Onkyo PR-SC5507",
            update_interval=10,
        )

        with self.assertRaises(self.module.UpdateFailed):
            await coordinator._async_update_data()

    async def test_update_data_raises_when_batch_completely_empty(self) -> None:
        client = FakeClient({})
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="192.168.1.23",
            name="Onkyo PR-SC5507",
            update_interval=10,
        )

        with self.assertRaises(self.module.UpdateFailed):
            await coordinator._async_update_data()

    # ------------------------------------------------------------------
    # Circuit breaker tests for OnkyoLegacyClient
    # ------------------------------------------------------------------

    async def test_circuit_breaker_resets_on_success(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=1)
        client._query_once = lambda command: "PWR01"  # type: ignore[method-assign]
        client._consecutive_failures = 3
        client._circuit_open_until = time.monotonic() - 1
        self.assertEqual(client.query("PWR"), "PWR01")
        self.assertEqual(client._consecutive_failures, 0)
        self.assertEqual(client._circuit_open_until, 0.0)

    async def test_circuit_breaker_opens_after_consecutive_failures(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=1)
        now = [100.0]
        with patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]):
            client._query_once = lambda command: (_ for _ in ()).throw(OSError("down"))  # type: ignore[method-assign]
            with self.assertRaises(OSError):
                client.query("PWR")
            self.assertEqual(client._circuit_open_until, 102.0)
            with self.assertRaises(ConnectionError):
                client.query("PWR")
            now[0] = 102.0
            with self.assertRaises(OSError):
                client.query("PWR")
            self.assertEqual(client._consecutive_failures, 2)
            self.assertEqual(client._circuit_open_until, 106.0)

    async def test_circuit_breaker_blocks_when_open(self) -> None:
        """When circuit is open, calls should raise ConnectionError immediately."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128, retries=1)
        client._circuit_open_until = time.monotonic() + 60

        with self.assertRaises(ConnectionError):
            client.send("MVL", "UP")

        with self.assertRaises(ConnectionError):
            client.query("PWR")

    async def test_circuit_breaker_recovers_after_cooldown(self) -> None:
        """Calls recover after the capped cooldown expires."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128, retries=1)
        client._circuit_open_until = time.monotonic() - 1  # just expired
        client._consecutive_failures = 5

        client._query_once = lambda command: "PWR01"  # type: ignore[method-assign]
        client._send_once = lambda command, value: None  # type: ignore[method-assign]

        result = client.query("PWR")
        self.assertEqual(result, "PWR01")
        self.assertEqual(client._consecutive_failures, 0)

        client.send("MVL", "UP")

    async def test_reconnect_cooldown_caps_at_one_minute(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=1)
        now = [10.0]
        client._query_once = lambda command: (_ for _ in ()).throw(OSError("down"))  # type: ignore[method-assign]
        with patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]):
            for index in range(2000):
                with self.assertRaises(OSError):
                    client.query("PWR")
                delay = client._circuit_open_until - now[0]
                self.assertGreater(delay, 0)
                if index == 0:
                    self.assertAlmostEqual(delay, 2.0)
                self.assertLessEqual(delay, 60.0)
                now[0] += delay

    async def test_circuit_breaker_reopens_after_recovery_failure(self) -> None:
        """After cooldown, if the next call fails, circuit should re-open."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128, retries=1)
        client._consecutive_failures = 5
        client._circuit_open_until = time.monotonic() - 1  # just expired

        client._query_once = lambda command: (_ for _ in ()).throw(OSError("down"))  # type: ignore[method-assign]

        with self.assertRaises(OSError):
            client.query("PWR")

        self.assertEqual(client._consecutive_failures, 6)
        self.assertGreater(client._circuit_open_until, time.monotonic())

    async def test_send_raises_home_assistant_error_when_circuit_open(self) -> None:
        """Verify that send() surfaces HomeAssistantError when circuit is open."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128, retries=1)
        client._circuit_open_until = time.monotonic() + 60

        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="127.0.0.1",
            name="Test",
            update_interval=10,
        )

        with self.assertRaises(self.module.HomeAssistantError):
            await coordinator._async_send("MVL", "UP")

    async def test_queued_absolute_volume_writes_coalesce_before_gate_grant(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=2)
        sent: list[tuple[str, str]] = []
        queried: list[str] = []
        client._send_once = lambda command, value: sent.append((command, value))  # type: ignore[method-assign]

        def query(command: str, timeout: float = 5.0) -> str:
            queried.append(command)
            return f"{command}{sent[-1][1]}"

        client._query_once = query  # type: ignore[method-assign]
        gate = client._operation_lock
        acquired = threading.Event()
        release = threading.Event()

        def hold_gate() -> None:
            gate.acquire(0)
            acquired.set()
            release.wait(2)
            gate.release()

        holder = threading.Thread(target=hold_gate)
        holder.start()
        self.assertTrue(await asyncio.to_thread(acquired.wait, 1))
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )
        calls = [
            asyncio.create_task(coordinator.async_set_volume(value, 80))
            for value in (8 / 80, 16 / 80, 40 / 80)
        ]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with gate._condition:
                if gate._waiters:
                    break
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)
        release.set()
        holder.join(1)
        await asyncio.gather(*calls)
        self.assertEqual(sent, [("MVL", "28")])
        self.assertEqual(queried, ["MVL"])

    async def test_inflight_absolute_is_not_replaced_but_next_burst_coalesces(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=2)
        sent: list[tuple[str, str]] = []
        claimed = threading.Event()
        release = threading.Event()

        def send(command: str, value: str) -> None:
            sent.append((command, value))
            if len(sent) == 1:
                claimed.set()
                release.wait(2)

        client._send_once = send  # type: ignore[method-assign]
        client._query_once = lambda command, timeout=5.0: f"{command}{sent[-1][1]}"  # type: ignore[method-assign]
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )
        first = asyncio.create_task(coordinator.async_set_volume(8 / 80, 80))
        self.assertTrue(await asyncio.to_thread(claimed.wait, 1))
        next_burst = [
            asyncio.create_task(coordinator.async_set_volume(value, 80))
            for value in (16 / 80, 40 / 80)
        ]
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(first, *next_burst)
        self.assertEqual(sent, [("MVL", "08"), ("MVL", "28")])

    async def test_zone_volume_queues_coalesce_independently(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=2)
        sent: list[tuple[str, str]] = []
        gate = client._operation_lock
        acquired = threading.Event()
        release = threading.Event()

        def hold_gate() -> None:
            gate.acquire(0)
            acquired.set()
            release.wait(2)
            gate.release()

        client._send_once = lambda command, value: sent.append((command, value))  # type: ignore[method-assign]
        values = {"MVL": "28", "ZVL": "20"}
        client._query_once = lambda command, timeout=5.0: f"{command}{values[command]}"  # type: ignore[method-assign]
        holder = threading.Thread(target=hold_gate)
        holder.start()
        self.assertTrue(await asyncio.to_thread(acquired.wait, 1))
        main = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )
        zone2 = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="receiver",
            name="Test",
            zone=self.module.ZONE_DEFINITIONS["zone2"],
            update_interval=0,
        )
        tasks = [
            asyncio.create_task(main.async_set_volume(8 / 80, 80)),
            asyncio.create_task(main.async_set_volume(40 / 80, 80)),
            asyncio.create_task(zone2.async_set_volume(16 / 80, 80)),
            asyncio.create_task(zone2.async_set_volume(32 / 80, 80)),
        ]
        await asyncio.sleep(0.02)
        release.set()
        holder.join(1)
        await asyncio.gather(*tasks)
        self.assertCountEqual(sent, [("MVL", "28"), ("ZVL", "20")])

    async def test_failed_volume_send_is_attempted_once(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=3)
        attempts: list[tuple[str, str]] = []

        def fail(command: str, value: str) -> None:
            attempts.append((command, value))
            raise OSError("ambiguous socket failure")

        client._send_once = fail  # type: ignore[method-assign]
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )
        with self.assertRaises(self.module.HomeAssistantError):
            await coordinator.async_set_volume(0.25, 80)
        self.assertEqual(attempts, [("MVL", "14")])

    async def test_pending_control_dispatches_before_volume_confirmation_wait(self) -> None:
        client = FakeClient({})
        sent: list[tuple[str, str]] = []
        query_started = threading.Event()
        volume_sent = threading.Event()
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )
        coordinator.async_request_refresh = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
        def send(command: str, value: str) -> None:
            sent.append((command, value))
            if command == "MVL":
                volume_sent.set()

        client.send = send
        client.query_optional = lambda command, timeout=1.5: (
            query_started.set() or "MVL14"
        )
        volume = asyncio.create_task(coordinator.async_set_volume(0.25, 80))
        self.assertTrue(await asyncio.to_thread(volume_sent.wait, 1))
        source = asyncio.create_task(coordinator._async_send("SLI", "12"))
        await asyncio.wait_for(source, 0.05)
        self.assertEqual(sent[:2], [("MVL", "14"), ("SLI", "12")])
        self.assertFalse(query_started.is_set())
        await volume
        self.assertTrue(query_started.is_set())

    async def test_volume_dispatches_while_prior_control_refresh_is_blocked(self) -> None:
        client = FakeClient({})
        sent: list[tuple[str, str]] = []
        volume_sent = threading.Event()
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )

        async def blocked_refresh() -> None:
            refresh_started.set()
            await release_refresh.wait()

        coordinator.async_request_refresh = blocked_refresh  # type: ignore[method-assign]

        def send(command: str, value: str) -> None:
            sent.append((command, value))
            if command == "MVL":
                volume_sent.set()
                self.hass.loop.call_soon_threadsafe(
                    coordinator._apply_push_message, f"{command}{value}"
                )

        client.send = send
        source = asyncio.create_task(coordinator._async_send("SLI", "12"))
        await asyncio.wait_for(refresh_started.wait(), 1)
        volume = asyncio.create_task(coordinator.async_set_volume(0.25, 80))
        self.assertTrue(await asyncio.to_thread(volume_sent.wait, 1))
        self.assertEqual(sent, [("SLI", "12"), ("MVL", "14")])
        release_refresh.set()
        await asyncio.gather(source, volume)

    async def test_late_old_volume_query_cannot_replace_newer_push(self) -> None:
        client = FakeClient({})
        sent: list[tuple[str, str]] = []
        query_started = threading.Event()
        release_query = threading.Event()
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )

        def send(command: str, value: str) -> None:
            sent.append((command, value))
            if len(sent) == 2:
                self.hass.loop.call_soon_threadsafe(
                    coordinator._apply_push_message, "MVL14"
                )

        def query(command: str, timeout: float = 1.5) -> str:
            query_started.set()
            release_query.wait(2)
            return "MVL08"

        client.send = send
        client.query_optional = query
        coordinator._volume_confirmation_grace = 0
        old_request = asyncio.create_task(coordinator.async_set_volume(8 / 80, 80))
        self.assertTrue(await asyncio.to_thread(query_started.wait, 1))
        new_request = asyncio.create_task(coordinator.async_set_volume(20 / 80, 80))
        await asyncio.wait_for(new_request, 1)
        release_query.set()
        await old_request
        self.assertEqual(sent, [("MVL", "08"), ("MVL", "14")])
        self.assertEqual(coordinator.data.volume, 20)

    async def test_targeted_volume_observation_wins_over_older_core_batch(self) -> None:
        core_started = threading.Event()
        release_core = threading.Event()

        class BlockingClient:
            sent: list[tuple[str, str]] = []

            def send(self, command: str, value: str) -> None:
                self.sent.append((command, value))

            def query_optional(self, command: str, timeout: float = 1.5) -> str:
                return "MVL14"

            def query_batch(self, commands: tuple[str, ...]) -> dict[str, str]:
                if commands == ("PWR",):
                    return {"PWR": "PWR01"}
                core_started.set()
                release_core.wait(2)
                return {"MVL": "MVL08", "AMT": "AMT00", "SLI": "SLI00"}

        client = BlockingClient()
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="receiver",
            name="Test",
            update_interval=0,
        )
        coordinator.data = self.module.OnkyoState(volume=8)
        old_refresh = asyncio.create_task(coordinator._async_update_data())
        self.assertTrue(await asyncio.to_thread(core_started.wait, 1))
        await coordinator.async_set_volume(20 / 80, 80)
        self.assertEqual(coordinator.data.volume, 20)
        release_core.set()
        returned = await old_refresh
        self.assertEqual(returned.volume, 20)

    async def test_non_numeric_confirmation_never_claims_volume_success(self) -> None:
        client = FakeClient({})
        client.query_optional = lambda command, timeout=1.5: "MVLN/A"
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )
        coordinator.data = self.module.OnkyoState(volume=20)
        with self.assertRaises(self.module.HomeAssistantError):
            await coordinator.async_set_volume(20 / 80, 80)
        self.assertEqual(coordinator.data.volume, 20)

        with self.assertRaises(self.module.HomeAssistantError):
            await coordinator.async_volume_step(True)
        self.assertEqual(client.sent, [
            ("MVL", "14"),
            ("MVL", "UP1"),
        ])

    async def test_volume_relative_and_unrelated_controls_are_queue_barriers(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=2)
        sent: list[tuple[str, str]] = []
        volume = ["08"]

        def send(command: str, value: str) -> None:
            sent.append((command, value))
            if command == "MVL" and len(value) == 2:
                volume[0] = value

        client._send_once = send  # type: ignore[method-assign]
        client._query_once = lambda command, timeout=5.0: {
            "PWR": "PWR01",
            "MVL": f"MVL{volume[0]}",
            "AMT": "AMT00",
            "SLI": "SLI00",
        }[command]  # type: ignore[method-assign]
        gate = client._operation_lock
        acquired = threading.Event()
        release = threading.Event()

        def hold_gate() -> None:
            gate.acquire(0)
            acquired.set()
            release.wait(2)
            gate.release()

        holder = threading.Thread(target=hold_gate)
        holder.start()
        self.assertTrue(await asyncio.to_thread(acquired.wait, 1))
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )
        tasks = [
            asyncio.create_task(coordinator.async_set_volume(8 / 80, 80)),
            asyncio.create_task(coordinator.async_volume_step(True)),
            asyncio.create_task(coordinator._async_send("SLI", "12")),
            asyncio.create_task(coordinator.async_set_volume(40 / 80, 80)),
        ]
        await asyncio.sleep(0.02)
        release.set()
        holder.join(1)
        await asyncio.gather(*tasks)
        self.assertEqual(
            sent,
            [("MVL", "08"), ("MVL", "UP1"), ("SLI", "12"), ("MVL", "28")],
        )

    async def test_matching_volume_push_skips_confirmation_query(self) -> None:
        client = FakeClient({})
        queried: list[str] = []
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )

        def send(command: str, value: str) -> None:
            client.sent.append((command, value))
            self.hass.loop.call_soon_threadsafe(
                coordinator._apply_push_message, f"{command}{value}"
            )

        client.send = send
        client.query_optional = lambda command, timeout=1.5: (
            queried.append(command) or "MVL14"
        )
        await coordinator.async_set_volume(0.25, 80)
        self.assertEqual(client.sent, [("MVL", "14")])
        self.assertEqual(queried, [])
        self.assertEqual(coordinator.data.volume, 20)

    async def test_mismatched_volume_query_updates_observed_state_and_fails(self) -> None:
        client = FakeClient({})
        queried: list[str] = []
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )
        coordinator.data = self.module.OnkyoState(volume=5)
        client.query_optional = lambda command, timeout=1.5: (
            queried.append(command) or "MVL0A"
        )
        with self.assertRaises(self.module.HomeAssistantError):
            await coordinator.async_set_volume(0.25, 80)
        self.assertEqual(client.sent, [("MVL", "14")])
        self.assertEqual(queried, ["MVL"])
        self.assertEqual(coordinator.data.volume, 10)

    async def test_shutdown_settles_volume_waiter_without_sending_queued_write(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=2)
        sent: list[tuple[str, str]] = []
        client._send_once = lambda command, value: sent.append((command, value))  # type: ignore[method-assign]
        gate = client._operation_lock
        acquired = threading.Event()
        release = threading.Event()

        def hold_gate() -> None:
            gate.acquire(0)
            acquired.set()
            release.wait(2)
            gate.release()

        holder = threading.Thread(target=hold_gate)
        holder.start()
        self.assertTrue(await asyncio.to_thread(acquired.wait, 1))
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=0,
        )
        caller = asyncio.create_task(coordinator.async_set_volume(0.25, 80))
        await asyncio.sleep(0.01)
        coordinator._closing = True
        client.begin_close()
        await coordinator.async_cancel_writes()
        with self.assertRaises(self.module.HomeAssistantError):
            await caller
        release.set()
        holder.join(1)
        await asyncio.sleep(0.01)
        self.assertEqual(sent, [])

    # ------------------------------------------------------------------
    # ModelProfile and GENERIC_PROFILE tests
    # ------------------------------------------------------------------

    async def test_model_profile_prsc5507_lookup(self) -> None:
        profile = self.module.PROFILES["PR-SC5507"]
        self.assertEqual(profile.model, "PR-SC5507")
        self.assertEqual(profile.default_name, "Onkyo PR-SC5507")
        self.assertEqual(profile.max_volume, 80)
        self.assertIn("dvd", profile.default_sources.values())
        self.assertIn("LMD", profile.queryable_commands)
        self.assertIn("IFA", profile.queryable_commands)

    async def test_model_profile_tx8050_lookup(self) -> None:
        profile = self.module.PROFILES["TX-8050"]
        self.assertEqual(profile.model, "TX-8050")
        self.assertEqual(profile.default_name, "Onkyo TX-8050")
        self.assertEqual(profile.max_volume, 80)
        self.assertEqual(profile.queryable_commands, ("LMD", "DIM", "SLP", "TUN"))
        self.assertIn("dvd", profile.default_sources.values())

    async def test_generic_profile_exists(self) -> None:
        profile = self.module.GENERIC_PROFILE
        self.assertEqual(profile.model, "GENERIC")
        self.assertGreater(len(profile.queryable_commands), 0)

    async def test_normalize_model_known(self) -> None:
        result = self.module._normalize_model("PR-SC5507")
        self.assertEqual(result, "PR-SC5507")

    async def test_normalize_model_unknown_passthrough(self) -> None:
        result = self.module._normalize_model("some-random-model")
        self.assertEqual(result, "SOME-RANDOM-MODEL")

    async def test_build_runtime_data_uses_generic_profile(self) -> None:
        runtime = self.module.build_runtime_data(
            self.hass,
            host="192.168.1.99",
            port=60128,
            name="Unknown Receiver",
            model="UNKNOWN-MODEL",
            scan_interval=10,
            sources={},
            max_volume=80,
        )
        self.assertEqual(runtime.model, "UNKNOWN-MODEL")
        self.assertEqual(runtime.queryable_commands, self.module.GENERIC_PROFILE.queryable_commands)
        self.assertEqual(runtime.sources, {})
        self.assertIn("auto", runtime.supported_audio_selectors)
        self.assertIn("off", runtime.supported_late_night_modes)
        self.assertIn("medium", runtime.supported_audyssey_volume_modes)

    # ------------------------------------------------------------------
    # probe_commands tests
    # ------------------------------------------------------------------

    async def test_probe_commands_marks_supported(self) -> None:
        client = self.module.OnkyoLegacyClient("test", 60128, retries=1)

        def mock_query_optional(command: str, timeout: float = 1.5) -> str:
            if command == "PWR":
                return "PWR01"
            raise self.module.CommandResponseTimeout("no response")

        client.query_optional = mock_query_optional  # type: ignore[method-assign]

        results = client.probe_commands(("PWR", "XYZ"))
        self.assertEqual(results, {"PWR": True, "XYZ": False})

    async def test_probe_commands_does_not_trip_circuit_breaker(self) -> None:
        client = self.module.OnkyoLegacyClient("test", 60128, retries=1)
        client._consecutive_failures = 2

        def failing_query_optional(command: str, timeout: float = 1.5) -> str:
            raise self.module.CommandResponseTimeout("no response")

        client.query_optional = failing_query_optional  # type: ignore[method-assign]

        commands = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10")
        results = client.probe_commands(commands)
        self.assertEqual(len(results), 10)
        self.assertTrue(all(v is False for v in results.values()))
        self.assertEqual(client._consecutive_failures, 2)

    # ------------------------------------------------------------------
    # Push listener tests
    # ------------------------------------------------------------------

    async def test_listener_start_creates_thread(self) -> None:
        """Starting a listener creates a background thread."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128)
        mock_device = MagicMock()
        client._connect = lambda: mock_device  # type: ignore[assignment]
        client._read_message = lambda device, timeout: None  # type: ignore[method-assign]

        client.start_listener(lambda msg: None)
        try:
            self.assertIsNotNone(client._listener_thread)
            self.assertTrue(client._listener_thread.is_alive())
        finally:
            client.stop_listener()

    async def test_listener_stop_joins_thread(self) -> None:
        """Stopping a listener joins and clears the thread."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128)
        mock_device = MagicMock()
        client._connect = lambda: mock_device  # type: ignore[assignment]
        client._read_message = lambda device, timeout: None  # type: ignore[method-assign]

        client.start_listener(lambda msg: None)
        client.stop_listener()
        self.assertIsNone(client._listener_thread)

    async def test_listener_callback_receives_messages(self) -> None:
        """Listener thread delivers unsolicited ISCP messages to the callback."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128)
        mock_device = MagicMock()
        client._connect = lambda: mock_device  # type: ignore[assignment]
        messages = iter(["PWR01", None])
        def read_message(device, timeout):
            value = next(messages, None)
            if value is None:
                time.sleep(0.01)
            return value
        client._read_message = read_message  # type: ignore[method-assign]

        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=client,
            host="127.0.0.1",
            name="Test",
            update_interval=10,
        )

        client.start_listener(coordinator._handle_push_message)
        await asyncio.sleep(0.05)

        try:
            self.assertTrue(coordinator.data.power)
        finally:
            client.stop_listener()

    async def test_push_message_updates_power(self) -> None:
        """Direct _handle_push_message call updates power state."""
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128),
            host="127.0.0.1",
            name="Test",
            update_interval=10,
        )
        coordinator.data = self.module.OnkyoState(power=False)

        coordinator._handle_push_message("PWR01")
        await asyncio.sleep(0)

        self.assertTrue(coordinator.data.power)

    async def test_push_message_updates_volume(self) -> None:
        """Direct _handle_push_message call updates volume state."""
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128),
            host="127.0.0.1",
            name="Test",
            update_interval=10,
        )
        coordinator.data = self.module.OnkyoState(volume=0)

        coordinator._handle_push_message("MVL1A")
        await asyncio.sleep(0)

        self.assertEqual(coordinator.data.volume, 26)

    async def test_push_message_updates_mute(self) -> None:
        """Direct _handle_push_message call updates mute state."""
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128),
            host="127.0.0.1",
            name="Test",
            update_interval=10,
        )
        coordinator.data = self.module.OnkyoState(muted=False)

        coordinator._handle_push_message("AMT01")
        await asyncio.sleep(0)

        self.assertTrue(coordinator.data.muted)

    async def test_push_message_updates_source(self) -> None:
        """Direct _handle_push_message call updates source state."""
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128),
            host="127.0.0.1",
            name="Test",
            update_interval=10,
        )
        coordinator.data = self.module.OnkyoState(source=None)

        coordinator._handle_push_message("SLI02")
        await asyncio.sleep(0)

        self.assertEqual(coordinator.data.source, "video3")

    async def test_pushes_during_optional_poll_survive_refresh_per_field_and_zone(self) -> None:
        for zone_key in ("main", "zone2"):
            zone = self.module.ZONE_DEFINITIONS[zone_key]
            for suffix, field_name, expected in (
                ("power", "power", False),
                ("volume", "volume", 26),
                ("mute", "muted", True),
                ("source", "source", "video3"),
            ):
                with self.subTest(zone=zone_key, field=field_name):
                    client = FakeClient({
                        zone.power_command: zone.power_command + "01",
                        zone.volume_command: zone.volume_command + "10",
                        zone.mute_command: zone.mute_command + "00",
                        zone.source_command: zone.source_command + "00",
                        "LMD": "LMD00",
                    })
                    coordinator = self.module.OnkyoLegacyCoordinator(
                        self.hass, client=client, host="receiver", name="Test",
                        zone=zone, update_interval=10,
                    )
                    sibling_zone = self.module.ZONE_DEFINITIONS[
                        "zone2" if zone_key == "main" else "main"
                    ]
                    sibling = self.module.OnkyoLegacyCoordinator(
                        self.hass, client=client, host="receiver", name="Sibling",
                        zone=sibling_zone, update_interval=10,
                    )
                    sibling_before = sibling.data
                    coordinator.set_command_capability("LMD", True)
                    self.demand(coordinator, "LMD")
                    entered = threading.Event()
                    release = threading.Event()

                    def optional(command, timeout=1.5):
                        entered.set()
                        if not release.wait(2):
                            raise TimeoutError("test barrier was not released")
                        return "LMD00"

                    client.query_optional = optional
                    state = await coordinator._async_update_data()
                    coordinator.data = state
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                        self.assertEqual(coordinator.data.volume, 16)
                        command = getattr(zone, suffix + "_command")
                        value = {"power": "00", "volume": "1A", "mute": "01", "source": "02"}[suffix]
                        # The same wire push is dispatched to every active zone.
                        coordinator._handle_push_message(command + value)
                        sibling._handle_push_message(command + value)
                        await asyncio.sleep(0)
                        pushed_state = coordinator.data
                    finally:
                        release.set()
                    await coordinator._optional_task
                    self.assertEqual(getattr(coordinator.data, field_name), expected)
                    self.assertEqual(coordinator.data.power, False if field_name == "power" else True)
                    self.assertEqual(coordinator.data.volume, 26 if field_name == "volume" else 16)
                    self.assertEqual(coordinator.data.muted, field_name == "muted")
                    self.assertIs(sibling.data, sibling_before)
                    # A subsequent poll supersedes the earlier push.
                    later = await coordinator._async_update_data()
                    self.assertTrue(later.power)
                    self.assertEqual(later.volume, 16)
                    self.assertFalse(later.muted)
                    self.assertNotEqual(later.source, "video3")

    async def test_later_core_poll_supersedes_push_during_power_query(self) -> None:
        client = FakeClient({"PWR": "PWR01", "MVL": "MVL10", "AMT": "AMT00", "SLI": "SLI00"})
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        entered = threading.Event()
        release = threading.Event()
        original = client.query_batch

        def query_batch(commands):
            if commands == ("PWR",):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test barrier was not released")
            return original(commands)

        client.query_batch = query_batch
        task = asyncio.create_task(coordinator._async_update_data())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            coordinator._handle_push_message("MVL1A")
            coordinator._handle_push_message("PWR00")
            await asyncio.sleep(0)
        finally:
            release.set()
        state = await task
        self.assertEqual(state.volume, 16)
        self.assertFalse(state.power)

    async def test_standby_poll_preserves_power_push_and_dictionary_snapshots(self) -> None:
        client = FakeClient({"PWR": "PWR00"})
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="receiver", name="Test", update_interval=10,
        )
        before = self.module.OnkyoState(
            volume=20, audio_information={"format": "PCM"}, video_information={"resolution": "1080p"},
        )
        coordinator.data = before
        entered = threading.Event()
        release = threading.Event()

        def query_batch(commands):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test barrier was not released")
            return {"PWR": "PWR00"}

        client.query_batch = query_batch
        task = asyncio.create_task(coordinator._async_update_data())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            coordinator._handle_push_message("PWR01")
            await asyncio.sleep(0)
            pushed = coordinator.data
        finally:
            release.set()
        state = await task
        self.assertTrue(state.power)
        self.assertEqual(state.volume, 20)
        for name in ("audio_information", "video_information"):
            self.assertEqual(getattr(state, name), getattr(before, name))
            self.assertIsNot(getattr(pushed, name), getattr(before, name))
            self.assertIsNot(getattr(state, name), getattr(pushed, name))
            getattr(state, name)["new"] = "value"
            self.assertNotIn("new", getattr(before, name))
            self.assertNotIn("new", getattr(pushed, name))

    async def test_optional_state_dict_preserves_optional_fields(self) -> None:
        """Partial push updates preserve previously-set optional fields."""
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass,
            client=self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128),
            host="127.0.0.1",
            name="Test",
            update_interval=10,
        )
        coordinator.data = self.module.OnkyoState(
            power=True,
            volume=30,
            muted=False,
            source="dvd",
            listening_mode="ALL CH STEREO",
            dimmer_level="dim",
            audio_selector="auto",
        )

        # Send a volume push that only updates one core field
        coordinator._handle_push_message("MVL1A")
        await asyncio.sleep(0)

        self.assertEqual(coordinator.data.volume, 26)
        self.assertEqual(coordinator.data.listening_mode, "ALL CH STEREO")
        self.assertEqual(coordinator.data.dimmer_level, "dim")
        self.assertEqual(coordinator.data.audio_selector, "auto")

    async def test_disconnect_keeps_listener_and_close_stops_it(self) -> None:
        """Socket recovery leaves listener alive; close stops it."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128)
        mock_device = MagicMock()
        mock_device.disconnect.return_value = None
        client._connect = lambda: mock_device  # type: ignore[assignment]
        client._read_message = lambda device, timeout: None  # type: ignore[method-assign]

        client.start_listener(lambda msg: None)
        client.disconnect()
        self.assertIsNotNone(client._listener_thread)
        client.close()
        self.assertIsNone(client._listener_thread)

    async def test_framed_reader_handles_fragmented_header_and_body(self) -> None:
        reader, writer = socket.socketpair()
        device = type("Device", (), {"command_socket": reader})()
        payload = b"!1PWR01\x1a\r"
        frame = struct.pack("!4sIIB3s", b"ISCP", 16, len(payload), 1, b"\0\0\0") + payload

        def send_fragments():
            for part in (frame[:3], frame[3:16], frame[16:19], frame[19:]):
                writer.sendall(part)
                time.sleep(0.01)

        sender = threading.Thread(target=send_fragments)
        sender.start()
        try:
            message = self.module.OnkyoLegacyClient._read_message(device, 1.0)
            self.assertEqual(message, "PWR01")
        finally:
            sender.join()
            reader.close()
            writer.close()

    async def test_framed_reader_treats_eof_mid_body_as_connection_failure(self) -> None:
        reader, writer = socket.socketpair()
        device = type("Device", (), {"command_socket": reader})()
        header = struct.pack("!4sIIB3s", b"ISCP", 16, 8, 1, b"\0\0\0")
        writer.sendall(header + b"!1P")
        writer.close()
        try:
            with self.assertRaises(ConnectionError):
                self.module.OnkyoLegacyClient._read_message(device, 1.0)
        finally:
            reader.close()

    async def test_standby_refresh_sends_only_power_query_on_wire(self) -> None:
        client_socket, receiver_socket = socket.socketpair()
        client_socket.setblocking(False)

        class SocketDevice:
            command_socket = client_socket

            def disconnect(self) -> None:
                client_socket.close()

        client = self.module.OnkyoLegacyClient("socketpair", 1, retries=1)
        client._connect = lambda: SocketDevice()  # type: ignore[method-assign]
        coordinator = self.module.OnkyoLegacyCoordinator(
            self.hass, client=client, host="socketpair", name="Test", update_interval=10
        )
        coordinator.data = self.module.OnkyoState(
            power=True, volume=31, muted=True, source="cd", video_information={"video_input": "old"}
        )

        received: list[bytes] = []

        def receiver():
            header = receiver_socket.recv(16)
            size = struct.unpack("!4sIIB3s", header)[2]
            body = receiver_socket.recv(size)
            received.append(header + body)
            payload = b"!1PWR00\x1a\r"
            response = struct.pack("!4sIIB3s", b"ISCP", 16, len(payload), 1, b"\0\0\0") + payload
            receiver_socket.sendall(response)

        server = threading.Thread(target=receiver)
        server.start()
        try:
            state = await coordinator._async_update_data()
            server.join()
            self.assertEqual(len(received), 1)
            self.assertIn(b"PWRQSTN", received[0])
            self.assertFalse(state.power)
            self.assertEqual(state.volume, 31)
            self.assertEqual(state.source, "cd")
            self.assertEqual(state.video_information, {"video_input": "old"})
        finally:
            client.close()
            receiver_socket.close()

    async def test_query_serializes_listener_and_preserves_push_during_response(self) -> None:
        client_socket, receiver_socket = socket.socketpair()
        client_socket.setblocking(False)

        class SocketDevice:
            command_socket = client_socket
            def send(self, command: str) -> None:
                packet = self.module_packet(command)
                client_socket.sendall(packet)
            @staticmethod
            def module_packet(command: str) -> bytes:
                from eiscp.core import command_to_packet
                return command_to_packet(command)
            def disconnect(self) -> None:
                client_socket.close()

        device = SocketDevice()
        client = self.module.OnkyoLegacyClient("socketpair", 1, retries=1)
        client._connect = lambda: device  # type: ignore[method-assign]
        pushed: list[str] = []
        push = b"!1MVL1A\x1a\r"
        response = b"!1PWR01\x1a\r"
        push_frame = struct.pack("!4sIIB3s", b"ISCP", 16, len(push), 1, b"\0\0\0") + push
        response_frame = struct.pack("!4sIIB3s", b"ISCP", 16, len(response), 1, b"\0\0\0") + response

        push_seen = threading.Event()

        def on_push(message: str) -> None:
            pushed.append(message)
            push_seen.set()

        def receiver():
            receiver_socket.sendall(push_frame)
            receiver_socket.recv(1024)
            receiver_socket.sendall(push_frame + response_frame)

        server = threading.Thread(target=receiver)
        server.start()
        client.start_listener(on_push)
        try:
            self.assertTrue(await asyncio.to_thread(push_seen.wait, 1.0))
            self.assertEqual(client.query("PWR"), "PWR01")
            self.assertGreaterEqual(pushed.count("MVL1A"), 1)
        finally:
            server.join()
            receiver_socket.close()
            client.close()

    async def test_listener_push_recovery_survives_socket_reset(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128)
        device = MagicMock()
        client._connect = lambda: device  # type: ignore[method-assign]
        messages = iter([None, "PWR01", None])
        delivered = threading.Event()

        def read_message(_device, _timeout):
            message = next(messages, None)
            if message:
                delivered.set()
            else:
                time.sleep(0.01)
            return message

        client._read_message = read_message  # type: ignore[method-assign]
        received: list[str] = []
        client.start_listener(received.append)
        try:
            client.disconnect()
            self.assertTrue(await asyncio.to_thread(delivered.wait, 1.0))
            self.assertEqual(received, ["PWR01"])
            self.assertIsNotNone(client._listener_thread)
        finally:
            client.close()

    async def test_optional_timeout_and_transport_timeout_have_distinct_policy(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=1)
        resets: list[bool] = []
        client._reset_socket = lambda: resets.append(True)  # type: ignore[method-assign]
        client._query_once = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            self.module.CommandResponseTimeout("no matching response")
        )  # type: ignore[method-assign]
        with self.assertRaises(self.module.CommandResponseTimeout):
            client.query_optional("DIM")
        self.assertEqual(client._consecutive_failures, 0)
        self.assertEqual(resets, [])

        client._query_once = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("socket send timed out")
        )  # type: ignore[method-assign]
        with self.assertRaises(TimeoutError):
            client.query_optional("DIM")
        self.assertEqual(client._consecutive_failures, 1)
        self.assertEqual(resets, [True])

    async def test_listener_failure_reset_is_serialized_before_queued_query(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=1)
        read_started = threading.Event()
        release_read = threading.Event()
        query_waiting = threading.Event()
        operation_lock = threading.Lock()

        class ObservableLock:
            def __enter__(self):
                if threading.current_thread().name == "queued-query":
                    query_waiting.set()
                operation_lock.acquire()
                return self

            def __exit__(self, *_args):
                operation_lock.release()

            def acquire(self, _priority=True):
                if threading.current_thread().name == "queued-query":
                    query_waiting.set()
                operation_lock.acquire()

            def release(self):
                operation_lock.release()

        client._operation_lock = ObservableLock()
        disconnects: list[bool] = []
        device = MagicMock()
        device.disconnect.side_effect = lambda: disconnects.append(True)
        connects: list[bool] = []

        def connect():
            connects.append(True)
            return device

        def read_message(_device, _timeout):
            read_started.set()
            release_read.wait(1)
            raise OSError("reader failed")

        client._connect = connect  # type: ignore[method-assign]
        client._device = device
        client._read_message = read_message  # type: ignore[method-assign]
        client.start_listener(lambda _message: None)
        self.assertTrue(await asyncio.to_thread(read_started.wait, 1))
        query_errors: list[Exception] = []

        def query():
            try:
                client.query("PWR")
            except Exception as err:  # capture the queued cooldown rejection
                query_errors.append(err)

        query_thread = threading.Thread(target=query, name="queued-query")
        query_thread.start()
        self.assertTrue(await asyncio.to_thread(query_waiting.wait, 1))
        release_read.set()
        query_thread.join(timeout=1)
        try:
            self.assertFalse(query_thread.is_alive())
            self.assertEqual(connects, [True])
            self.assertEqual(disconnects, [True])
            self.assertEqual(len(query_errors), 1)
            self.assertIsInstance(query_errors[0], ConnectionError)
        finally:
            client.close()

    async def test_close_wakes_retry_wait_before_listener_starts(self) -> None:
        client = self.module.OnkyoLegacyClient("receiver", 60128, retries=2)
        failure_seen = threading.Event()
        wait_started = threading.Event()
        original_wait = client._wait_cooldown
        attempts: list[str] = []
        errors: list[Exception] = []

        def fail_query(command: str, timeout: float = 5.0) -> str:
            attempts.append(command)
            failure_seen.set()
            raise OSError("receiver unavailable")

        def wait_cooldown() -> None:
            wait_started.set()
            original_wait()

        client._query_once = fail_query  # type: ignore[method-assign]
        client._wait_cooldown = wait_cooldown  # type: ignore[method-assign]

        def query() -> None:
            try:
                client.query("PWR")
            except Exception as err:
                errors.append(err)

        worker = threading.Thread(target=query)
        worker.start()
        self.assertTrue(await asyncio.to_thread(failure_seen.wait, 1))
        self.assertTrue(await asyncio.to_thread(wait_started.wait, 1))
        client.begin_close()
        await asyncio.to_thread(worker.join, 1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(client._listener_stop.is_set())
        self.assertEqual(attempts, ["PWR"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConnectionError)
