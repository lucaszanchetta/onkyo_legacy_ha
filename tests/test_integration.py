from __future__ import annotations

import sys
import unittest

from tests.helpers import FakeHass, fresh_import


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.client = type("Client", (), {"disconnect": lambda self: None})()

    async def async_request_refresh(self) -> None:
        self.calls.append(("refresh", ""))

    async def async_set_listening_mode(self, mode: str) -> None:
        self.calls.append(("LMD", mode))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.init_module = fresh_import("custom_components.onkyo_legacy")
        self.config_flow_module = fresh_import("custom_components.onkyo_legacy.config_flow")
        self.hass = FakeHass()

    async def test_yaml_setup_imports_config_entries(self) -> None:
        config = {
            "onkyo_legacy": [
                {
                    "host": "192.168.1.23",
                    "port": 60128,
                    "name": "Onkyo PR-SC5507",
                    "model": "PR-SC5507",
                    "scan_interval": 10,
                    "max_volume": 80,
                    "sources": {"HTPC": "vcr"},
                }
            ]
        }

        result = await self.init_module.async_setup(self.hass, config)
        if self.hass.created_tasks:
            await self.hass.created_tasks[0]

        self.assertTrue(result)
        self.assertEqual(len(self.hass.config_entries.init_calls), 1)
        domain, context, data = self.hass.config_entries.init_calls[0]
        self.assertEqual(domain, "onkyo_legacy")
        self.assertEqual(context, {"source": "import"})
        self.assertEqual(data["host"], "192.168.1.23")

    async def test_config_flow_import_creates_entry(self) -> None:
        flow = self.config_flow_module.OnkyoLegacyConfigFlow()

        result = await flow.async_step_import(
            {
                "host": "192.168.1.23",
                "port": 60128,
                "name": "Onkyo PR-SC5507",
                "model": "PR-SC5507",
                "scan_interval": 10,
                "max_volume": 80,
                "sources": {"HTPC": "vcr"},
            }
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], "Onkyo PR-SC5507")
        self.assertEqual(result["data"]["host"], "192.168.1.23")

    async def test_config_flow_uses_model_default_name(self) -> None:
        flow = self.config_flow_module.OnkyoLegacyConfigFlow()

        result = await flow.async_step_import(
            {
                "host": "192.168.1.14",
                "port": 60128,
                "model": "TX-8050",
                "scan_interval": 10,
                "max_volume": 80,
                "sources": {"Tuner": "tuner"},
            }
        )

        self.assertEqual(result["title"], "Onkyo TX-8050")
        self.assertEqual(result["data"]["model"], "TX-8050")

    async def test_main_entity_registry_migration_prefers_current_unique_ids(self) -> None:
        entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
        registry = entity_registry.async_get(self.hass)
        registry.add(
            "select",
            "onkyo_legacy",
            "192.168.1.23-source",
            "select.onkyo_pr_sc5507_source",
        )
        registry.add(
            "select",
            "onkyo_legacy",
            "192.168.1.23-main-source",
            "select.theater_source",
        )

        await self.init_module._async_migrate_main_entity_unique_ids(self.hass, "192.168.1.23")

        self.assertIsNone(
            registry.async_get_entity_id("select", "onkyo_legacy", "192.168.1.23-source")
        )
        self.assertEqual(
            registry.async_get_entity_id("select", "onkyo_legacy", "192.168.1.23-main-source"),
            "select.theater_source",
        )

    async def test_main_entity_registry_migration_reclaims_unsuffixed_entity_ids(self) -> None:
        entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
        registry = entity_registry.async_get(self.hass)
        registry.add(
            "select",
            "onkyo_legacy",
            "192.168.1.23-main-source",
            "select.onkyo_pr_sc5507_source_2",
        )

        await self.init_module._async_migrate_main_entity_unique_ids(self.hass, "192.168.1.23")

        self.assertEqual(
            registry.async_get_entity_id("select", "onkyo_legacy", "192.168.1.23-main-source"),
            "select.onkyo_pr_sc5507_source",
        )

    async def test_stale_zone_switch_registry_entries_are_removed(self) -> None:
        entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
        registry = entity_registry.async_get(self.hass)
        registry.add(
            "switch",
            "onkyo_legacy",
            "192.168.1.23-zone2-power",
            "switch.onkyo_pr_sc5507_zone_2_power",
        )
        registry.add(
            "switch",
            "onkyo_legacy",
            "192.168.1.23-zone3-mute",
            "switch.onkyo_pr_sc5507_zone_3_mute",
        )

        await self.init_module._async_remove_stale_entity_registry_entries(
            self.hass, "192.168.1.23"
        )

        self.assertIsNone(
            registry.async_get_entity_id("switch", "onkyo_legacy", "192.168.1.23-zone2-power")
        )
        self.assertIsNone(
            registry.async_get_entity_id("switch", "onkyo_legacy", "192.168.1.23-zone3-mute")
        )


    async def test_setup_entry_does_not_fail_when_initial_refresh_times_out(self) -> None:
        entry = self.init_module.ConfigEntry(
            entry_id="entry-1",
            data={
                "host": "192.168.1.23",
                "port": 60128,
                "name": "Onkyo PR-SC5507",
                "model": "PR-SC5507",
                "scan_interval": 10,
                "max_volume": 80,
                "sources": {"HTPC": "vcr"},
            },
        )

        result = await self.init_module.async_setup_entry(self.hass, entry)

        self.assertTrue(result)
        runtime = self.hass.data["onkyo_legacy"][entry.entry_id]
        self.assertEqual(runtime.coordinator.command_capabilities["LMD"], True)
        self.assertGreaterEqual(len(self.hass.created_tasks), 1)

        refresh_task = self.hass.created_tasks[0]
        await refresh_task
        self.assertFalse(runtime.coordinator.last_update_success)

    async def test_services_registration_and_routing(self) -> None:
        runtime = type("Runtime", (), {})()
        runtime.entity_ids = {"media_player.onkyo_pr_sc5507"}
        runtime.supported_listening_modes = ["stereo", "all-ch-stereo"]
        runtime.coordinator = FakeCoordinator()
        zone2 = type("ZoneRuntime", (), {"coordinator": FakeCoordinator()})()
        runtime.zones = (runtime, zone2)
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}

        await self.init_module._async_register_services(self.hass)
        await self.hass.services.async_call(
            "onkyo_legacy",
            "set_listening_mode",
            {"entity_id": "media_player.onkyo_pr_sc5507", "listening_mode": "stereo"},
        )
        await self.hass.services.async_call(
            "onkyo_legacy",
            "refresh",
            {"entity_id": "media_player.onkyo_pr_sc5507"},
        )

        calls = runtime.coordinator.calls
        self.assertIn(("LMD", "stereo"), calls)
        self.assertIn(("refresh", ""), calls)
        self.assertIn(("refresh", ""), zone2.coordinator.calls)
