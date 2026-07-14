"""Tests for diagnostics support."""

from __future__ import annotations

import json
import unittest

from tests.helpers import FakeHass, fresh_import


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.diag = fresh_import("custom_components.onkyo_legacy.diagnostics")
        self.coord_module = fresh_import("custom_components.onkyo_legacy.coordinator")
        self.hass = FakeHass()

    def _make_runtime(self) -> object:
        """Build minimal mock runtime for diagnostics."""
        state = self.coord_module.OnkyoState(power=True, volume=42, muted=False, source="dvd")
        client = type(
            "Client",
            (),
            {
                "_consecutive_failures": 0,
                "_circuit_open_until": 0.0,
            },
        )()
        coordinator = type(
            "Coordinator",
            (),
            {
                "data": state,
                "client": client,
            },
        )()
        zone = type("Zone", (), {"zone_key": "main"})()
        runtime = type(
            "Runtime",
            (),
            {
                "model": "PR-SC5507",
                "zones": [zone],
                "queryable_commands": ("PWR", "MVL", "AMT", "SLI"),
                "coordinator": coordinator,
            },
        )()
        return runtime

    async def test_diagnostics_returns_dict_with_expected_keys(self) -> None:
        runtime = self._make_runtime()
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        from homeassistant.config_entries import ConfigEntry

        entry = ConfigEntry(
            entry_id="entry-1", data={"host": "192.168.1.50", "port": 60128}
        )

        result = await self.diag.async_get_config_entry_diagnostics(self.hass, entry)

        self.assertIn("config_entry", result)
        self.assertIn("model", result)
        self.assertIn("zones", result)
        self.assertIn("queryable_commands", result)
        self.assertIn("coordinator_data", result)
        self.assertIn("circuit_breaker", result)
        self.assertEqual(result["model"], "PR-SC5507")
        self.assertEqual(result["zones"], ["main"])
        self.assertEqual(result["queryable_commands"], ["PWR", "MVL", "AMT", "SLI"])

    async def test_diagnostics_redacts_host_and_port(self) -> None:
        runtime = self._make_runtime()
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        from homeassistant.config_entries import ConfigEntry

        entry = ConfigEntry(
            entry_id="entry-1", data={"host": "192.168.1.50", "port": 60128}
        )

        result = await self.diag.async_get_config_entry_diagnostics(self.hass, entry)

        payload_str = json.dumps(result)
        self.assertNotIn("192.168.1.50", payload_str)
        self.assertNotIn("60128", payload_str)

    async def test_diagnostics_includes_queryable_commands(self) -> None:
        runtime = self._make_runtime()
        runtime.queryable_commands = (
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
        )
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        from homeassistant.config_entries import ConfigEntry

        entry = ConfigEntry(
            entry_id="entry-1", data={"host": "10.0.0.1", "port": 60128}
        )

        result = await self.diag.async_get_config_entry_diagnostics(self.hass, entry)

        self.assertIn("LMD", result["queryable_commands"])
        self.assertIn("ADV", result["queryable_commands"])
        self.assertGreater(len(result["queryable_commands"]), 5)
