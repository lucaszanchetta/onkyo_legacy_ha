from __future__ import annotations

import unittest

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
        self.assertEqual(runtime.candidate_zones[0].sources["VIDEO1"], "video1")
        self.assertEqual(runtime.candidate_zones[0].sources["DVD -- BD/DVD"], "dvd")
        self.assertEqual(runtime.candidate_zones[0].max_volume, 100)
        self.assertEqual(runtime.candidate_zones[1].max_volume, 100)
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
            sources=self.module.PROFILE_DEFAULT_SOURCES["TX-8050"],
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
        self.assertEqual(runtime.candidate_zones[0].sources, {"DVD -- BD/DVD": "dvd", "TV": "tv"})
        self.assertEqual(runtime.candidate_zones[1].sources, {"DVD -- BD/DVD": "dvd", "TV": "tv"})

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
