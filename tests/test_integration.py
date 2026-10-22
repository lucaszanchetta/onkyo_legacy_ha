from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
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

    def cooldown_remaining(self) -> float:
        return 0.0


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.client = FakeClient()
        self.last_update_success = False
        self.core_power_is_fresh = True

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
        self.config_flow_module = fresh_import(
            "custom_components.onkyo_legacy.config_flow"
        )
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

    async def test_connection_validation_queries_power_with_unprefixed_command(
        self,
    ) -> None:
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

        with patch.object(
            self.config_flow_module, "OnkyoLegacyClient", side_effect=make_client
        ):
            self.config_flow_module._validate_connection("192.0.2.10", 60128)

        self.assertEqual(clients[0].queries, ["PWR"])
        self.assertTrue(clients[0].disconnected)

    async def test_main_entity_registry_migration_prefers_current_unique_ids(
        self,
    ) -> None:
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

        await self.init_module._async_migrate_main_entity_unique_ids(
            self.hass, "192.168.1.23"
        )

        self.assertIsNone(
            registry.async_get_entity_id(
                "select", "onkyo_legacy", "192.168.1.23-source"
            )
        )
        self.assertEqual(
            registry.async_get_entity_id(
                "select", "onkyo_legacy", "192.168.1.23-main-source"
            ),
            "select.theater_source",
        )

    async def test_main_entity_registry_migration_reclaims_unsuffixed_entity_ids(
        self,
    ) -> None:
        entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
        registry = entity_registry.async_get(self.hass)
        registry.add(
            "select",
            "onkyo_legacy",
            "192.168.1.23-main-source",
            "select.onkyo_pr_sc5507_source_2",
        )

        await self.init_module._async_migrate_main_entity_unique_ids(
            self.hass, "192.168.1.23"
        )

        self.assertEqual(
            registry.async_get_entity_id(
                "select", "onkyo_legacy", "192.168.1.23-main-source"
            ),
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
            registry.async_get_entity_id(
                "switch", "onkyo_legacy", "192.168.1.23-zone2-power"
            )
        )
        self.assertIsNone(
            registry.async_get_entity_id(
                "switch", "onkyo_legacy", "192.168.1.23-zone3-mute"
            )
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
            registry.async_get_entity_id(
                "media_player", "onkyo_legacy", "192.168.1.23-zone2"
            )
        )
        self.assertIsNone(
            registry.async_get_entity_id(
                "number", "onkyo_legacy", "192.168.1.23-zone2-volume-level"
            )
        )
        self.assertIsNone(
            registry.async_get_entity_id(
                "select", "onkyo_legacy", "192.168.1.23-zone2-source"
            )
        )

    async def test_setup_entry_does_not_fail_when_initial_refresh_times_out(
        self,
    ) -> None:
        runtime = type("Runtime", (), {})()
        runtime.host = "192.0.2.10"
        runtime.port = 60128
        runtime.zone_key = "main"
        runtime.zones = (runtime,)
        runtime.candidate_zones = ()
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
            "IFA",
        )
        runtime.coordinator = FakeCoordinator()
        runtime.coordinator.hass = self.hass
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
        client = type(
            "Client",
            (),
            {
                "close": lambda self: self._closed.append(True),
                "begin_close": lambda self: None,
                "cooldown_remaining": lambda self: 0.0,
            },
        )()
        client._closed = []
        runtime.coordinator = type(
            "Coordinator",
            (),
            {
                "client": client,
                "hass": self.hass,
                "data": type("State", (), {"power": False})(),
                "core_power_is_fresh": False,
                "command_capabilities": {},
                "set_command_capability": lambda self, cmd, value: self.command_capabilities.__setitem__(cmd, value),
                "_start_optional_refresh": lambda self: None,
            },
        )()
        runtime.model = "UNKNOWN"
        runtime.host = "192.0.2.10"
        runtime.queryable_commands = ("PWR", "MVL", "AMT", "SLI", "LMD")
        runtime.candidate_zones = ()
        runtime.discovery_states = {}
        runtime.discovery_timeouts = {}
        runtime.capability_timeouts = {}
        runtime.capability_cursor = 0
        additions = []
        runtime.platform_adders = {"media_player": lambda: additions.append("added")}
        runtime.stopping = False
        runtime.initial_refresh_task = None
        runtime.stop_listener_unsub = lambda: None
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        unload_started = asyncio.Event()
        finish_unload = asyncio.Event()

        async def failed_unload(*_args):
            self.assertTrue(runtime.stopping)
            unload_started.set()
            await finish_unload.wait()
            return False

        self.hass.config_entries.async_unload_platforms = failed_unload
        entry = self.init_module.ConfigEntry(entry_id="entry-1", data={})

        stale_task = asyncio.create_task(asyncio.Event().wait())
        runtime.discovery_task = stale_task
        unload_task = asyncio.create_task(self.init_module.async_unload_entry(self.hass, entry))
        await unload_started.wait()
        self.assertTrue(stale_task.cancelled())
        self.init_module._notify_dynamic_entities(runtime)
        self.assertEqual(additions, [])
        finish_unload.set()
        result = await unload_task

        self.assertFalse(result)
        self.assertIs(self.hass.data["onkyo_legacy"]["entry-1"], runtime)
        self.assertEqual(client._closed, [])
        self.assertFalse(runtime.stopping)
        self.assertIsNotNone(runtime.discovery_task)
        self.assertIsNot(runtime.discovery_task, stale_task)
        self.init_module._notify_dynamic_entities(runtime)
        self.assertEqual(additions, ["added"])
        runtime.stopping = True
        runtime.discovery_task.cancel()
        await asyncio.gather(runtime.discovery_task, return_exceptions=True)

    async def test_successful_unload_closes_client_in_executor(self) -> None:
        runtime = type("Runtime", (), {})()
        client = type(
            "Client",
            (),
            {
                "close": lambda self: self._closed.append(True),
                "begin_close": lambda self: None,
            },
        )()
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

    async def test_homeassistant_stop_cancels_optional_tasks_for_every_zone(
        self,
    ) -> None:
        class Client:
            def __init__(self):
                self.closed = False

            def begin_close(self):
                self.closing = True

            def close(self):
                self.closed = True

        class Coordinator:
            def __init__(self, client=None):
                self.client = client
                self.cancelled = False

            async def async_cancel_optional_refresh(self):
                self.cancelled = True

        client = Client()
        runtime = type("Runtime", (), {})()
        runtime.coordinator = Coordinator(client)
        zone2 = type("Zone", (), {})()
        zone2.coordinator = Coordinator()
        runtime.zones = (runtime, zone2)
        self.hass.data["onkyo_legacy"] = {"entry": runtime}

        await self.init_module._disconnect_all(self.hass)

        self.assertTrue(runtime.coordinator.cancelled)
        self.assertTrue(zone2.coordinator.cancelled)
        self.assertTrue(client.closing)
        self.assertTrue(client.closed)

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

        self.hass.async_add_executor_job = lambda func, *args: (
            self.hass.loop.run_in_executor(None, queued_executor, func, *args)
        )
        entry = self.init_module.ConfigEntry(entry_id="entry-1", data={})
        unload_task = asyncio.create_task(
            self.init_module.async_unload_entry(self.hass, entry)
        )
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
        runtime.coordinator = type("Coordinator", (), {"client": Client()})()
        runtime.stop_listener_unsub = lambda: None
        self.hass.data["onkyo_legacy"] = {"entry-1": runtime}
        self.hass.async_add_executor_job = lambda func, *args: (
            self.hass.loop.run_in_executor(None, func, *args)
        )
        entry = self.init_module.ConfigEntry(entry_id="entry-1", data={})
        unload_task = asyncio.create_task(
            self.init_module.async_unload_entry(self.hass, entry)
        )
        await cleanup_started.wait()
        unload_task.cancel()
        release_cleanup.set()
        with self.assertRaises(asyncio.CancelledError):
            await unload_task
        self.assertTrue(close_ran.is_set())
        self.assertTrue(task.done())

    async def test_setup_returns_while_discovery_query_is_silent_and_unload_cancels_it(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()
        runtime = type("Runtime", (), {})()
        runtime.host = "192.0.2.10"
        runtime.port = 60128
        runtime.zone_key = "main"
        runtime.zones = (runtime,)
        runtime.candidate_zones = ()
        runtime.queryable_commands = (
            "LMD", "DIM", "SLA", "LTN", "RAS", "ADQ", "ADV", "MOT", "TGA", "IFA",
        )
        runtime.discovery_states = {}
        runtime.discovery_timeouts = {}
        runtime.capability_timeouts = {}
        runtime.platform_adders = {}
        runtime.stopping = False
        runtime.model = "UNKNOWN-MODEL"
        runtime.coordinator = FakeCoordinator()
        runtime.coordinator.hass = self.hass
        runtime.coordinator.data = type("State", (), {"power": True})()
        runtime.coordinator.command_capabilities = {}
        runtime.coordinator.zone = type(
            "Zone",
            (),
            {
                "power_command": "PWR",
                "volume_command": "MVL",
                "mute_command": "AMT",
                "source_command": "SLI",
            },
        )()
        runtime.coordinator._start_optional_refresh = lambda: None
        client = runtime.coordinator.client

        def blocked_power_query(command, timeout=1.5):
            started.set()
            release.wait(1.0)
            return "PWR01"

        client.query_optional = blocked_power_query
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
            setup_started = time.perf_counter()
            result = await asyncio.wait_for(
                self.init_module.async_setup_entry(self.hass, entry), 0.5
            )
            setup_elapsed = time.perf_counter() - setup_started
            self.assertTrue(result)
            self.assertLess(setup_elapsed, 0.5)
            if os.environ.get("ONKYO_REPORT_SETUP_TIMING") == "1":
                print(
                    f"setup_elapsed_ms={setup_elapsed * 1000:.3f} "
                    f"silent_optional_commands={len(runtime.queryable_commands)} "
                    "first_optional_response_blocked=True"
                )
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            unload_task = asyncio.create_task(
                self.init_module.async_unload_entry(self.hass, entry)
            )
            await unload_task
            release.set()

        self.assertTrue(getattr(client, "_closing", False))
        self.assertNotIn(entry.entry_id, self.hass.data["onkyo_legacy"])

    async def test_setup_cancellation_during_forwarding_cleans_executor_future(
        self,
    ) -> None:
        runtime = type("Runtime", (), {})()
        runtime.host = "192.0.2.10"
        runtime.port = 60128
        runtime.zone_key = "main"
        runtime.zones = (runtime,)
        runtime.candidate_zones = ()
        runtime.queryable_commands = ()
        runtime.coordinator = FakeCoordinator()

        async def cancel_forward(_entry, _platforms):
            raise asyncio.CancelledError

        self.hass.config_entries.async_forward_entry_setups = cancel_forward
        self.hass.async_add_executor_job = lambda func, *args: (
            self.hass.loop.run_in_executor(None, func, *args)
        )
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

    async def test_discovery_rotates_past_inconclusive_zone_and_adds_zone3_once(
        self,
    ) -> None:
        class SharedClient:
            def __init__(self) -> None:
                self.calls = []

            def cooldown_remaining(self) -> float:
                return 0.0

            def query_optional(self, command: str, timeout: float = 1.5) -> str:
                self.calls.append(command)
                if command == "ZPW":
                    raise ConnectionError("shared receiver cooldown")
                return "PW300"

        main = type("Runtime", (), {})()
        main.host = "192.0.2.1"
        main.port = 60128
        main.zone_key = "main"
        main.model = "PR-SC5507"
        main.queryable_commands = ()
        main.stopping = False
        main.zones = (main,)
        main.discovery_states = {"zone2": "pending", "zone3": "pending"}
        main.discovery_timeouts = {}
        main.capability_timeouts = {}
        main.platform_adders = {"media_player": lambda: added.append("zone3")}
        main.coordinator = FakeCoordinator()
        main.coordinator.hass = self.hass
        main.coordinator.data = type("State", (), {"power": True})()
        main.coordinator.supports = lambda _command: False
        main.coordinator.client = SharedClient()
        main.coordinator.command_capabilities = {}

        def candidate(key: str, label: str, power: str):
            zone = type("Zone", (), {})()
            zone.zone_key = key
            zone.zone_label = label
            zone.host = main.host
            zone.port = main.port
            zone.coordinator = type("ZoneCoordinator", (), {})()
            zone.coordinator.zone = type("ZoneDef", (), {"power_command": power})()
            return zone

        zone2 = candidate("zone2", "Zone 2", "ZPW")
        zone3 = candidate("zone3", "Zone 3", "PW3")
        main.candidate_zones = (zone2, zone3)
        added = []
        sleep_calls = 0

        async def stop_after_two_rounds(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 2:
                raise asyncio.CancelledError

        with patch.object(
            self.init_module.asyncio, "sleep", side_effect=stop_after_two_rounds
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.init_module._async_discovery_loop(main)

        self.assertEqual(main.coordinator.client.calls, ["ZPW", "PW3"])
        self.assertEqual(main.discovery_states["zone2"], "inconclusive")
        self.assertEqual(main.discovery_states["zone3"], "supported")
        self.assertEqual([zone.zone_key for zone in main.zones], ["main", "zone3"])
        self.assertEqual(added, ["zone3"])

    async def test_zone_discovery_waits_for_shared_cooldown_before_next_candidate(self) -> None:
        class CooldownClient:
            def __init__(self) -> None:
                self.calls = []
                self.remaining = 0.0

            def cooldown_remaining(self) -> float:
                return self.remaining

            def query_optional(self, command: str, timeout: float = 1.5) -> str:
                self.calls.append(command)
                if command == "ZPW":
                    self.remaining = 60.0
                    raise ConnectionError("transport failure opened shared cooldown")
                return "PW300"

        main = type("Runtime", (), {})()
        main.host = "192.0.2.5"
        main.port = 60128
        main.zone_key = "main"
        main.model = "PR-SC5507"
        main.stopping = False
        main.queryable_commands = ()
        main.zones = (main,)
        main.discovery_states = {"zone2": "pending", "zone3": "pending"}
        main.discovery_timeouts = {}
        main.capability_timeouts = {}
        main.platform_adders = {"media_player": lambda: None}
        main.coordinator = FakeCoordinator()
        main.coordinator.hass = self.hass
        main.coordinator.data = type("State", (), {"power": True})()
        main.coordinator.supports = lambda _command: False
        main.coordinator.client = CooldownClient()
        main.coordinator.command_capabilities = {}

        def zone(key: str, command: str):
            value = type("Zone", (), {})()
            value.zone_key = key
            value.zone_label = key
            value.coordinator = type("Coordinator", (), {})()
            value.coordinator.zone = type("Definition", (), {"power_command": command})()
            return value

        main.candidate_zones = (zone("zone2", "ZPW"), zone("zone3", "PW3"))
        delays = []

        async def simulated_sleep(delay: float) -> None:
            delays.append(delay)
            if delay == 60.0:
                main.coordinator.client.remaining = 0.0
            else:
                raise asyncio.CancelledError

        with patch.object(self.init_module.asyncio, "sleep", side_effect=simulated_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.init_module._async_discovery_loop(main)

        self.assertEqual(delays[0], 60.0)
        self.assertEqual(main.coordinator.client.calls, ["ZPW", "PW3"])
        self.assertEqual(main.discovery_states["zone3"], "supported")

    async def test_zone_registry_cleanup_keeps_inconclusive_candidates(self) -> None:
        registry = sys.modules["homeassistant.helpers.entity_registry"].async_get(
            self.hass
        )
        registry.add(
            "media_player", "onkyo_legacy", "192.0.2.1-zone2", "media_player.zone2"
        )
        runtime = type("Runtime", (), {})()
        runtime.host = "192.0.2.1"
        runtime.zones = ()
        runtime.discovery_states = {"zone2": "inconclusive", "zone3": "pending"}

        await self.init_module._async_remove_unsupported_zone_registry_entries(
            self.hass, runtime
        )

        self.assertIsNotNone(
            registry.async_get_entity_id(
                "media_player", "onkyo_legacy", "192.0.2.1-zone2"
            )
        )

    async def test_zone_power_timeouts_become_unsupported_only_after_repeated_on_state_silence(
        self,
    ) -> None:
        class SilentClient:
            def __init__(self) -> None:
                self.calls = 0

            def cooldown_remaining(self) -> float:
                return 0.0

            def query_optional(self, _command: str, timeout: float = 1.5) -> str:
                self.calls += 1
                raise self_outer.init_module.CommandResponseTimeout(
                    "no command response"
                )

        self_outer = self
        main = type("Runtime", (), {})()
        main.host = "192.0.2.2"
        main.port = 60128
        main.zone_key = "main"
        main.model = "PR-SC5507"
        main.stopping = False
        main.queryable_commands = ()
        main.zones = (main,)
        main.discovery_states = {"zone2": "pending"}
        main.discovery_timeouts = {}
        main.capability_timeouts = {}
        main.platform_adders = {}
        main.coordinator = FakeCoordinator()
        main.coordinator.hass = self.hass
        main.coordinator.data = type("State", (), {"power": True})()
        main.coordinator.core_power_is_fresh = False
        main.coordinator.supports = lambda _command: False
        main.coordinator.client = SilentClient()
        zone = type("Zone", (), {})()
        zone.zone_key = "zone2"
        zone.zone_label = "Zone 2"
        zone.coordinator = type(
            "ZoneCoordinator",
            (),
            {
                "zone": type("ZoneDef", (), {"power_command": "ZPW"})(),
            },
        )()
        main.candidate_zones = (zone,)
        sleep_calls = 0

        async def refresh_then_continue(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                main.coordinator.core_power_is_fresh = True

        with patch.object(
            self.init_module.asyncio, "sleep", side_effect=refresh_then_continue
        ):
            await self.init_module._async_discovery_loop(main)

        self.assertEqual(main.coordinator.client.calls, 3)
        self.assertEqual(main.discovery_timeouts["zone2"], 2)
        self.assertEqual(main.discovery_states["zone2"], "unsupported")

    async def test_generic_capabilities_wait_for_main_power_and_probe_two_per_round(
        self,
    ) -> None:
        main = type("Runtime", (), {})()
        main.host = "192.0.2.3"
        main.port = 60128
        main.zone_key = "main"
        main.model = "UNKNOWN"
        main.stopping = False
        main.queryable_commands = ("PWR", "MVL", "AMT", "SLI", "LMD", "DIM", "SLA")
        main.zones = (main,)
        main.candidate_zones = ()
        main.discovery_states = {}
        main.discovery_timeouts = {}
        main.capability_timeouts = {}
        main.platform_adders = {}
        main.coordinator = FakeCoordinator()
        main.coordinator.hass = self.hass
        main.coordinator.data = type("State", (), {"power": False})()
        main.coordinator.command_capabilities = {}
        main.coordinator.set_command_capability = lambda command, supported: (
            main.coordinator.command_capabilities.__setitem__(command, supported)
        )
        main.coordinator._start_optional_refresh = lambda: None
        calls = []
        main.coordinator.client.query_optional = lambda command, _timeout=1.5: (
            calls.append(command) or f"{command}01"
        )
        sleep_calls = 0

        async def power_on_then_stop(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                main.coordinator.data.power = True
            else:
                raise asyncio.CancelledError

        with patch.object(
            self.init_module.asyncio, "sleep", side_effect=power_on_then_stop
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.init_module._async_discovery_loop(main)

        self.assertEqual(calls, ["LMD", "DIM"])
        self.assertEqual(
            main.coordinator.command_capabilities, {"LMD": True, "DIM": True}
        )

    async def test_generic_capability_cursor_moves_past_transport_failures(self) -> None:
        main = type("Runtime", (), {})()
        main.host = "192.0.2.4"
        main.port = 60128
        main.zone_key = "main"
        main.model = "UNKNOWN"
        main.stopping = False
        main.queryable_commands = (
            "PWR", "MVL", "AMT", "SLI", "LMD", "DIM", "SLA", "TUN",
        )
        main.zones = (main,)
        main.candidate_zones = ()
        main.discovery_states = {}
        main.discovery_timeouts = {}
        main.capability_timeouts = {}
        main.capability_cursor = 0
        main.coordinator = FakeCoordinator()
        main.coordinator.hass = self.hass
        main.coordinator.data = type("State", (), {"power": True})()
        main.coordinator.command_capabilities = {}
        main.coordinator.set_command_capability = lambda command, supported: (
            main.coordinator.command_capabilities.__setitem__(command, supported)
        )
        main.coordinator._start_optional_refresh = lambda: None
        calls = []

        class Client:
            def cooldown_remaining(self) -> float:
                return 0.0

            def query_optional(self, command: str, timeout: float = 1.5) -> str:
                calls.append(command)
                if command in {"LMD", "DIM"}:
                    raise ConnectionError("temporary transport failure")
                if command == "SLA":
                    return "SLA00"
                raise asyncio.CancelledError

        main.coordinator.client = Client()
        main.platform_adders = {"select": lambda: setattr(main, "stopping", True)}
        sleeps = 0

        async def one_retry_round(_delay: float) -> None:
            nonlocal sleeps
            sleeps += 1

        with patch.object(self.init_module.asyncio, "sleep", side_effect=one_retry_round):
            await self.init_module._async_discovery_loop(main)

        self.assertEqual(calls, ["LMD", "DIM", "SLA"])
        self.assertEqual(main.coordinator.command_capabilities, {"SLA": True})

    async def test_cancel_during_generic_probe_cooldown_is_not_suppressed(self) -> None:
        entered_probe = asyncio.Event()
        release_probe = asyncio.Event()
        cooldown_active = False

        async def blocked_executor(_func, *_args):
            nonlocal cooldown_active
            cooldown_active = True
            entered_probe.set()
            await release_probe.wait()

        self.hass.async_add_executor_job = blocked_executor
        main = type("Runtime", (), {})()
        main.host = "192.0.2.4"
        main.port = 60128
        main.zone_key = "main"
        main.model = "UNKNOWN"
        main.queryable_commands = ("PWR", "MVL", "AMT", "SLI", "LMD")
        main.zones = (main,)
        main.candidate_zones = ()
        main.discovery_states = {}
        main.discovery_timeouts = {}
        main.capability_timeouts = {}
        main.capability_cursor = 0
        main.stopping = False
        main.platform_adders = {}
        main.coordinator = FakeCoordinator()
        main.coordinator.hass = self.hass
        main.coordinator.data = type("State", (), {"power": True})()
        main.coordinator.command_capabilities = {}
        main.coordinator.set_command_capability = lambda command, supported: (
            main.coordinator.command_capabilities.__setitem__(command, supported)
        )
        main.coordinator._start_optional_refresh = lambda: None

        class Client:
            def cooldown_remaining(self) -> float:
                return 60.0 if cooldown_active else 0.0

            def query_optional(self, _command: str, timeout: float = 1.5) -> str:
                self.probe_started = True
                raise AssertionError("executor should own the blocked query")

        main.coordinator.client = Client()
        task = asyncio.create_task(self.init_module._async_discovery_loop(main))
        await asyncio.wait_for(entered_probe.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)
        self.assertTrue(task.cancelled())

    async def test_config_flow_import_uses_model_default_name_when_name_missing(
        self,
    ) -> None:
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

    async def test_config_flow_import_populates_retries_and_strict_sources(
        self,
    ) -> None:
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
            with patch(
                "custom_components.onkyo_legacy.config_flow._validate_connection"
            ):
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
