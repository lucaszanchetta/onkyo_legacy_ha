# Onkyo Legacy Custom Integration

Custom Home Assistant integration for legacy Onkyo receivers (PR-SC5507, TX-8050, and generic models) that use ISCP over TCP but don't work well with the official HA integration.

## Features

- **UI-based setup** — add receivers through the HA Integrations page with automatic connection validation
- **Options flow** — adjust polling interval, max volume, retries, and source mode without YAML
- **Reconfigure flow** — change host/port post-setup
- **Extensible model profiles** — built-in profiles for PR-SC5507 and TX-8050, plus a GENERIC profile with automatic capability detection for unknown models
- **Push/event support** — background listener thread for instant state updates from the receiver, with polling as a safety net
- **Responsive polling** — power and core state publish before optional checks; optional commands run two at a time in a bounded background task
- **Circuit breaker** — exponential backoff retries with automatic recovery after connection failures; queued controls receive bounded priority over optional polling
- **Zone 2/3 support** — per-zone power, mute, volume, source, and listening mode controls
- **Diagnostics** — built-in diagnostics for bug reports (config, state, circuit breaker status)
- **5 services** — `refresh`, `set_listening_mode`, `set_source`, `set_volume`, `set_dimmer`

## Compatibility

This integration works with any receiver that supports **eISCP over TCP port 60128**. This includes:

- **Onkyo** networked AV receivers, stereo receivers, and pre/pros (2011+)
- **Integra** custom install receivers and processors (2011+)
- **Pioneer** networked AV receivers (2016+)

### Requirements

- Ethernet or Wi-Fi connection to the receiver
- **Network Control** enabled on the receiver (`Setup → Hardware → Network → Network Control`)
- The receiver must be reachable on TCP port 60128

### Tested Models

| Model | Type | Zones | Profile |
|-------|------|-------|---------|
| PR-SC5507 | AV Processor | Main, Zone 2, Zone 3 | Built-in (22/30 commands queryable) |
| TX-8050 | Stereo Receiver | Main | Built-in (11/12 commands queryable) |

### Generic Model Support

Any unlisted eISCP receiver uses the **GENERIC** profile. It probes optional commands at startup only when a power query reports the receiver is on. At standby startup, optional capabilities remain provisional; while powered on, optional commands are checked two at a time with per-command retry suppression. Those runtime checks preserve polling health but do not change the displayed capability list until setup runs again. This means:

- Core controls (power, volume, mute, source) work on virtually all eISCP receivers
- Optional controls (listening mode, dimmer, sleep timer, etc.) are auto-detected while powered on
- Zone 2/3 support is detected at startup
- No manual configuration needed — just enter the IP address

To check what your receiver supports before installing, use the included smoke test tool:

```bash
./.venv/bin/python scripts/prsc5507_smoke.py --host 192.168.1.23 --model YOUR_MODEL --discover
```

Run `python scripts/receiver_scheduling_benchmark.py --runs 3` to measure the client gate against a FIFO baseline with simulated I/O. The benchmark holds one optional transaction in flight, queues another, then measures a control send and its core query. In the recorded run, median send queue/core completion was 45.6/65.7 ms for FIFO versus 25.9/46.1 ms for the priority gate with 20 ms optional responses; with 1.5 s silent responses, it was 3,005.9/3,026.1 ms versus 1,505.8/1,526.0 ms. These timings exercise the client scheduling code with simulated delays; they are not receiver measurements.

### Known Limitations

- Volume resolution varies by model era: older models use 0–80, newer models use 0–200
- Some commands may time out on certain models (these are automatically disabled)
- Zone 2/3 volume control may not work on all models (e.g., TX-8050 Zone 2 volume returns N/A)
- Network Control must be enabled — without it, the receiver only accepts connections when powered on

## Install

Copy `custom_components/onkyo_legacy` into your Home Assistant config directory:

```
config/
  custom_components/
    onkyo_legacy/
```

Restart Home Assistant, then add the integration through **Settings → Devices & Services → Add Integration → Onkyo Legacy**.

## Configuration

### UI Setup (recommended)

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Onkyo Legacy**
3. Enter the receiver's IP address, port (default 60128), and model
4. The integration validates the connection before creating the entry

### YAML (legacy)

```yaml
onkyo_legacy:
  - host: 192.168.1.23
    port: 60128
    name: Onkyo PR-SC5507
    model: PR-SC5507
    scan_interval: 10
    max_volume: 80
    retries: 2
    strict_sources: true
    sources:
      TV: tv
      Blu-ray: dvd
      PC: pc
      CD: cd
```

YAML configs are imported into config entries automatically.

### Options

| Option | Default | Range | Description |
|--------|---------|-------|-------------|
| `scan_interval` | 10 | 1–60 | Polling interval in seconds |
| `max_volume` | 80 | 1–200 | Maximum volume slider range |
| `retries` | 2 | 1–5 | Read/query attempts; writes are sent once to avoid repeating ambiguous commands |
| `strict_sources` | true | — | Fail on unknown source aliases (false = warn and skip) |

## Entities

Each receiver creates a Home Assistant device with these entities:

### Media Player (per zone)
- Power on/off, volume set/step, mute, source select

### Switches
- **Power** and **Mute** per zone (disabled by default — duplicates media_player)
- **Cinema Filter**, **Audyssey Dynamic EQ**, **Music Optimizer** (PR-SC5507)
- **12V Trigger A/B/C** (PR-SC5507)

### Numbers
- **Volume** per zone (disabled by default — duplicates media_player)
- **Sleep Timer**, **Center Level**, **Subwoofer Level** (main zone)

### Selects
- **Source** per zone (disabled by default — duplicates media_player)
- **Listening Mode** per zone
- **Dimmer Level**, **Audio Selector**, **Late Night**, **Audyssey Dynamic Volume** (main zone, disabled by default)

### Sensors (diagnostic, disabled by default)
- Audio: input terminal, input signal, sampling frequency, input channels, listening mode
- Video: input, input resolution, output, output resolution, picture mode, resolution, HDMI output
- Tuner frequency

## Services

| Service | Fields | Description |
|---------|--------|-------------|
| `onkyo_legacy.refresh` | `entity_id` (optional) | Force a state refresh |
| `onkyo_legacy.set_listening_mode` | `entity_id`, `listening_mode` | Set surround/listening mode |
| `onkyo_legacy.set_source` | `entity_id`, `source` | Set input source by name |
| `onkyo_legacy.set_volume` | `entity_id`, `volume` | Set absolute volume (0–200) |
| `onkyo_legacy.set_dimmer` | `entity_id`, `level` | Set display dimmer level |

## Architecture

```
┌─────────────────────────────────────────────┐
│              Home Assistant                 │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐      │
│  │ media_  │  │ switch/ │  │ sensor/ │      │
│  │ player  │  │ select/ │  │ number  │      │
│  └────┬────┘  └────┬────┘  └────┬────┘      │
│       └────────────┼────────────┘           │
│              ┌─────┴─────┐                  │
│              │Coordinator│ ← polling        │
│              │  (per     │ ← push listener  │
│              │   zone)   │                  │
│              └─────┬─────┘                  │
│              ┌─────┴─────┐                  │
│              │  Client   │ ← circuit breaker│
│              │ (shared)  │ ← retry+backoff  │
│              └─────┬─────┘                  │
└────────────────────┼────────────────────────┘
                     │ TCP:60128
              ┌──────┴──────┐
              │   Onkyo     │
              │  Receiver   │
              └─────────────┘
```

- **Client** — serialized framed eISCP reader with shared capped reconnect cooldown, batch queries, and probes
- **Coordinator** — `DataUpdateCoordinator` per zone; core polling publishes immediately, while optional queries run in the background and merge onto the latest state
- **Listener** — background daemon thread for unsolicited ISCP messages (push updates)
- **Profiles** — `ModelProfile` frozen dataclass with per-model capabilities, sources, and zones

### Poll and Push Ordering

Each zone tracks push revisions independently for power, volume, mute, and source. A refresh captures a field's revision immediately before starting its query batch. Pushes applied on the Home Assistant loop after that boundary take precedence when the refresh completes. A later query batch can replace an earlier push. Because eISCP has no response identifiers, a push during the same query is conservatively preferred. Power and the other core fields use separate query boundaries. Partial updates and returned snapshots copy the audio/video information dictionaries.

### Checking Network Retry Traffic

The integration uses TCP unicast to the configured receiver. Repeated TCP reconnects to an unreachable receiver can cause the host to repeat ARP resolution. Validate any change with matched 120-second captures on the Home Assistant host, using the same receiver power states and network conditions before and after:

```bash
sudo timeout 120 tcpdump -ni any -w onkyo-before.pcap 'arp or (host 192.168.1.10 and tcp port 60128)'
sudo timeout 120 tcpdump -ni any -w onkyo-after.pcap 'arp or (host 192.168.1.10 and tcp port 60128)'
```

Count ARP requests and TCP SYN attempts for the receiver in each capture; repeat for each configured receiver. Local tests do not measure ARP traffic, so they cannot establish a reduction on a live network.

## Development

### Running Tests

```bash
./.venv/bin/python -m pytest tests/ -v
```

Tests run without a real Home Assistant installation; the test suite uses lightweight stubs.

### Live Smoke Testing

Validate receiver capabilities before expanding the entity surface:

```bash
# Read-only probe sweep
./.venv/bin/python scripts/prsc5507_smoke.py --host 192.168.1.23 --model PR-SC5507

# With write tests and JSON output
./.venv/bin/python scripts/prsc5507_smoke.py --model TX-8050 --writes --output tx8050-smoke.json

# Unknown receiver with discovery
./.venv/bin/python scripts/prsc5507_smoke.py --host 192.168.1.186 --model TX-NR801 --timeout 15 --discover
```

### Project Structure

```
custom_components/onkyo_legacy/
├── __init__.py          # Entry setup, services, zone detection, migrations
├── config_flow.py       # UI/options/reconfigure flows + YAML import
├── const.py             # ModelProfile, PROFILES, constants
├── coordinator.py       # ISCP client, coordinator, parsers, listener
├── diagnostics.py       # Bug report diagnostics
├── manifest.json        # HA integration manifest
├── media_player.py      # Media player entity (per zone)
├── number.py            # Volume, sleep, level entities
├── select.py            # Source, listening mode, dimmer, etc.
├── sensor.py            # Audio/video/tuner diagnostic sensors
├── services.yaml        # Service definitions
├── strings.json         # UI translations
├── switch.py            # Power, mute, feature toggles
└── translations/
    └── en.json          # English translations
```

## Requirements

- Home Assistant 2024.1.0 or later
- Python 3.11+
- `onkyo-eiscp==1.2.7` (installed automatically)
