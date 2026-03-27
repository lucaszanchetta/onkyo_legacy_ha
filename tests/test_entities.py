from __future__ import annotations

import unittest

from tests.helpers import FakeHass, fresh_import


class EntityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.coordinator_module = fresh_import("custom_components.onkyo_legacy.coordinator")
        self.media_player_module = fresh_import("custom_components.onkyo_legacy.media_player")
        self.select_module = fresh_import("custom_components.onkyo_legacy.select")
        self.switch_module = fresh_import("custom_components.onkyo_legacy.switch")
        self.number_module = fresh_import("custom_components.onkyo_legacy.number")
        self.sensor_module = fresh_import("custom_components.onkyo_legacy.sensor")
        self.hass = FakeHass()

    def _runtime(self):
        runtime = self.coordinator_module.build_runtime_data(
            self.hass,
            host="192.168.1.23",
            port=60128,
            name="Onkyo PR-SC5507",
            model="PR-SC5507",
            scan_interval=10,
            sources={"HTPC": "vcr", "Blu-ray": "dvd"},
            max_volume=80,
        )
        runtime.coordinator.data = self.coordinator_module.OnkyoState(
            power=True,
            volume=40,
            muted=False,
            source="video1",
            audio_selector="auto",
            listening_mode="DOLBY PLII MUSIC",
            late_night_mode="off",
            cinema_filter=False,
            audyssey_dynamic_eq=True,
            audyssey_dynamic_volume="medium",
            music_optimizer=True,
            trigger_a=False,
            trigger_b=True,
            trigger_c=True,
            dimmer_level="bright",
            sleep_minutes=0,
            center_level=0,
            subwoofer_level=1,
            audio_information={
                "input_terminal": "HDMI 2",
                "input_signal": "PCM",
                "sampling_frequency": "48kHz",
                "input_channels": "2.0ch",
                "listening_mode": "All Ch Stereo",
            },
            video_information={
                "video_input": "V(VCR/DVR)",
                "video_output": "Analog",
                "output_resolution": "720x480i  60Hz",
                "picture_mode": "Custom",
            },
        )
        runtime.coordinator.last_update_success = True
        for command in (
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
        ):
            runtime.coordinator.set_command_capability(command, True)
        zone2 = runtime.candidate_zones[0]
        zone2.coordinator.data = self.coordinator_module.OnkyoState(
            power=False,
            volume=22,
            muted=True,
            source="dvd",
        )
        zone2.coordinator.last_update_success = True
        zone3 = runtime.candidate_zones[1]
        zone3.coordinator.data = self.coordinator_module.OnkyoState(
            power=True,
            volume=18,
            muted=False,
            source="video1",
        )
        zone3.coordinator.last_update_success = True
        runtime.zones = (runtime, zone2, zone3)
        return runtime

    def _tx8050_runtime(self):
        runtime = self.coordinator_module.build_runtime_data(
            self.hass,
            host="192.168.1.14",
            port=60128,
            name="Onkyo TX-8050",
            model="TX-8050",
            scan_interval=10,
            sources={"Tuner": "tuner", "CD": "cd", "Network": "net"},
            max_volume=80,
        )
        runtime.coordinator.data = self.coordinator_module.OnkyoState(
            power=True,
            volume=30,
            muted=True,
            source="net",
            listening_mode="stereo",
            dimmer_level="bright",
            sleep_minutes=15,
            tuner_frequency=10710,
        )
        runtime.coordinator.last_update_success = True
        for command in ("LMD", "DIM", "SLP", "TUN"):
            runtime.coordinator.set_command_capability(command, True)
        return runtime

    async def test_media_player_uses_shared_device_info_and_source_mapping(self) -> None:
        runtime = self._runtime()
        entity = self.media_player_module.OnkyoLegacyMediaPlayer(runtime)
        entity.entity_id = "media_player.onkyo_pr_sc5507"
        await entity.async_added_to_hass()

        self.assertEqual(entity._attr_device_info, runtime.device_info)
        self.assertEqual(entity._attr_icon, "mdi:audio-video")
        self.assertEqual(entity.source, "VIDEO1")
        self.assertEqual(entity.source_list, ["VIDEO1", "DVD -- BD/DVD"])
        self.assertAlmostEqual(entity.volume_level, 0.5)

    async def test_select_setup_creates_theater_select_entities(self) -> None:
        runtime = self._runtime()
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        entities: list[object] = []
        counter = {"value": 0}
        entry = type("Entry", (), {"entry_id": "entry-1", "data": {}})()

        def capture(new_entities):
            for entity in new_entities:
                entity.entity_id = f"test.entity_{counter['value']}"
                counter["value"] += 1
                entities.append(entity)

        await self.select_module.async_setup_entry(
            self.hass,
            entry,
            capture,
        )

        names = {entity._attr_name for entity in entities}
        self.assertEqual(
            names,
            {
                "Source",
                "Zone 2 Source",
                "Zone 3 Source",
                "Listening Mode",
                "Dimmer Level",
                "Audio Selector",
                "Late Night",
                "Audyssey Dynamic Volume",
            },
        )
        for entity in entities:
            self.assertEqual(entity._attr_device_info, runtime.device_info)
        icons = {entity._attr_name: entity._attr_icon for entity in entities}
        self.assertEqual(icons["Source"], "mdi:source-branch")
        self.assertEqual(icons["Listening Mode"], "mdi:surround-sound")
        self.assertEqual(icons["Dimmer Level"], "mdi:brightness-6")
        self.assertEqual(icons["Audio Selector"], "mdi:audio-input-rca")

    async def test_listening_mode_select_includes_live_current_value(self) -> None:
        runtime = self._runtime()
        entity = self.select_module.OnkyoLegacyListeningModeSelect(runtime)

        self.assertEqual(entity.current_option, "DOLBY PLII MUSIC")
        self.assertIn("DOLBY PLII MUSIC", entity.options)
        self.assertIn("plii", entity.options)
        self.assertNotIn("query", entity.options)

    async def test_switch_setup_creates_binary_controls_for_supported_commands(self) -> None:
        runtime = self._runtime()
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        entities: list[object] = []
        counter = {"value": 0}
        entry = type("Entry", (), {"entry_id": "entry-1", "data": {}})()

        def capture(new_entities):
            for entity in new_entities:
                entity.entity_id = f"test.entity_{counter['value']}"
                counter["value"] += 1
                entities.append(entity)

        await self.switch_module.async_setup_entry(
            self.hass,
            entry,
            capture,
        )

        names = {entity._attr_name for entity in entities}
        self.assertEqual(
            names,
            {
                "Power",
                "Mute",
                "Cinema Filter",
                "Audyssey Dynamic EQ",
                "Music Optimizer",
                "12V Trigger A",
                "12V Trigger B",
                "12V Trigger C",
            },
        )
        icons = {entity._attr_name: entity._attr_icon for entity in entities}
        self.assertEqual(icons["Power"], "mdi:power")
        self.assertEqual(icons["Mute"], "mdi:volume-mute")
        self.assertEqual(icons["Cinema Filter"], "mdi:movie-filter")
        self.assertEqual(icons["Audyssey Dynamic EQ"], "mdi:equalizer")
        self.assertEqual(icons["Music Optimizer"], "mdi:music")

    async def test_number_setup_creates_sleep_and_level_entities(self) -> None:
        runtime = self._runtime()
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        entities: list[object] = []
        counter = {"value": 0}
        entry = type("Entry", (), {"entry_id": "entry-1", "data": {}})()

        def capture(new_entities):
            for entity in new_entities:
                entity.entity_id = f"test.entity_{counter['value']}"
                counter["value"] += 1
                entities.append(entity)

        await self.number_module.async_setup_entry(
            self.hass,
            entry,
            capture,
        )

        names = {entity._attr_name for entity in entities}
        self.assertEqual(
            names,
            {
                "Volume Level",
                "Zone 2 Volume Level",
                "Zone 3 Volume Level",
                "Sleep Timer",
                "Center Temporary Level",
                "Subwoofer Temporary Level",
            },
        )
        for entity in entities:
            self.assertEqual(entity._attr_device_info, runtime.device_info)
        icons = {entity._attr_name: entity._attr_icon for entity in entities}
        self.assertEqual(icons["Volume Level"], "mdi:volume-high")
        self.assertEqual(icons["Sleep Timer"], "mdi:sleep")
        self.assertEqual(icons["Center Temporary Level"], "mdi:speaker-center")
        self.assertEqual(icons["Subwoofer Temporary Level"], "mdi:subwoofer")

    async def test_tx8050_setup_creates_audio_only_separate_controls(self) -> None:
        runtime = self._tx8050_runtime()

        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        entry = type("Entry", (), {"entry_id": "entry-1", "data": {}})()

        select_entities: list[object] = []
        number_entities: list[object] = []
        switch_entities: list[object] = []
        sensor_entities: list[object] = []
        counter = {"value": 0}

        def capture(target):
            def _capture(new_entities):
                for entity in new_entities:
                    entity.entity_id = f"test.entity_{counter['value']}"
                    counter["value"] += 1
                    target.append(entity)

            return _capture

        await self.select_module.async_setup_entry(self.hass, entry, capture(select_entities))
        await self.number_module.async_setup_entry(self.hass, entry, capture(number_entities))
        await self.switch_module.async_setup_entry(self.hass, entry, capture(switch_entities))
        await self.sensor_module.async_setup_entry(self.hass, entry, capture(sensor_entities))

        self.assertEqual(
            {entity._attr_name for entity in select_entities},
            {"Source", "Listening Mode", "Dimmer Level"},
        )
        self.assertEqual(
            {entity._attr_name for entity in number_entities},
            {"Volume Level", "Sleep Timer"},
        )
        self.assertEqual({entity._attr_name for entity in switch_entities}, {"Power", "Mute"})
        self.assertEqual({entity._attr_name for entity in sensor_entities}, {"Tuner Frequency"})

    async def test_tx8050_media_player_uses_receiver_source_names(self) -> None:
        runtime = self._tx8050_runtime()
        entity = self.media_player_module.OnkyoLegacyMediaPlayer(runtime)

        self.assertEqual(entity.source, "NETWORK")
        self.assertEqual(entity.source_list, ["TUNER", "CD -- TV/CD", "NETWORK"])

    async def test_sensor_setup_creates_audio_and_video_diagnostics(self) -> None:
        runtime = self._runtime()
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        entities: list[object] = []
        counter = {"value": 0}
        entry = type("Entry", (), {"entry_id": "entry-1", "data": {}})()

        def capture(new_entities):
            for entity in new_entities:
                entity.entity_id = f"test.entity_{counter['value']}"
                counter["value"] += 1
                entities.append(entity)

        await self.sensor_module.async_setup_entry(
            self.hass,
            entry,
            capture,
        )

        names = {entity._attr_name for entity in entities}
        self.assertIn("Audio Input Terminal", names)
        self.assertIn("Audio Listening Mode", names)
        self.assertIn("Video Output Resolution", names)
        self.assertIn("Video Picture Mode", names)
        icons = {entity._attr_name: entity._attr_icon for entity in entities}
        self.assertEqual(icons["Audio Input Terminal"], "mdi:input-hdmi")
        self.assertEqual(icons["Audio Listening Mode"], "mdi:surround-sound")
        self.assertEqual(icons["Video Output Resolution"], "mdi:monitor")
