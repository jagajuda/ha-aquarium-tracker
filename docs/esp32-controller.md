# ESP32 aquarium controller — rough plan

Status: **draft for discussion**. Nothing here is built or ordered yet. Prices are left out on purpose;
they change too fast and depend on local sourcing.

## Goals

| Phase | What | Why |
|---|---|---|
| 1 | Water temperature per tank | Heater failures (the nano heater already mis-calibrated once) |
| 1 | Leak detection under both tanks | Cheap insurance for an 80×40×40 tank on a cabinet |
| 2 | pH on the big tank | Cycle diagnostics now; CO₂ tuning later if CO₂ is added |
| 2 | Heater cut-off on over-temperature | Safety: a stuck heater must not cook the tank |
| 3 | Chihiros light states (B20, B80) on the timeline | See photoperiod next to algae / plant changes |
| later | Dosing pump, water level, flow | Open-ended |

## Architecture

```
 DS18B20 ─┐
 pH (EZO) ─┤ I²C / 1-Wire
           ESP32 (ESPHome) ──Wi-Fi──► Home Assistant ──► aquarium_tracker
                │                         ▲    ▲
                └─ Bluetooth proxy ───────┘    │
                    (Chihiros BLE lights)      │
 Zigbee leak sensors ── Zigbee coordinator ────┤
 Zigbee smart plug (heater) ───────────────────┘
```

- **ESPHome** on the ESP32: sensors are written in YAML, flashed once over USB, then updated over Wi-Fi.
  Entities appear in HA automatically through the native API. No custom firmware to maintain.
- **Leak and heater power go over Zigbee**, not through the ESP32: the Zigbee coordinator is already there,
  battery leak sensors need no wiring near water, and switching mains through an off-the-shelf certified plug
  is safer than a DIY relay board.
- **Safety logic lives in HA automations** (over-temperature → plug off → notify). The heater's own
  thermostat stays as the first line of defence.
- The same ESP32 doubles as an **ESPHome Bluetooth proxy**, so the Chihiros lights can be reached even if the
  HA laptop is in another room.

## Bill of materials (phase 1 + 2)

| Qty | Part | Notes |
|---|---|---|
| 1–2 | ESP32 dev board (ESP32-WROOM-32 DevKitC or ESP32-S3 DevKitC) | One per tank location if the nano moves to the bedroom; one is enough while the tanks are side by side |
| 2 | DS18B20 waterproof stainless probe (1 m cable) | One per tank; same 1-Wire bus if one ESP32 |
| 1 | 4.7 kΩ resistor | 1-Wire pull-up to 3.3 V |
| 1 | pH circuit — **option A or B below** | Phase 2 |
| 1 | pH calibration buffers 4.00 and 7.00 (+ KCl storage solution) | Needed for either option |
| 2 | Zigbee water leak sensor | Under each tank / cabinet |
| 1 | Zigbee smart plug with power metering | Heater cut-off; power reading also shows when the heater is actually heating |
| 1 | 5 V / 2 A USB power supply + cable | Mains stays away from the water side |
| 1 | Enclosure | 3D-printed PETG, mounted high and dry; cable glands for probe leads |
| — | Perfboard or screw-terminal breakout, JST/Dupont leads | Avoid loose jumpers near water |

### pH options

| | A. DFRobot Gravity analog pH v2 + ADS1115 | B. Atlas Scientific EZO-pH + isolated carrier + lab-grade probe |
|---|---|---|
| Cost | Low | ~3–4× A |
| ESPHome | Community custom component (C++ include) | Native `ezo` component (I²C) |
| Noise | Needs added isolation; drifts with heater/pump interference | Isolated carrier board included |
| Calibration | Voltage-based, by hand | Commands from ESPHome/HA (`Cal,mid,7.00`…) |
| Verdict | Fine to experiment | **Recommended** for a value you will act on |

pH electrodes are consumables (roughly a year in continuous use) and need recalibration every few weeks
regardless of option.

## Wiring sketch (single ESP32)

| ESP32 pin | Goes to |
|---|---|
| 3V3 | DS18B20 VDD (red), 4.7 kΩ to data |
| GND | DS18B20 GND (black), EZO carrier GND |
| GPIO4 | DS18B20 data (yellow) — both probes in parallel |
| GPIO21 (SDA) / GPIO22 (SCL) | EZO-pH isolated carrier |
| 5V (VIN) | EZO carrier VCC (check carrier spec) |

## How it lands in aquarium_tracker

- Continuous values (temperature, pH every few seconds) stay in HA's recorder / long-term statistics —
  they are not events and would flood the timeline.
- Tracker work needed (see README → Future work):
  - map a tank parameter to a live sensor (`live_sensors: {temp: sensor.big_tank_temperature}`), plot it as a
    continuous line on the chart and show it in the value tiles
  - optional threshold alerts per tank (e.g. temp outside 22–27 °C) through the same notification system
  - light on/off via `watch_entities` (already supported) once the Chihiros entities exist

## Open questions for the next session

1. One ESP32 for both tanks, or one per tank (depends on the nano moving to the bedroom)?
2. pH: option A to experiment first, or straight to B?
3. Heater cut-off threshold and whether the plug should switch back on automatically.
4. Leak sensor placement: inside the cabinet, behind it, or both?
5. Any local display on the controller, or HA only?
