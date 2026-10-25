# Lunatone DALI WLAN for Home Assistant

Custom integration for the **Lunatone DALI WLAN gateway** (article 24138478,
firmware 1.1.2/1.0.10). This gateway speaks a JSON websocket protocol on
`ws://<gateway>/`, unlike the DALI-2 IoT gateway handled by the core `lunatone`
integration.

## Features

- **Push buttons as automation triggers.** DALI-2 input devices such as the
  Lunatone MC2 show up as devices with one event entity per button and device
  triggers for short press, double press, long press start/repeat/stop,
  pressed and released.
- **Occupancy and light sensors.** DALI-2 occupancy sensors (IEC 62386-303)
  get *Occupancy* and *Movement* binary sensors, light sensors (IEC 62386-304)
  an *Illuminance* sensor in lux. Both come with the standard Home Assistant
  device triggers and conditions. Tested with the Lunatone DALI-2 CS Mini.
- **Scenes as a service.** `lunatone_wlan.go_to_scene` recalls a scene on all
  lights, a group or a single address.
- **Bus monitor.** Every frame on the bus is fired as a `lunatone_wlan_monitor`
  event, including the scene commands buttons send directly to the lights.
- **Raw frames.** `lunatone_wlan.send_frame` sends any 16 or 24-bit frame and
  returns the answer.
- **Bus scan.** Control devices are found on every start. Use the *Scan bus*
  button or `lunatone_wlan.scan_bus` after adding or re-addressing devices.
  When an address now holds a different device, its entities are replaced.

Buttons keep working when Home Assistant is down if the MC2 sends scene
commands to the lights itself. Home Assistant only sees the events, so you can
leave simple buttons on the bus and handle only the complex ones (for example
single vs. double press) in automations.

## Installation

### HACS

1. HACS → ⋮ → *Custom repositories* → add this repository as *Integration*.
2. Install *Lunatone DALI WLAN* and restart Home Assistant.

### Manual

Copy `custom_components/lunatone_wlan` into your `config/custom_components`
folder and restart Home Assistant.

Then add the integration under *Settings → Devices & services* and enter the
gateway's IP address.

## Connection limit

The gateway accepts at most three websocket clients at a time. The Lunatone
software and the gateway's web page each use one. Close them when you are
done, otherwise Home Assistant may be refused after a reconnect.

## Configuring the MC2 inputs

Home Assistant can only tell inputs apart if their events carry the device
address. In the Lunatone software, for each input instance:

1. **Enable** the instance. Disabled instances do not send events.
2. Set the **event scheme** to **device/instance**. The default *instance*
   scheme only sends the instance number, which is the same on every MC2.
3. Select the **events** you need in the event filter (short press,
   double press, long press, ...).

The scan logs a warning for enabled inputs that still use a scheme without
the device address.

## Examples

Double press of button 1 on A1:

```yaml
triggers:
  - trigger: device
    domain: lunatone_wlan
    device_id: <device id of "DALI input device A1">
    type: double_press
    subtype: button_1
actions:
  - action: lunatone_wlan.go_to_scene
    data:
      group: 6
      scene: 2
```

Any button event, without the device registry:

```yaml
triggers:
  - trigger: event
    event_type: lunatone_wlan_button
    event_data:
      address: 2
      instance: 3
      type: short_press
```

Scene commands sent by buttons:

```yaml
triggers:
  - trigger: event
    event_type: lunatone_wlan_monitor
    event_data:
      target: group
      group: 6
      scene: 0
```

## Events

`lunatone_wlan_button`:

| Field | Example |
| --- | --- |
| `device_id` | device registry id, `null` if the sender is unknown |
| `address` | `2` |
| `instance` | `3` |
| `type` | `short_press`, `double_press`, `long_press_start`, `long_press_repeat`, `long_press_stop`, `pressed`, `released`, `button_stuck`, `button_free` |
| `scheme` | `device_instance` |

`lunatone_wlan_occupancy`: `device_id`, `address`, `instance`, `occupied`,
`movement` and `repeat` (periodic report without change).

`lunatone_wlan_illuminance`: `device_id`, `address`, `instance` and
`illuminance`.

IEC 62386-304 light values are relative. They are reported at the sensor's
resolution, which for the Lunatone CS Mini (11 bit, 0–2047) is 1 lx per step.

`lunatone_wlan_monitor` contains `bits`, `data` (list of bytes), `hex` and a
decoded description: `frame` (`command`, `arc_power`, `special_command`,
`event`, `device_command`), the `target` with `group` or `address`, `opcode`
and `scene` for scene commands, or the event source for 24-bit events.

## Protocol

Reverse engineered from the Lunatone Windows software:

| Direction | Message |
| --- | --- |
| ← | `{"type":"info","data":{"name":…,"version":…,"device":{"serial":…}}}` on connect |
| → | `{"type":"filtering","data":{"ping":true,"group":true}}` |
| → | `{"type":"daliFrame","data":{"daliData":[255,17],"line":0,"numberOfBits":16,"mode":{"priority":4,"sendTwice":false,"waitForAnswer":false}}}` |
| ← | `{"type":"daliFrame","data":{"line":0,"result":0}}` once per transmission (twice with `sendTwice`) |
| ← | `{"type":"daliAnswer","data":{"line":0,"result":8,"daliData":42}}` with `waitForAnswer`, `result` 0 without answer |
| ← | `{"type":"daliMonitor","data":{"bits":24,"data":[6,128,2],"line":0,…}}` for every frame on the bus |

## Development

```sh
uv venv --python 3.14 && uv pip install -r requirements_test.txt
.venv/bin/pytest
```

The tests run against a fake gateway (`tests/fake_gateway.py`) that answers
like the real one.
