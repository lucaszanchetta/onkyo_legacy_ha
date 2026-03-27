# Onkyo Legacy Custom Integration

Custom Home Assistant integration for legacy Onkyo receivers that still answer direct ISCP commands but do not work well with the current built-in integration discovery flow.

## Install

Copy `custom_components/onkyo_legacy` into your Home Assistant config directory:

```text
config/
  custom_components/
    onkyo_legacy/
```

Restart Home Assistant after copying the files. The YAML config is imported into a Home Assistant config entry so the processor can appear as a real HA device with grouped entities.

## Configuration

Add this to `configuration.yaml`:

```yaml
onkyo_legacy:
  - host: 192.168.1.23
    port: 60128
    name: Onkyo PR-SC5507
    model: PR-SC5507
    scan_interval: 10
    max_volume: 80
    sources:
      TV: tv
      Blu-ray: dvd
      PC: pc
      CD: cd
  - host: 192.168.1.14
    port: 60128
    model: TX-8050
    scan_interval: 10
    max_volume: 80
    sources:
      DVD: dvd
      TAPE: tv/tape
      PHONO: phono
      CD: tv/cd
      FM: fm
      AM: am
      INTERNET RADIO: internet-radio
      NETWORK: net
```

## Exposed Device And Entities

Each configured receiver is attached to one Home Assistant device.

- One `media_player` for the main zone.
- Additional `media_player` entities for Zone 2 and Zone 3 when the receiver responds to the zone-specific core ISCP queries.
- `select` entities for source, listening mode, dimmer level, and audio selector when those queries respond.
- `switch` entities for power, mute, and 12V trigger A/B/C when those queries respond.
- `number` entities for volume, sleep timer, center temporary level, and subwoofer temporary level when those queries respond.
- Diagnostic sensors for audio/video metadata on the PR-SC5507, plus tuner frequency on the TX-8050.

Zone 2 and Zone 3 stay grouped under the same Home Assistant device as the receiver. Extra zones currently expose the proven core controls only: power, mute, volume, and source selection.

The existing `sources:` mapping is reused for every zone. Main-zone labels are filtered automatically so Zone 2 and Zone 3 only show inputs supported by that zone on the receiver. Displayed source names are normalized to the receiver's canonical Onkyo names instead of preserving arbitrary YAML labels.

The default surface is intentionally limited to controls that have been proven queryable on each model.

## Live Smoke Validation

A dedicated live smoke runner is included to validate supported receivers:

```bash
./.venv/bin/python scripts/prsc5507_smoke.py --model PR-SC5507
```

To also run a small reversible write test subset and save the report:

```bash
./.venv/bin/python scripts/prsc5507_smoke.py --model TX-8050 --writes --output tx8050-smoke.json
```

For an older or unprofiled receiver, run a read-only legacy probe sweep with a longer timeout and optional UDP discovery probe:

```bash
./.venv/bin/python scripts/prsc5507_smoke.py --host 192.168.1.186 --model TX-NR801 --timeout 15 --discover
```

You can also override the exact raw commands to test:

```bash
./.venv/bin/python scripts/prsc5507_smoke.py --host 192.168.1.186 --model TX-NR801 --commands PWR,MVL,AMT,SLI,LMD
```

If that still times out, add `--raw-probe` to try several safe TCP framing variants for the first query command and capture any raw bytes returned by the receiver.

The smoke runner classifies commands as queryable or failing on the live box and should be used as the source of truth before expanding the entity surface.

For PR-SC5507-class profiles, the default smoke query set includes the Zone 2 and Zone 3 core command families. For TX-8050, the default query set includes Zone 2 core commands as well.
