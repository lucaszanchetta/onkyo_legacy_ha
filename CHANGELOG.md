# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
