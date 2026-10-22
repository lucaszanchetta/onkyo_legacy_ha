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
        coordinator._optional_failures["LMD"] = (3, time.monotonic() + 3600)
        coordinator._optional_failures["DIM"] = (3, time.monotonic() + 3600)

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
