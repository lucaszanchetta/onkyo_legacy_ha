# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Preserve newer per-field receiver push updates when polling completes, including standby and secondary zones; keep information dictionaries independent between snapshots.
- Serialize listener reads and command queries through one framed socket reader; preserve unsolicited updates received during a query and reset connections on partial frames or EOF.
- Share a capped exponential reconnect cooldown across listener, queries, batches, and probes; send-only writes are not retried.
- Poll core state before optional commands, limit optional polling to powered-on receivers, and back off independently for silent optional commands.
- Publish core state before optional queries; schedule one bounded optional refresh task and prioritize queued controls with a fair serialized socket gate that releases during retry cooldowns.
- Keep push state mutation on the Home Assistant loop, fan out pushes to active zones, and close clients off-loop during unload while preserving runtime after failed platform unload.
- Validate config-flow connectivity through the same client, and route volume/dimmer services through coordinator methods.
- Discover receiver zones and generic optional capabilities in the background; add confirmed entities dynamically, preserve inconclusive registry entries, and cancel discovery cleanly on shutdown.

## [0.3.0] - 2026-07-14

### Added
- `hacs.json` repository manifest to enable installation via HACS.
- Missing sensor translations for `video_resolution` and `hdmi_output` so they no longer display raw keys in the Home Assistant UI.

### Fixed
- Generic and PR-SC5507 receiver profiles now correctly expose Audio Selector, Late Night, and Audyssey Dynamic Volume select entities. Option lists were previously gated on model identity (`normalized_model == MODEL`) which suppressed these selects for the GENERIC profile even when the receiver supported the commands. Gating now keys on `profile.queryable_commands` membership, consistent with the project's capability-driven model.
- README compatibility table now lists the TX-8050 as "Main" only, matching the `supported_zones` declared in `const.py` (it previously claimed "Main, Zone 2").

### Changed
- Bumped integration version to 0.3.0.

## [0.2.0] - 2026-07-14

### Added
- Initial public release of the Onkyo Legacy integration.
- Support for PR-SC5507, TX-8050, and generic models with auto-detection.
- Media player, number, select, sensor, and switch entities for supported capabilities.
- Thread-safe eISCP client with retry, backoff, and circuit breaker.
- Background push listener for unsolicited ISCP updates.
- Config flow with user, import, reconfigure, and options flows.
- Diagnostics export for bug reports.
- 74 tests using lightweight HA stubs (no Home Assistant installation required).
