from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock

from tests.helpers import FakeHass, fresh_import


class FakeClient:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.sent: list[tuple[str, str]] = []

    def query(self, command: str) -> str:
        response = self.responses.get(command)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise ValueError(f"missing response for {command}")
        return response

    def query_batch(self, commands: tuple[str, ...]) -> dict[str, str]:
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
        self.assertEqual(tuple(zone.zone_key for zone in runtime.candidate_zones), ())
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
        ))

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
        self.assertEqual(runtime.candidate_zones, ())

    async def test_update_data_parses_prsc5507_queryable_state(self) -> None:
        client = FakeClient(
            {
                "PWR": "PWR00",
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

        state = await coordinator._async_update_data()

        self.assertFalse(state.power)
        self.assertEqual(state.volume, 0)
        self.assertFalse(state.muted)
        self.assertEqual(state.source, "video3")
        self.assertEqual(state.audio_selector, "auto")
        self.assertEqual(state.listening_mode, "ALL CH STEREO")
        self.assertEqual(state.late_night_mode, "off")
        self.assertTrue(state.cinema_filter)
        self.assertTrue(state.audyssey_dynamic_eq)
        self.assertEqual(state.audyssey_dynamic_volume, "medium")
        self.assertTrue(state.music_optimizer)
        self.assertFalse(state.trigger_a)
        self.assertTrue(state.trigger_b)
        self.assertTrue(state.trigger_c)
        self.assertEqual(state.dimmer_level, "bright")
        self.assertEqual(state.sleep_minutes, 0)
        self.assertEqual(state.center_level, 0)
        self.assertEqual(state.subwoofer_level, 1)
        self.assertEqual(state.audio_information["input_terminal"], "HDMI 2")
        self.assertEqual(state.audio_information["listening_mode"], "ALL CH STEREO")
        self.assertEqual(state.video_information["video_output"], "ANALOG")
        self.assertEqual(state.video_information["picture_mode"], "CUSTOM")

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

        state = await coordinator._async_update_data()

        self.assertTrue(state.power)
        self.assertEqual(state.volume, 42)
        self.assertFalse(state.muted)
        self.assertEqual(state.source, "network")
        self.assertEqual(state.listening_mode, "stereo")
        self.assertEqual(state.dimmer_level, "dim")
        self.assertEqual(state.sleep_minutes, 30)
        self.assertEqual(state.tuner_frequency, 10710)

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
        client = self.module.OnkyoLegacyClient.__new__(self.module.OnkyoLegacyClient)
        client._host = "test"
        client._port = 60128
        client._device = None
        client._retries = 2
        client._consecutive_failures = 0
        client._circuit_open_until = 0.0
        from threading import Lock
        client._lock = Lock()

        class FakeDevice:
            def __init__(self, responses: dict[str, str]) -> None:
                self._responses = responses
            def raw(self, msg: str) -> str:
                cmd = msg[:3]
                return self._responses[cmd]
            def disconnect(self) -> None:
                pass

        device = FakeDevice({"PWR": "PWR01", "MVL": "MVL2A", "SLI": "SLI02"})
        client._device = device

        results = client.query_batch(("PWR", "MVL", "SLI"))

        self.assertEqual(results, {"PWR": "PWR01", "MVL": "MVL2A", "SLI": "SLI02"})

    async def test_query_batch_aborts_on_first_failure(self) -> None:
        client = self.module.OnkyoLegacyClient.__new__(self.module.OnkyoLegacyClient)
        client._host = "test"
        client._port = 60128
        client._device = None
        client._retries = 1
        client._consecutive_failures = 0
        client._circuit_open_until = 0.0
        from threading import Lock
        client._lock = Lock()

        class FailingDevice:
            def raw(self, msg: str) -> str:
                raise TimeoutError("no response")
            def disconnect(self) -> None:
                pass

        client._device = FailingDevice()

        results = client.query_batch(("PWR", "MVL", "SLI"))

        self.assertEqual(results, {})

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
        """After 4 failures and 1 success, _consecutive_failures should be 0."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128, retries=1)

        mock_device = MagicMock()
        call_count = 0

        def raw_side_effect(msg: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= 4:
                raise OSError("device busy")
            return "PWR01"

        mock_device.raw.side_effect = raw_side_effect
        mock_device.disconnect.return_value = None
        client._device = mock_device
        client._connect = lambda: mock_device

        for _ in range(4):
            with self.assertRaises(OSError):
                client.query("PWR")

        self.assertEqual(client._consecutive_failures, 4)

        result = client.query("PWR")
        self.assertEqual(result, "PWR01")
        self.assertEqual(client._consecutive_failures, 0)

    async def test_circuit_breaker_opens_after_consecutive_failures(self) -> None:
        """After 5 consecutive failures, _circuit_open_until should be set."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128, retries=1)

        mock_device = MagicMock()
        mock_device.raw.side_effect = OSError("device busy")
        mock_device.disconnect.return_value = None
        client._device = mock_device
        client._connect = lambda: mock_device

        before = time.monotonic()
        for _ in range(5):
            with self.assertRaises(OSError):
                client.query("PWR")

        self.assertEqual(client._consecutive_failures, 5)
        self.assertGreater(client._circuit_open_until, before)

    async def test_circuit_breaker_blocks_when_open(self) -> None:
        """When circuit is open, calls should raise ConnectionError immediately."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128, retries=1)
        client._circuit_open_until = time.monotonic() + 60

        with self.assertRaises(ConnectionError):
            client.send("MVL", "UP")

        with self.assertRaises(ConnectionError):
            client.query("PWR")

    async def test_circuit_breaker_recovers_after_cooldown(self) -> None:
        """After the 30s cooldown, calls should succeed again."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128, retries=1)
        client._circuit_open_until = time.monotonic() - 1  # just expired
        client._consecutive_failures = 5

        mock_device = MagicMock()
        mock_device.raw.return_value = "PWR01"
        mock_device.send.return_value = None
        mock_device.disconnect.return_value = None
        client._device = mock_device
        client._connect = lambda: mock_device

        result = client.query("PWR")
        self.assertEqual(result, "PWR01")
        self.assertEqual(client._consecutive_failures, 0)

        client.send("MVL", "UP")

    async def test_circuit_breaker_reopens_after_recovery_failure(self) -> None:
        """After cooldown, if the next call fails, circuit should re-open."""
        client = self.module.OnkyoLegacyClient(host="127.0.0.1", port=60128, retries=1)
        client._consecutive_failures = 5
        client._circuit_open_until = time.monotonic() - 1  # just expired

        mock_device = MagicMock()
        mock_device.raw.side_effect = OSError("device busy")
        mock_device.disconnect.return_value = None
        client._device = mock_device
        client._connect = lambda: mock_device

        before = time.monotonic()
        with self.assertRaises(OSError):
            client.query("PWR")

        self.assertEqual(client._consecutive_failures, 6)
        self.assertGreater(client._circuit_open_until, before)

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

    # ------------------------------------------------------------------
    # probe_commands tests
    # ------------------------------------------------------------------

    async def test_probe_commands_marks_supported(self) -> None:
        client = self.module.OnkyoLegacyClient.__new__(self.module.OnkyoLegacyClient)
        client._host = "test"
        client._port = 60128
        client._device = None
        client._retries = 1
        client._consecutive_failures = 0
        client._circuit_open_until = 0.0
        from threading import Lock
        client._lock = Lock()

        def mock_query_once(command: str) -> str:
            if command == "PWR":
                return "PWR01"
            raise OSError("no response")

        client._query_once = mock_query_once  # type: ignore[assignment]

        results = client.probe_commands(("PWR", "XYZ"))
        self.assertEqual(results, {"PWR": True, "XYZ": False})

    async def test_probe_commands_does_not_trip_circuit_breaker(self) -> None:
        client = self.module.OnkyoLegacyClient.__new__(self.module.OnkyoLegacyClient)
        client._host = "test"
        client._port = 60128
        client._device = None
        client._retries = 1
        client._consecutive_failures = 2
        client._circuit_open_until = 0.0
        from threading import Lock
        client._lock = Lock()

        def failing_query_once(command: str) -> str:
            raise OSError("no response")

        client._query_once = failing_query_once  # type: ignore[assignment]

        commands = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10")
        results = client.probe_commands(commands)
        self.assertEqual(len(results), 10)
        self.assertTrue(all(v is False for v in results.values()))
        self.assertEqual(client._consecutive_failures, 2)
