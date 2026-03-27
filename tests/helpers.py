"""Test helpers and lightweight stubs for Home Assistant imports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum, IntFlag
import importlib
from pathlib import Path
import sys
import types
from typing import Any, Callable


def install_stubs() -> None:
    """Install lightweight stubs for Home Assistant and voluptuous."""
    site_packages = next(
        Path(__file__).resolve().parents[1].glob(".venv/lib/python*/site-packages"),
        None,
    )
    if site_packages is not None and str(site_packages) not in sys.path:
        sys.path.insert(0, str(site_packages))

    if "homeassistant" in sys.modules:
        return

    vol = types.ModuleType("voluptuous")

    class Invalid(Exception):
        pass

    def _identity(value: Any) -> Any:
        return value

    class Schema:
        def __init__(self, schema: Any, extra: Any | None = None) -> None:
            self.schema = schema
            self.extra = extra

        def __call__(self, value: Any) -> Any:
            return value

    def Required(key: Any, default: Any | None = None) -> Any:
        return key

    def Optional(key: Any, default: Any | None = None) -> Any:
        return key

    def All(*validators: Callable[[Any], Any]) -> Callable[[Any], Any]:
        def validator(value: Any) -> Any:
            current = value
            for fn in validators:
                current = fn(current)
            return current

        return validator

    def Coerce(target: Callable[[Any], Any]) -> Callable[[Any], Any]:
        return lambda value: target(value)

    def Range(min: Any | None = None, max: Any | None = None) -> Callable[[Any], Any]:
        def validator(value: Any) -> Any:
            if min is not None and value < min:
                raise Invalid(f"value {value} below {min}")
            if max is not None and value > max:
                raise Invalid(f"value {value} above {max}")
            return value

        return validator

    def In(options: Any) -> Callable[[Any], Any]:
        allowed = set(options)

        def validator(value: Any) -> Any:
            if value not in allowed:
                raise Invalid(f"{value!r} not in allowed set")
            return value

        return validator

    vol.Invalid = Invalid
    vol.Schema = Schema
    vol.Required = Required
    vol.Optional = Optional
    vol.All = All
    vol.Coerce = Coerce
    vol.Range = Range
    vol.In = In
    vol.ALLOW_EXTRA = object()
    sys.modules["voluptuous"] = vol

    homeassistant = types.ModuleType("homeassistant")
    sys.modules["homeassistant"] = homeassistant

    const = types.ModuleType("homeassistant.const")
    const.CONF_HOST = "host"
    const.CONF_NAME = "name"
    const.CONF_PORT = "port"
    const.CONF_ENTITY_ID = "entity_id"
    sys.modules["homeassistant.const"] = const

    core = types.ModuleType("homeassistant.core")

    @dataclass
    class ServiceCall:
        data: dict[str, Any]

    class HomeAssistant:
        pass

    core.ServiceCall = ServiceCall
    core.HomeAssistant = HomeAssistant
    sys.modules["homeassistant.core"] = core

    config_entries = types.ModuleType("homeassistant.config_entries")

    @dataclass
    class ConfigEntry:
        entry_id: str
        data: dict[str, Any]
        title: str = ""

    class ConfigFlow:
        def __init_subclass__(cls, domain: str | None = None, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)
            cls.DOMAIN = domain

        async def async_set_unique_id(self, unique_id: str) -> None:
            self.unique_id = unique_id

        def _abort_if_unique_id_configured(self) -> None:
            return None

        def async_create_entry(self, *, title: str, data: dict[str, Any]) -> dict[str, Any]:
            return {"type": "create_entry", "title": title, "data": data}

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow
    sys.modules["homeassistant.config_entries"] = config_entries

    helpers_pkg = types.ModuleType("homeassistant.helpers")
    sys.modules["homeassistant.helpers"] = helpers_pkg

    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.string = _identity
    cv.port = _identity
    cv.entity_id = _identity
    cv.ensure_list = lambda value: value if isinstance(value, list) else [value]
    sys.modules["homeassistant.helpers.config_validation"] = cv

    discovery = types.ModuleType("homeassistant.helpers.discovery")

    async def async_load_platform(*args: Any, **kwargs: Any) -> None:
        return None

    discovery.async_load_platform = async_load_platform
    sys.modules["homeassistant.helpers.discovery"] = discovery

    typing_mod = types.ModuleType("homeassistant.helpers.typing")
    typing_mod.ConfigType = dict
    typing_mod.DiscoveryInfoType = dict
    sys.modules["homeassistant.helpers.typing"] = typing_mod

    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = Callable
    sys.modules["homeassistant.helpers.entity_platform"] = entity_platform

    entity = types.ModuleType("homeassistant.helpers.entity")

    class EntityCategory(Enum):
        DIAGNOSTIC = "diagnostic"

    entity.EntityCategory = EntityCategory
    sys.modules["homeassistant.helpers.entity"] = entity

    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")

    class _RegistryEntry:
        def __init__(self, entity_id: str, domain: str, platform: str, unique_id: str) -> None:
            self.entity_id = entity_id
            self.domain = domain
            self.platform = platform
            self.unique_id = unique_id

    class _Registry:
        def __init__(self) -> None:
            self._entries: dict[tuple[str, str, str], _RegistryEntry] = {}

        def async_get(self, entity_id: str) -> _RegistryEntry | None:
            for entry in self._entries.values():
                if entry.entity_id == entity_id:
                    return entry
            return None

        def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
            entry = self._entries.get((domain, platform, unique_id))
            return None if entry is None else entry.entity_id

        def async_remove(self, entity_id: str) -> None:
            for key, entry in list(self._entries.items()):
                if entry.entity_id == entity_id:
                    self._entries.pop(key)

        def async_update_entity(
            self,
            entity_id: str,
            *,
            new_unique_id: str | None = None,
            new_entity_id: str | None = None,
        ) -> None:
            for key, entry in list(self._entries.items()):
                if entry.entity_id == entity_id:
                    self._entries.pop(key)
                    next_entity_id = new_entity_id or entity_id
                    if new_unique_id is not None:
                        self._entries[(entry.domain, entry.platform, new_unique_id)] = _RegistryEntry(
                            next_entity_id,
                            entry.domain,
                            entry.platform,
                            new_unique_id,
                        )
                    else:
                        self._entries[key] = _RegistryEntry(
                            next_entity_id,
                            entry.domain,
                            entry.platform,
                            entry.unique_id,
                        )
                    return

        def add(self, domain: str, platform: str, unique_id: str, entity_id: str) -> None:
            self._entries[(domain, platform, unique_id)] = _RegistryEntry(
                entity_id, domain, platform, unique_id
            )

    _entity_registry = _Registry()

    def async_get(hass: Any) -> _Registry:
        return _entity_registry

    entity_registry.async_get = async_get
    entity_registry._entity_registry = _entity_registry
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry

    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")

    class UpdateFailed(Exception):
        pass

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, item: Any) -> type["DataUpdateCoordinator"]:
            return cls

        def __init__(self, hass: Any, logger: Any, name: str, update_interval: Any = None) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None
            self.last_update_success = False

        async def async_refresh(self) -> None:
            self.data = await self._async_update_data()
            self.last_update_success = True

        async def async_request_refresh(self) -> None:
            await self.async_refresh()

    class CoordinatorEntity:
        def __init__(self, coordinator: DataUpdateCoordinator) -> None:
            self.coordinator = coordinator
            self.entity_id: str | None = None

        async def async_added_to_hass(self) -> None:
            return None

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.CoordinatorEntity = CoordinatorEntity
    update_coordinator.UpdateFailed = UpdateFailed
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator

    components = types.ModuleType("homeassistant.components")
    sys.modules["homeassistant.components"] = components

    media_player = types.ModuleType("homeassistant.components.media_player")

    class MediaPlayerEntity:
        pass

    class MediaPlayerEntityFeature(IntFlag):
        TURN_ON = 1
        TURN_OFF = 2
        VOLUME_SET = 4
        VOLUME_STEP = 8
        VOLUME_MUTE = 16
        SELECT_SOURCE = 32

    class MediaPlayerState(Enum):
        OFF = "off"
        ON = "on"

    media_player.MediaPlayerEntity = MediaPlayerEntity
    media_player.MediaPlayerEntityFeature = MediaPlayerEntityFeature
    media_player.MediaPlayerState = MediaPlayerState
    sys.modules["homeassistant.components.media_player"] = media_player

    select = types.ModuleType("homeassistant.components.select")

    class SelectEntity:
        pass

    select.SelectEntity = SelectEntity
    sys.modules["homeassistant.components.select"] = select

    switch = types.ModuleType("homeassistant.components.switch")

    class SwitchEntity:
        pass

    switch.SwitchEntity = SwitchEntity
    sys.modules["homeassistant.components.switch"] = switch

    number = types.ModuleType("homeassistant.components.number")

    class NumberEntity:
        pass

    class NumberMode(Enum):
        BOX = "box"

    number.NumberEntity = NumberEntity
    number.NumberMode = NumberMode
    sys.modules["homeassistant.components.number"] = number

    sensor = types.ModuleType("homeassistant.components.sensor")

    @dataclass(frozen=True)
    class SensorEntityDescription:
        key: str = ""
        name: str | None = None
        icon: str | None = None

    class SensorEntity:
        pass

    sensor.SensorEntity = SensorEntity
    sensor.SensorEntityDescription = SensorEntityDescription
    sys.modules["homeassistant.components.sensor"] = sensor


class FakeServices:
    """Simple registry for Home Assistant services."""

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], Callable[..., Any]] = {}

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self._handlers

    def async_register(self, domain: str, service: str, handler: Callable[..., Any], schema: Any = None) -> None:
        self._handlers[(domain, service)] = handler

    async def async_call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        handler = self._handlers[(domain, service)]
        await handler(types.SimpleNamespace(data=data))


class FakeBus:
    def async_listen_once(self, event: str, callback: Callable[..., Any]) -> None:
        self.event = event
        self.callback = callback


class FakeHass:
    """Minimal HA object for testing async integration code."""

    def __init__(self) -> None:
        entity_registry = sys.modules.get("homeassistant.helpers.entity_registry")
        if entity_registry is not None:
            entity_registry._entity_registry = type(entity_registry._entity_registry)()
            entity_registry.async_get = lambda hass: entity_registry._entity_registry
        self.data: dict[str, Any] = {}
        self.services = FakeServices()
        self.bus = FakeBus()
        self.created_tasks: list[Any] = []
        self.config_entries = FakeConfigEntries()

    async def async_add_executor_job(self, func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)

    def async_create_task(self, coro: Any) -> Any:
        task = asyncio.create_task(coro)
        self.created_tasks.append(task)
        return task


class FakeConfigEntries:
    def __init__(self) -> None:
        self.flow = types.SimpleNamespace(async_init=self._async_init)
        self.init_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.forward_calls: list[tuple[str, tuple[str, ...]]] = []

    async def _async_init(self, domain: str, context: dict[str, Any], data: dict[str, Any]) -> None:
        self.init_calls.append((domain, context, data))

    async def async_forward_entry_setups(self, entry: Any, platforms: tuple[str, ...]) -> None:
        self.forward_calls.append((entry.entry_id, platforms))

    async def async_unload_platforms(self, entry: Any, platforms: tuple[str, ...]) -> bool:
        return True


def fresh_import(module_name: str) -> Any:
    """Import a module after stubs have been installed."""
    install_stubs()
    prefix = f"{module_name}."
    for name in list(sys.modules):
        if name == module_name or name.startswith(prefix):
            sys.modules.pop(name, None)
    return importlib.import_module(module_name)


async def add_entities(entities: list[Any], sink: list[Any]) -> None:
    """Capture entities from async_setup_platform callbacks."""
    sink.extend(entities)
    for index, entity in enumerate(entities):
        entity.entity_id = f"test.entity_{index}"
        await entity.async_added_to_hass()


def run(coro: Any) -> Any:
    """Run an async coroutine in tests."""
    return asyncio.run(coro)
