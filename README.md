# Onkyo Legacy Custom Integration

Custom Home Assistant integration for legacy Onkyo processors that still answer direct ISCP commands but do not work with the current built-in integration discovery flow.

## Install

Copy `custom_components/onkyo_legacy` into your Home Assistant config directory:

```text
config/
  custom_components/
    onkyo_legacy/
```

Restart Home Assistant after copying the files.

## Configuration

Add this to `configuration.yaml`:

```yaml
onkyo_legacy:
  - host: 192.168.1.23
    port: 60128
    name: Onkyo PR-SC5507
    scan_interval: 10
    max_volume: 80
    sources:
      TV: tv
      Blu-ray: dvd
      PC: pc
      CD: cd
```

## Exposed Entities

- One `media_player` for the main zone.
- One `select` for listening mode if `LMDQSTN` responds.
- Optional `switch` entities for speaker A/B only if `SPAQSTN` and `SPBQSTN` respond.

On the tested PR-SC5507 target, core media commands and listening mode queries responded, while speaker A/B queries timed out. Expect the media player to work even if the speaker switches are not created.
