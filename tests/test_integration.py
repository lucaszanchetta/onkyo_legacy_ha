from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from unittest.mock import patch

from tests.helpers import FakeHass, fresh_import


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, command: str, value: str) -> None:
        self.sent.append((command, value))

    def disconnect(self) -> None:
        pass

    def start_listener(self, callback) -> None:
        self.listener_callback = callback

    def close(self) -> None:
        pass

    def begin_close(self) -> None:
        self._closing = True


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.client = FakeClient()
        self.last_update_success = False

    def set_command_capability(self, command: str, supported: bool) -> None:
        self.capabilities = getattr(self, "capabilities", {})
        self.capabilities[command] = supported

    async def async_refresh(self) -> None:
        from homeassistant.helpers.update_coordinator import UpdateFailed
        raise UpdateFailed("offline fake")

    async def async_request_refresh(self) -> None:
        self.calls.append(("refresh", ""))

    async def async_set_listening_mode(self, mode: str) -> None:
        self.calls.append(("LMD", mode))

    async def async_select_source(self, source: str) -> None:
        self.calls.append(("select_source", source))

    async def async_set_volume(self, value: float, max_volume: int) -> None:
        target = round(value * max_volume)
        self.client.sent.append(("MVL", f"{target:02X}"))
        self.calls.append(("refresh", ""))

    async def async_set_dimmer_level(self, level: str) -> None:
        self.client.sent.append(("DIM", level))
        self.calls.append(("refresh", ""))


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

    async def test_connection_validation_queries_power_with_unprefixed_command(self) -> None:
        class Client:
            def __init__(self, host, port, retries):
                self.queries = []
                self.disconnected = False

            def query(self, command):
                self.queries.append(command)
                return "PWR01"

            def disconnect(self):
                self.disconnected = True

        clients = []

        def make_client(*args, **kwargs):
            client = Client(*args, **kwargs)
            clients.append(client)
            return client

        with patch.object(self.config_flow_module, "OnkyoLegacyClient", side_effect=make_client):
            self.config_flow_module._validate_connection("192.0.2.10", 60128)

        self.assertEqual(clients[0].queries, ["PWR"])
        self.assertTrue(clients[0].disconnected)

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

    async def test_unsupported_zone_registry_entries_are_removed(self) -> None:
        entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
        registry = entity_registry.async_get(self.hass)
        registry.add(
            "media_player",
            "onkyo_legacy",
            "192.168.1.23-zone2",
            "media_player.onkyo_pr_sc5507_zone_2",
        )
        registry.add(
            "number",
            "onkyo_legacy",
            "192.168.1.23-zone2-volume-level",
            "number.onkyo_pr_sc5507_zone_2_volume_level",
        )
        registry.add(
            "select",
            "onkyo_legacy",
            "192.168.1.23-zone2-source",
            "select.onkyo_pr_sc5507_zone_2_source",
        )
        runtime = type("Runtime", (), {})()
        runtime.host = "192.168.1.23"
        runtime.zones = (type("MainRuntime", (), {"zone_key": "main"})(),)

        await self.init_module._async_remove_unsupported_zone_registry_entries(
            self.hass, runtime
        )

        self.assertIsNone(
            registry.async_get_entity_id("media_player", "onkyo_legacy", "192.168.1.23-zone2")
        )
        self.assertIsNone(
            registry.async_get_entity_id(
                "number", "onkyo_legacy", "192.168.1.23-zone2-volume-level"
            )
        )
        self.assertIsNone(
            registry.async_get_entity_id("select", "onkyo_legacy", "192.168.1.23-zone2-source")
        )


    async def test_setup_entry_does_not_fail_when_initial_refresh_times_out(self) -> None:
        async def fake_detect(runtime):
            return (runtime,)

        self.init_module._async_detect_supported_zones = fake_detect
        runtime = type("Runtime", (), {})()
        runtime.host = "192.0.2.10"
        runtime.port = 60128
        runtime.zone_key = "main"
        runtime.zones = (runtime,)
        runtime.candidate_zones = ()
        runtime.queryable_commands = ("LMD",)
        runtime.coordinator = FakeCoordinator()
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

        with patch.object(self.init_module, "build_runtime_data", return_value=runtime):
            result = await self.init_module.async_setup_entry(self.hass, entry)

        self.assertTrue(result)
        runtime = self.hass.data["onkyo_legacy"][entry.entry_id]
        self.assertEqual(runtime.coordinator.capabilities["LMD"], True)
        self.assertGreaterEqual(len(self.hass.created_tasks), 1)
        refresh_task = self.hass.created_tasks[0]
        await refresh_task
        self.assertFalse(runtime.coordinator.last_update_success)

    async def test_failed_platform_unload_keeps_runtime_and_client_open(self) -> None:
        runtime = type("Runtime", (), {})()
        client = type("Client", (), {
            "close": lambda self: self._closed.append(True),
            "begin_close": lambda self: None,
        })()
        client._closed = []
        runtime.coordinator = type("Coordinator", (), {"client": client})()
        runtime.initial_refresh_task = None
        runtime.stop_listener_unsub = lambda: None
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        self.hass.config_entries.async_unload_platforms = lambda *_: asyncio.sleep(0, result=False)
        entry = self.init_module.ConfigEntry(entry_id="entry-1", data={})

        result = await self.init_module.async_unload_entry(self.hass, entry)

        self.assertFalse(result)
        self.assertIs(self.hass.data["onkyo_legacy"]["entry-1"], runtime)
        self.assertEqual(client._closed, [])

    async def test_successful_unload_closes_client_in_executor(self) -> None:
        runtime = type("Runtime", (), {})()
        client = type("Client", (), {
            "close": lambda self: self._closed.append(True),
            "begin_close": lambda self: None,
        })()
        client._closed = []
        runtime.coordinator = type("Coordinator", (), {"client": client})()
        runtime.initial_refresh_task = None
        runtime.stop_listener_unsub = lambda: None
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        entry = self.init_module.ConfigEntry(entry_id="entry-1", data={})

        result = await self.init_module.async_unload_entry(self.hass, entry)

        self.assertTrue(result)
        self.assertNotIn("entry-1", self.hass.data["onkyo_legacy"])
        self.assertEqual(client._closed, [True])

    async def test_cancelled_unload_waits_for_queued_executor_close(self) -> None:
        runtime = type("Runtime", (), {})()
        close_started = threading.Event()
        release_close = threading.Event()
        close_ran = threading.Event()

        class Client:
            def begin_close(self):
                self.closing = True

            def close(self):
                close_ran.set()

        client = Client()
        runtime.coordinator = type("Coordinator", (), {"client": client})()
        runtime.initial_refresh_task = None
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}

        def queued_executor(func, *args):
            close_started.set()
            release_close.wait()
            func(*args)

        self.hass.async_add_executor_job = lambda func, *args: self.hass.loop.run_in_executor(
            None, queued_executor, func, *args
        )
        entry = self.init_module.ConfigEntry(entry_id="entry-1", data={})
        unload_task = asyncio.create_task(self.init_module.async_unload_entry(self.hass, entry))
        self.assertTrue(await asyncio.to_thread(close_started.wait, 1))
        self.assertNotIn("entry-1", self.hass.data["onkyo_legacy"])
        unload_task.cancel()
        release_close.set()
        with self.assertRaises(asyncio.CancelledError):
            await unload_task
        self.assertTrue(close_ran.is_set())

    async def test_cancelled_unload_waits_for_initial_refresh_cleanup(self) -> None:
        runtime = type("Runtime", (), {})()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        close_ran = threading.Event()

        class Client:
            def begin_close(self):
                self.closing = True

            def close(self):
                close_ran.set()

        async def initial_refresh():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleanup_started.set()
                await release_cleanup.wait()

        task = asyncio.create_task(initial_refresh())
        runtime.initial_refresh_task = task
        runtime.coordinator = type(
            "Coordinator", (), {"client": Client()}
        )()
        runtime.stop_listener_unsub = lambda: None
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        self.hass.async_add_executor_job = lambda func, *args: self.hass.loop.run_in_executor(
            None, func, *args
        )
        entry = self.init_module.ConfigEntry(entry_id="entry-1", data={})
        unload_task = asyncio.create_task(self.init_module.async_unload_entry(self.hass, entry))
        await cleanup_started.wait()
        unload_task.cancel()
        release_cleanup.set()
        with self.assertRaises(asyncio.CancelledError):
            await unload_task
        self.assertTrue(close_ran.is_set())
        self.assertTrue(task.done())

    async def test_setup_cancellation_during_probe_closes_client(self) -> None:
        started = threading.Event()
        release = threading.Event()
        runtime = type("Runtime", (), {})()
        runtime.host = "192.0.2.10"
        runtime.port = 60128
        runtime.zone_key = "main"
        runtime.zones = (runtime,)
        runtime.candidate_zones = ()
        runtime.queryable_commands = ("LMD",)
        runtime.coordinator = FakeCoordinator()
        client = runtime.coordinator.client

        def blocked_power_query(command, timeout=1.5):
            started.set()
            release.wait(1.0)
            return "PWR01"

        client.query_optional = blocked_power_query
        self.init_module._async_detect_supported_zones = lambda _runtime: asyncio.sleep(0, result=(runtime,))
        entry = self.init_module.ConfigEntry(
            entry_id="cancel-probe",
            data={
                "host": "192.0.2.10",
                "port": 60128,
                "name": "Generic Receiver",
                "model": "UNKNOWN-MODEL",
                "scan_interval": 10,
                "max_volume": 80,
                "sources": {},
            },
        )
        with patch.object(self.init_module, "build_runtime_data", return_value=runtime):
            task = asyncio.create_task(self.init_module.async_setup_entry(self.hass, entry))
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(getattr(client, "_closing", False))
        self.assertNotIn(entry.entry_id, self.hass.data["onkyo_legacy"])

    async def test_setup_cancellation_during_forwarding_cleans_executor_future(self) -> None:
        runtime = type("Runtime", (), {})()
        runtime.host = "192.0.2.10"
        runtime.port = 60128
        runtime.zone_key = "main"
        runtime.zones = (runtime,)
        runtime.candidate_zones = ()
        runtime.queryable_commands = ()
        runtime.coordinator = FakeCoordinator()
        self.init_module._async_detect_supported_zones = lambda _runtime: asyncio.sleep(0, result=(runtime,))
        async def cancel_forward(_entry, _platforms):
            raise asyncio.CancelledError
        self.hass.config_entries.async_forward_entry_setups = cancel_forward
        self.hass.async_add_executor_job = lambda func, *args: self.hass.loop.run_in_executor(None, func, *args)
        entry = self.init_module.ConfigEntry(
            entry_id="cancel-forward",
            data={
                "host": "192.0.2.10",
                "port": 60128,
                "name": "Known Receiver",
                "model": "PR-SC5507",
                "scan_interval": 10,
                "max_volume": 80,
                "sources": {},
            },
        )
        with patch.object(self.init_module, "build_runtime_data", return_value=runtime):
            with self.assertRaises(asyncio.CancelledError):
                await self.init_module.async_setup_entry(self.hass, entry)

        self.assertNotIn(entry.entry_id, self.hass.data["onkyo_legacy"])
        self.assertTrue(getattr(runtime.coordinator.client, "_closing", False))

    async def test_detect_supported_zones_returns_main_only_without_candidates(self) -> None:
        class ProbeCoordinator:
            def __init__(self, should_fail: bool) -> None:
                self.should_fail = should_fail
                self.hass = self_outer.hass

            def _query_core_state(self) -> tuple[str, str, str, str]:
                if self.should_fail:
                    raise TimeoutError("no response")
                return ("PWR01", "MVL00", "AMT00", "SLI00")

        self_outer = self
        runtime = type("Runtime", (), {})()
        runtime.zone_label = "Main"
        runtime.host = "192.168.1.23"
        runtime.port = 60128
        runtime.candidate_zones = ()

        zones = await self.init_module._async_detect_supported_zones(runtime)

        self.assertEqual([zone.zone_label for zone in zones], ["Main"])

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

    async def test_set_source_service_calls_select_source(self) -> None:
        """set_source service calls coordinator.async_select_source with the mapped eISCP alias."""
        runtime = type("Runtime", (), {})()
        runtime.entity_ids = {"media_player.onkyo_pr_sc5507"}
        runtime.coordinator = FakeCoordinator()
        runtime.sources = {"Blu-ray": "dvd"}
        runtime.zones = (runtime,)
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}

        await self.init_module._async_register_services(self.hass)
        await self.hass.services.async_call(
            "onkyo_legacy",
            "set_source",
            {"entity_id": "media_player.onkyo_pr_sc5507", "source": "Blu-ray"},
        )

        self.assertIn(("select_source", "dvd"), runtime.coordinator.calls)

    async def test_set_source_service_invalid_source_raises(self) -> None:
        """set_source with an unknown source name raises vol.Invalid."""
        import voluptuous as vol

        runtime = type("Runtime", (), {})()
        runtime.entity_ids = {"media_player.onkyo_pr_sc5507"}
        runtime.coordinator = FakeCoordinator()
        runtime.sources = {"Blu-ray": "dvd"}
        runtime.zones = (runtime,)
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}

        await self.init_module._async_register_services(self.hass)

        with self.assertRaises(vol.Invalid):
            await self.hass.services.async_call(
                "onkyo_legacy",
                "set_source",
                {
                    "entity_id": "media_player.onkyo_pr_sc5507",
                    "source": "INVALID-SOURCE",
                },
            )

    async def test_set_volume_service_calls_set_volume(self) -> None:
        """set_volume service sends the hex-encoded volume to the zone volume command."""
        runtime = type("Runtime", (), {})()
        runtime.entity_ids = {"media_player.onkyo_pr_sc5507"}
        coordinator = FakeCoordinator()
        runtime.coordinator = coordinator
        zone_def = type("ZoneDef", (), {"volume_command": "MVL"})()
        runtime.zone = zone_def
        runtime.zones = (runtime,)
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}

        await self.init_module._async_register_services(self.hass)
        await self.hass.services.async_call(
            "onkyo_legacy",
            "set_volume",
            {"entity_id": "media_player.onkyo_pr_sc5507", "volume": 50},
        )

        # volume=50 -> format(50, "02X") = "32"
        self.assertIn(("MVL", "32"), coordinator.client.sent)
        self.assertIn(("refresh", ""), coordinator.calls)

    async def test_set_dimmer_service_calls_set_dimmer(self) -> None:
        """set_dimmer service sends the dimmer level string to the DIM command."""
        runtime = type("Runtime", (), {})()
        runtime.entity_ids = {"media_player.onkyo_pr_sc5507"}
        coordinator = FakeCoordinator()
        runtime.coordinator = coordinator
        runtime.zones = (runtime,)
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}

        await self.init_module._async_register_services(self.hass)
        await self.hass.services.async_call(
            "onkyo_legacy",
            "set_dimmer",
            {"entity_id": "media_player.onkyo_pr_sc5507", "level": "bright"},
        )

        self.assertIn(("DIM", "bright"), coordinator.client.sent)
        self.assertIn(("refresh", ""), coordinator.calls)

    async def test_detect_supported_zones_skips_failing_zone2(self) -> None:
        self_outer = self
        runtime = type("Runtime", (), {})()
        runtime.zone_label = "Main"
        runtime.host = "192.168.1.23"
        runtime.port = 60128

        zone2_runtime = type("ZoneRuntime", (), {})()
        zone2_runtime.zone_label = "Zone 2"
        zone2_runtime.host = "192.168.1.23"
        zone2_runtime.port = 60128

        class FailingCoordinator:
            def __init__(self) -> None:
                self.hass = self_outer.hass

            def _query_core_state(self) -> tuple[str, str, str, str]:
                raise TimeoutError("no response")

        zone2_runtime.coordinator = FailingCoordinator()
        runtime.candidate_zones = (zone2_runtime,)

        zones = await self.init_module._async_detect_supported_zones(runtime)

        self.assertEqual([zone.zone_label for zone in zones], ["Main"])

    async def test_detect_supported_zones_includes_successful_zone2(self) -> None:
        self_outer = self
        runtime = type("Runtime", (), {})()
        runtime.zone_label = "Main"
        runtime.host = "192.168.1.23"
        runtime.port = 60128

        zone2_runtime = type("ZoneRuntime", (), {})()
        zone2_runtime.zone_label = "Zone 2"
        zone2_runtime.host = "192.168.1.23"
        zone2_runtime.port = 60128

        class SuccessCoordinator:
            def __init__(self) -> None:
                self.hass = self_outer.hass

            def _query_core_state(self) -> tuple[str, str, str, str]:
                return ("PWR01", "MVL00", "AMT00", "SLI00")

        zone2_runtime.coordinator = SuccessCoordinator()
        runtime.candidate_zones = (zone2_runtime,)

        zones = await self.init_module._async_detect_supported_zones(runtime)

        self.assertEqual([zone.zone_label for zone in zones], ["Main", "Zone 2"])

    async def test_config_flow_import_uses_model_default_name_when_name_missing(self) -> None:
        flow = self.config_flow_module.OnkyoLegacyConfigFlow()

        result = await flow.async_step_import(
            {
                "host": "192.168.1.23",
                "port": 60128,
                "model": "PR-SC5507",
                "scan_interval": 10,
                "max_volume": 80,
                "sources": {"HTPC": "vcr"},
            }
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], "Onkyo PR-SC5507")

    async def test_config_flow_import_populates_retries_and_strict_sources(self) -> None:
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
                "retries": 3,
                "strict_sources": False,
            }
        )

        self.assertEqual(result["data"]["retries"], 3)
        self.assertFalse(result["data"]["strict_sources"])

    # ── async_step_user tests ──────────────────────────────────────────

    async def test_config_flow_user_shows_form(self) -> None:
        """async_step_user returns a form with host, port, model fields."""
        flow = self.config_flow_module.OnkyoLegacyConfigFlow()
        flow.hass = self.hass

        result = await flow.async_step_user(None)

        self.assertEqual(result["type"], "show_form")
        self.assertEqual(result["step_id"], "user")
        self.assertIn("host", result["data_schema"].schema)
        self.assertIn("port", result["data_schema"].schema)
        self.assertIn("model", result["data_schema"].schema)

    async def test_config_flow_user_creates_entry_on_valid_connection(self) -> None:
        """Valid host/port/model creates a config entry."""
        flow = self.config_flow_module.OnkyoLegacyConfigFlow()
        flow.hass = self.hass

        with patch("custom_components.onkyo_legacy.config_flow._validate_connection"):
            result = await flow.async_step_user(
                {
                    "host": "192.168.1.100",
                    "port": 60128,
                    "model": "PR-SC5507",
                }
            )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], "Onkyo PR-SC5507")
        self.assertEqual(result["data"]["host"], "192.168.1.100")
        self.assertEqual(result["data"]["port"], 60128)
        self.assertEqual(result["data"]["model"], "PR-SC5507")
        self.assertEqual(result["data"]["name"], "Onkyo PR-SC5507")
        self.assertEqual(result["data"]["scan_interval"], 10)
        self.assertEqual(result["data"]["max_volume"], 80)
        self.assertEqual(result["data"]["retries"], 2)
        self.assertTrue(result["data"]["strict_sources"])

    async def test_config_flow_user_shows_error_on_connection_failure(self) -> None:
        """Connection failure shows error."""
        flow = self.config_flow_module.OnkyoLegacyConfigFlow()
        flow.hass = self.hass

        with patch(
            "custom_components.onkyo_legacy.config_flow._validate_connection",
            side_effect=OSError,
        ):
            result = await flow.async_step_user(
                {
                    "host": "192.168.1.100",
                    "port": 60128,
                    "model": "PR-SC5507",
                }
            )

        self.assertEqual(result["type"], "show_form")
        self.assertEqual(result["errors"]["base"], "cannot_connect")

    async def test_config_flow_user_uses_tx8050_defaults(self) -> None:
        """TX-8050 model gets correct title and sources."""
        flow = self.config_flow_module.OnkyoLegacyConfigFlow()
        flow.hass = self.hass

        with patch("custom_components.onkyo_legacy.config_flow._validate_connection"):
            result = await flow.async_step_user(
                {
                    "host": "192.168.1.100",
                    "port": 60128,
                    "model": "TX-8050",
                }
            )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], "Onkyo TX-8050")
        self.assertEqual(result["data"]["model"], "TX-8050")
        self.assertEqual(result["data"]["name"], "Onkyo TX-8050")
        # TX-8050 specific source
        self.assertIn("DVD -- BD/DVD", result["data"]["sources"])

    async def test_config_flow_user_aborts_on_duplicate(self) -> None:
        """Duplicate host:port aborts with AbortFlow."""
        from homeassistant.data_entry_flow import AbortFlow

        flow = self.config_flow_module.OnkyoLegacyConfigFlow()
        flow.hass = self.hass

        # First call — creates entry
        with patch("custom_components.onkyo_legacy.config_flow._validate_connection"):
            result = await flow.async_step_user(
                {
                    "host": "192.168.1.100",
                    "port": 60128,
                    "model": "PR-SC5507",
                }
            )
        self.assertEqual(result["type"], "create_entry")

        # Second call with same host:port — should abort
        with self.assertRaises(AbortFlow):
            await flow.async_step_user(
                {
                    "host": "192.168.1.100",
                    "port": 60128,
                    "model": "PR-SC5507",
                }
            )

    # ── async_step_reconfigure tests ──────────────────────────────────

    async def test_config_flow_reconfigure_shows_form(self) -> None:
        """Reconfigure step returns form with pre-filled host/port."""
        flow = self.config_flow_module.OnkyoLegacyConfigFlow()
        flow.hass = self.hass

        mock_entry = self.init_module.ConfigEntry(
            entry_id="reconfigure",
            data={"host": "192.168.1.100", "port": 60128},
            title="Onkyo PR-SC5507",
        )

        with patch.object(flow, "_get_reconfigure_entry", return_value=mock_entry):
            result = await flow.async_step_reconfigure(None)

        self.assertEqual(result["type"], "show_form")
        self.assertEqual(result["step_id"], "reconfigure")
        self.assertIn("host", result["data_schema"].schema)
        self.assertIn("port", result["data_schema"].schema)

    async def test_config_flow_reconfigure_updates_on_valid_connection(self) -> None:
        """Reconfigure with valid host/port updates the entry data."""
        flow = self.config_flow_module.OnkyoLegacyConfigFlow()
        flow.hass = self.hass

        mock_entry = self.init_module.ConfigEntry(
            entry_id="reconfigure",
            data={"host": "192.168.1.100", "port": 60128},
            title="Onkyo PR-SC5507",
        )

        with patch.object(flow, "_get_reconfigure_entry", return_value=mock_entry):
            with patch("custom_components.onkyo_legacy.config_flow._validate_connection"):
                result = await flow.async_step_reconfigure(
                    {
                        "host": "192.168.1.200",
                        "port": 60129,
                    }
                )

        self.assertEqual(result["type"], "update_entry")
        self.assertEqual(mock_entry.data["host"], "192.168.1.200")
        self.assertEqual(mock_entry.data["port"], 60129)

    async def test_config_flow_reconfigure_shows_error_on_failure(self) -> None:
        """Reconfigure with unreachable host shows error."""
        flow = self.config_flow_module.OnkyoLegacyConfigFlow()
        flow.hass = self.hass

        mock_entry = self.init_module.ConfigEntry(
            entry_id="reconfigure",
            data={"host": "192.168.1.100", "port": 60128},
            title="Onkyo PR-SC5507",
        )

        with patch.object(flow, "_get_reconfigure_entry", return_value=mock_entry):
            with patch(
                "custom_components.onkyo_legacy.config_flow._validate_connection",
                side_effect=OSError,
            ):
                result = await flow.async_step_reconfigure(
                    {
                        "host": "192.168.1.200",
                        "port": 60129,
                    }
                )

        self.assertEqual(result["type"], "show_form")
        self.assertEqual(result["errors"]["base"], "cannot_connect")

    # ── Options flow tests ────────────────────────────────────────────

    async def test_options_flow_shows_form(self) -> None:
        """Options flow returns form with scan_interval, max_volume, retries, strict_sources."""
        entry = self.init_module.ConfigEntry(
            entry_id="test",
            data={},
            title="Onkyo Test",
            options={"scan_interval": 5},
        )
        handler = self.config_flow_module.OnkyoLegacyOptionsFlowHandler(entry)

        result = await handler.async_step_init(None)

        self.assertEqual(result["type"], "show_form")
        self.assertEqual(result["step_id"], "init")
        self.assertIn("scan_interval", result["data_schema"].schema)
        self.assertIn("max_volume", result["data_schema"].schema)
        self.assertIn("retries", result["data_schema"].schema)
        self.assertIn("strict_sources", result["data_schema"].schema)

    async def test_options_flow_creates_entry(self) -> None:
        """Options flow with valid input creates entry."""
        entry = self.init_module.ConfigEntry(
            entry_id="test",
            data={},
            title="Onkyo Test",
        )
        handler = self.config_flow_module.OnkyoLegacyOptionsFlowHandler(entry)

        result = await handler.async_step_init(
            {
                "scan_interval": 15,
                "max_volume": 90,
                "retries": 3,
                "strict_sources": False,
            }
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["scan_interval"], 15)
        self.assertEqual(result["data"]["max_volume"], 90)
        self.assertEqual(result["data"]["retries"], 3)
        self.assertFalse(result["data"]["strict_sources"])
