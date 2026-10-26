# Patchbox Lights — Technical Reference

This document describes the implementation in `lights.py` and the current
seven-channel fixture profile. It is intended for development, deployment, and
diagnosis. The user-facing overview and setup are in [README.md](README.md).

## System architecture

```text
XR18 / MR18
  ├─ OSC query/reply: /meters/1 ───────┐
  ├─ OSC query/reply: /meters/5 ───────┤ UDP, port 10024
  └─ OSC names + /info probe ──────────┘
                                       v
Raspberry Pi                           lights.py
  ├─ Flask control panel :8080          ├─ OSC receiver / parser
  ├─ persisted controls JSON            ├─ meter smoothing and event detectors
  ├─ console status monitor             ├─ pattern renderer, 7ch fixture buffers
  └─ main 2 ms loop                     └─ latest-frame handoff to OLA client
                                              |
                                              | OLA client, universe 0
                                              v
                                      olad → FTDI USB-DMX adapter → fixtures
```

The process has distinct responsibilities:

1. **Meter renewal thread** sends the two subscription requests every four
   seconds and wakes immediately when the selected OSC source changes.
2. **Engine loop** runs approximately every 2 ms. It drains available UDP
   datagrams, accepts packets only from the configured source IP, updates meter
   state, advances envelopes and pattern phases, renders the DMX buffer, and
   publishes a new frame at a 40 ms cadence.
3. **OLA output thread** owns an OLA `ClientWrapper` event loop. It reads the
   newest immutable frame and submits it to universe 0 every 40 ms. The app
   never opens the FTDI serial port itself.
4. **Flask thread** serves the UI and JSON API on `0.0.0.0:8080`; control
   changes are synchronized with the engine using an `RLock`.
5. **Console monitor thread** prints one status line per second, including
   meter age, packet counts, levels, fixture channel values, OLA submissions,
   engine-loop age, and errors.

The UI polls `/api/status`; lighting continues when no browser is connected.
On normal shutdown, `close()` clears fixture values and requests a final
blackout frame through OLA.

## Supported mixer and network protocol

### Mixer support

The settings model exposes Behringer XR18 and Midas MR18. The implementation
uses the shared X AIR OSC behavior and UDP port **10024**. The current default
source IP is `192.168.0.18`; it is configurable in Audio settings and persisted.
The service subscribes to:

| OSC address | Purpose | Values used |
| --- | --- | --- |
| `/meters/1` | Input-channel meters | Input 1–16 |
| `/meters/5` | Output meters | Aux 1–6, Main L, Main R |
| `/ch/NN/config/name` | Input scribble-strip name query | Inputs 1–16 |
| `/bus/N/config/name` | Aux scribble-strip name query | Aux 1–6 |
| `/lr/config/name` | Main scribble-strip name query | Main L/R label |
| `/info` | Test-connection probe | Accepts `/info` or `/xinfo` reply |

The subscription payloads are OSC messages with a string argument; the app
renews both streams at four-second intervals. Input and output name queries
use short-lived UDP sockets. Input names are cached for 60 seconds for the
currently selected mixer IP. Whenever a new set is fetched (cache miss, expiry,
or explicit refresh), the app scans it for a suggested kick channel and, when
found, writes that input to `tempo_input`. The user can then select another
input; cached reads do not overwrite that manual choice. A later explicit
refresh or new name fetch performs the suggestion scan again. Test connection
waits up to 1.5 seconds for a reply. A successful test verifies that an OSC
endpoint answered the `/info` probe; live meter status separately confirms
that meter data is arriving.

### Meter packet decoding

`parse_meter_values()` accepts one OSC message whose address exactly matches
the requested stream and whose argument is a single OSC binary blob. The blob
layout decoded by the app is:

```text
OSC address: NUL-terminated, 4-byte aligned
OSC tags:    ,b\0\0
blob size:   big-endian uint32
blob body:
  channel count: little-endian uint32
  channel data:  channel count × signed little-endian int16
```

Packets with a bad address, type tag, truncated blob, or too few samples are
discarded. `/meters/1` requires at least 16 samples; `/meters/5` requires at
least 8. For the output stream, sample indexes 0–5 mean Aux 1–6, then indexes
6 and 7 mean Main L and Main R. In the shared internal source numbering those
are sources 17–24; input sources occupy 1–16.

The signed meter value is represented in 1/256 dB units:

```text
dB = meter_value / 256
```

`clean_vu()` clamps the displayed/derived level to 0 dB, maps values at or
below -60 dB to 0, maps values at or above -6 dB to 1, and linearly maps the
interval between those endpoints to 0–1. The UI's input, band, and kick meters
are level views; the lighting response uses separate threshold, hold, and
envelope state.

### Channel names and kick discovery

Name reads send OSC get requests and collect matching string replies from the
configured mixer IP. Suggested-kick detection normalizes punctuation/case and
prefers an exact `Kick` label; it also accepts labels containing `kick`,
`bass drum`, or exactly `BD`. If several match, an exact `Kick` match wins,
then the lowest input number. `Find Kick` uses the same matching rule. Names
are display metadata: the selected meter remains the numeric input channel.

## Audio analysis and control model

### Independent sensitivities

Three controls alter audio interpretation independently:

- `sensitivity`: Spotlight and output-meter trigger levels.
- `band_sensitivity`: band-group level used for ambient response.
- `kick_sensitivity`: selected kick input used for beat detection.

For each, the UI value is a linear multiplier from 0.5× to 2.0×. The app
converts it to a dB offset as `12 × log2(multiplier)` and adds that offset to
the incoming meter value before level mapping. Thus the adjustment is roughly
−12 dB to +12 dB across the slider range. A larger multiplier moves a given
meter sample upward in level; the UI communicates this as quieter signal
triggering more easily.

### Spotlight trigger and envelope

For each of up to 24 selectable sources, the engine tracks a peak state, a
hold deadline, and the rendered white-light envelope. Current constants are:

| Parameter | Value | Effect |
| --- | ---: | --- |
| Trigger threshold | -10 dB | Arms/raises a source peak |
| Release threshold | -24 dB | Rearms the detector after the signal falls |
| Peak hold | 1.0 s | Holds a captured peak before peak decay |
| Peak decay | 2.0 normalized units/s | Lowers the held peak after its deadline |
| White attack | 0.2 s | Limits how fast a white spotlight rises |
| White release | 3.0 s | Tapers white after the target level falls |
| Retrigger delta | 0.08 | Requires a material increase to refresh the captured peak |

The rendered white envelope is multiplied by the independent
`spotlight_brightness` master-dimmer value (0–1). In Spotlight mode, colored
ambient/Band output instead uses `band_brightness`. These are separate DMX
master-dimmer controls; the `brightness` control remains the master for Color
cycle and Chase. A meter-triggered white overlay over Color cycle/Chase uses
`spotlight_brightness` as well.

Input channels feed the first 16 sources. `/meters/5` feeds the next eight.
Only the selected spotlight source for each enabled fixture is rendered as
white. Repeated higher peaks can refresh a hold; a lower signal at or below
the release threshold rearms the detector. The white output has its own attack
and release ramp, so it is not a direct copy of instantaneous meter level.

### Band group and influence

The band source group has two assignment modes:

- **Automatic:** all Input 1–16 sources except inputs assigned to spotlight
  fixtures.
- **Custom:** exactly the user-selected input list. The list may be empty.

The band level is the maximum adjusted sample among the effective band inputs,
then smoothed with `BAND_SMOOTHING_ALPHA = 0.20` per received meter packet.
Output meters are available as spotlight sources, but the band assignment is
specifically an input-channel group.

When a band lift starts (smoothed level ≥ 0.25), the engine selects a color
from a six-color palette, begins a swell, and tracks local peaks and valleys.
Color activity releases at ≤ 0.10. A captured band peak is held for 1 s, then
decays at 2.0 normalized units/s. Swell attack/release constants are 0.12 s
and 0.50 s; band ambient intensity is scaled to 50% before the master
brightness channel is applied.

The **Spotlight audio influence** slider is a blend between band level and
kick-pulse influence for ambient output while Spotlight mode is active. It does
not change which meters produce white spotlights. The `audio_enabled` switch
disables meter-driven behavior; meter polling continues so the panel can still
show signal levels.

### Kick tempo detector

Beat sync reads only the selected `tempo_input` (Input 1–16). It uses two
normalized thresholds: trigger at 0.46 and rearm at 0.22. A crossing produces
one beat event and a kick pulse whose linear decay duration is 0.32 s. Intervals
shorter than 0.25 s are ignored; intervals longer than 2.0 s clear the estimate.
The detector retains the latest seven valid intervals, takes their median, and
converts the median interval to BPM. A 0.35 new / 0.65 previous exponential
blend smooths successive BPM estimates.

The UI can select timer, band peaks, or beat sync as the event source for Color
cycle and its Chase behavior. Band peaks are detected on each selected Band
input independently, then debounced globally so a single musical hit across
multiple channels advances the effect once. This prevents a sustained loud
channel from masking transients on other Band inputs. Kick beat detection uses
a lower trigger/release threshold to re-arm between hits. `trigger_division`
can divide those event counts by 1, 2, 4, or 8. Beat sync requires a selected
kick input. The status API reports `band_peak_events` and `tempo_beat_events`
for diagnosing event activity. In Spotlight mode, band meter rolls and kick pulses are combined according
to `kick_influence`. The selected Kick input is excluded from effective Band
inputs, so the balance compares separate sources. In Color cycle / Chase, the
same control blends the Band swell with a smoothed live Kick meter and beat
pulse to modulate palette brightness; with
audio response disabled, Color cycle retains its fixed ambient level. White
meter overlays remain independent of this balance.

## Pattern renderer

Top-level pattern names are persisted as `spotlight` and `color_cycle`.
Color cycle has a `cycle_style` of `cycle` or `chase`; older saved `pattern:
chase` values migrate to Color cycle with Chase selected. Blackout is a
separate boolean state; the legacy string `blackout` remains accepted for
compatibility, but the UI control toggles blackout without discarding the
selected pattern.

### Spotlight

The baseline ambient color is purple. Band activity lifts and varies the
ambient fixtures. The renderer rotates colors by fixture during mixed-color
effects, alternates unison and mixed arrangements during longer runs, and
combines band phase, kick beat phase, pair accents, and swell/dive gains. The
engine changes this evolution after 32 detected band peaks. White meter
spotlights take priority for their assigned fixtures when active.

Spotlight mode exposes two independent brightness controls: white spotlight
output uses `spotlight_brightness`, while its colored Band/ambient output uses
`band_brightness`. The generic `brightness` value is not applied in this mode.

### Color cycle

The user selects one or more named RGB colors. The timer trigger uses
`cycle_speed` seconds per color (0.25–15 s). Event triggers advance according
to `trigger_division`. Unison gives every fixture the same palette position;
spread offsets the color index by fixture group: each group contains
`concurrent_fixtures` neighboring fixtures, so a value of 2 shows two colors
at a time across four fixtures. `cycle_transition` selects
`cut` (immediate change, the default) or `fade`. Fade linearly interpolates
from the previous palette color to the new color over 0.5 s after each advance;
timer-driven fades are capped at the configured seconds-per-color interval.
The initial color and changes to cycle configuration start without an
unrequested transition. Ambient palette intensity is 50% before master
brightness.

The control **Show white meter spotlights over Color cycle / Chase** is the
`spotlight_overlay` boolean. When enabled, an assigned fixture's live white
meter envelope temporarily replaces that fixture's cycle or chase color.
Disabling it leaves the selected effect visible on every enabled fixture;
Spotlight mode always uses the assigned meter sources.

### Chase behavior

Chase uses the same non-empty `cycle_colors` selection as Color cycle. At each
chase event, the color advances in the selected list order; selecting one color
keeps the chase that color. Direction is forward, reverse, or bounce; chase
speed ranges from 0.1× to 8×. There are `concurrent_color_chases` chase paths, distributed around the
fixture ring. Each path takes the next color from `cycle_colors`, wrapping when
there are fewer selected colors than paths. Each path covers
`concurrent_fixtures` neighboring positions (wrapping at the end of the rig),
so a value of 2 moves two fixtures together per path. Both controls are capped
at the configured fixture count. Other fixtures
use the existing tail levels: 0.18 when adjacent to the active group and 0.05
elsewhere. Optional strobe applies DMX value 24 to every active fixture's
strobe channel; all others receive zero.

### Blackout and fixture enable

Blackout, a legacy blackout pattern value, or a disabled fixture sets its
master dimmer to zero. Switching blackout off restores the currently selected
pattern because the pattern setting is retained independently.

## DMX output

### Transport and frame timing

`OlaDmxOutput` submits frames through the OLA client to **universe 0**. It does
not use `pyserial`, enumerate `/dev/ttyUSB*`, or control the adapter's FTDI
serial settings. Those direct-serial operations exist in standalone diagnostic
scripts and must not compete with OLA for the same interface.

The app's renderer runs on the engine loop, while the OLA event loop sends the
most recently published buffer every 40 ms (nominally 25 updates/s). A lock
protects the latest-frame snapshot. The emitted frame omits buffer index 0;
DMX slot 1 is stored at `dmx_buffer[1]`, matching the fixture start address
convention. On shutdown the OLA thread sends a zero-filled frame.

### Seven-channel fixture profile

For start address `A`, the app writes:

| DMX slot | Buffer index | Function | Current value behavior |
| ---: | ---: | --- | --- |
| `A` | `A` | Master dimmer | 0 when fixture disabled/blackout; otherwise `255 × brightness` |
| `A+1` | `A+1` | Red | 0–255 RGB component |
| `A+2` | `A+2` | Green | 0–255 RGB component |
| `A+3` | `A+3` | Blue | 0–255 RGB component |
| `A+4` | `A+4` | Strobe | Normally 0; Chase strobe uses 24 on active fixture |
| `A+5` | `A+5` | Control mode | 0 (manual) |
| `A+6` | `A+6` | Effect speed | 0 (unused) |

This is the assumed fixture profile. The fixture menu/manual must match it; the
app does not issue fixture setup commands and does not rewrite the fixture's
DMX start address.

Start addresses are generated by:

```python
address(i) = 1 + 7 * i       # i is zero-based
```

The addresses begin at d001, d008, d015, d022, and continue through d505 for
fixture 73. `MAX_FIXTURE_COUNT = floor(512 / 7) = 73`. The final fixture uses
slots 505–511. A four-fixture frame reaches slot 28.

## Web interface and API

The Flask app binds to `0.0.0.0:8080`. Pages are embedded in `lights.py` as
HTML/CSS/JavaScript strings; the project currently has no separate frontend
build step.

| Route | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Live controls and meter display |
| `/settings` | GET | Settings landing page |
| `/settings/fixtures` | GET | Fixture count, enable state, spotlight source, band-input summary |
| `/settings/audio` | GET | Mixer, audio sensitivity, band, and kick settings |
| `/settings/audio/help` | GET | Internal XR/MR18 OSC setup help |
| `/api/status` | GET | Controls, fixture addresses, meter levels, tempo, age, frame count, error |
| `/api/control` | POST | Validate, apply, and persist supported controls |
| `/api/test-osc` | POST | Probe the supplied mixer IP with `/info` |
| `/api/find-kick` | POST | Read names and return the best kick-input match |
| `/api/input-names` | GET | Cached/fresh input and output labels |
| `/api/input-names/refresh` | POST | Force input/output label refresh |

`/api/control` accepts a JSON object containing one or more supported fields.
It validates ranges/enums before replacing the current controls. Persistent
settings are written to a temporary file and atomically replaced at
`/home/patch/lights/control_settings.json`.

### Control fields

| Field | Type / range | Persisted |
| --- | --- | --- |
| `sensitivity`, `band_sensitivity`, `kick_sensitivity` | Number, 0.5–2.0 | General sensitivity is runtime-only; band and kick sensitivity persist |
| `audio_enabled` | Boolean | Yes |
| `brightness` | Number, 0.0–1.0; Color cycle / Chase master | No |
| `spotlight_brightness` | Number, 0.0–1.0; white fixtures | Yes |
| `band_brightness` | Number, 0.0–1.0; Spotlight ambient fixtures | Yes |
| `pattern` | `spotlight`, `color_cycle`; legacy `chase` maps to color-cycle Chase | Yes |
| `blackout` | Boolean | Yes |
| `fixture_count` | Integer, 1–73 | Yes |
| `fixture_enabled` | Boolean list, one per fixture | Yes |
| `spotlight_inputs` | Integer list, 0–24; 0 means none | Yes |
| `band_mode` | `auto` or `custom` | Yes |
| `band_inputs` | Unique input-number list, 1–16 | Yes |
| `cycle_colors` | Non-empty unique list of named palette colors | Yes |
| `cycle_speed` | Seconds per color, 0.25–15 | Yes |
| `cycle_layout` | `unison` or `spread` | Yes |
| `cycle_transition` | `cut` or `fade` | Yes |
| `cycle_style` | `cycle` or `chase` | Yes |
| `concurrent_fixtures` | Integer, 1 through the configured fixture count; fixtures per color group / chase path | Yes |
| `concurrent_color_chases` | Integer, 1 through the configured fixture count; simultaneous chase paths | Yes |
| `behavior_trigger` | `timer`, `band_peaks`, `beat_sync` | Yes |
| `trigger_division` | 1, 2, 4, or 8 | Yes |
| `tempo_input` | 0 (off) or Input 1–16 | Yes |
| `kick_influence` | Number, 0.0–1.0 | Yes |
| `spotlight_overlay` | Boolean | Yes |
| `chase_direction` | `forward`, `reverse`, `bounce` | Yes |
| `chase_speed` | Number, 0.1–8.0 | Yes |
| `chase_color` | Legacy API alias; sets `cycle_colors` to that single color | No |
| `chase_strobe` | Boolean | Yes |
| `osc_model`, `osc_ip` | Supported model / valid IPv4 address | Yes |

Most persistent controls are written on each update. Color cycle / Chase
brightness and general sensitivity are runtime-only by default; the home-page
**Save settings** action writes a full snapshot so these values also survive a
restart. Spotlight white and Band brightness persist on each change. The action
uses `POST /api/save-settings` and writes the current controls atomically to the
settings file.

## Configuration and state

`DEFAULT_CONTROLS` in `lights.py` defines first-run values. At startup,
`load_control_settings()` overlays validated saved values from
`/home/patch/lights/control_settings.json`. The file is deliberately excluded
from Git. Do not commit a venue's network address or live operating choices as
application defaults unless the project intends to change defaults for every
installation.

Important defaults include:

```text
OSC source:       192.168.0.18
Fixture count:    4
Fixture starts:   d001, d008, d015, d022
Spotlight inputs: 1, 2, 3, 4
Band mode:        automatic (exclude spotlight and Kick inputs)
Audio response:   on
Pattern:          Spotlight
Cycle style:      Cycle through colors
Concurrent:       2 fixtures
OLA universe:     0
Web port:         8080
```

## Deployment and operations

The live app is started directly with:

```sh
python3 lights.py
```

The Pi installation also runs it as `lights.service`. Useful commands:

```sh
sudo systemctl status lights.service
sudo journalctl -u lights.service -f
sudo systemctl restart lights.service
```

The app's once-per-second monitor line includes the current numeric fixture
channels. `meters age` shows how recently a valid packet arrived;
`UDP=received/valid` separates all received datagrams from recognized meter
packets; `req` counts the two subscription datagrams sent per renewal. `OLA
updates` reports the output thread's submitted frame count. `loop_age` helps
spot a stalled engine loop; `error` reports the latest OLA or network issue.

### Layered diagnosis

Follow the path in order:

1. **Panel:** open the Pi address at port 8080. If unavailable, inspect
   `lights.service` and its logs.
2. **OSC reachability:** use Audio settings → Test connection. A successful
   `/info` probe is a quick network/protocol check.
3. **Meter stream:** look for `Live` and fresh `meters age` in the panel/log.
   The test probe alone does not prove that meter subscriptions are flowing.
4. **Renderer:** inspect meter bars, selected fixture sources, enabled state,
   brightness, audio response, and blackout state. The console line exposes
   the rendered channel bytes.
5. **OLA:** check that `olad` is running, the adapter is detected, and its
   output port is patched to universe 0. The app intentionally does not open
   the serial device.
6. **DMX/fixture:** verify daisy-chain continuity, fixture DMX mode, seven
   channel personality, and exact start address. DMX carries channel values;
   it does not change a fixture's menu-selected start address.

The Web panel binds to all network interfaces and currently has no login or
authentication layer. Keep access on a trusted private network and do not
forward port 8080 to the public internet.

## Repository utilities

| File | Notes |
| --- | --- |
| `dmx_basic.py` | Direct serial test using pyserial. Use only when OLA is not holding that adapter. |
| `dmx_color_test.py` | Direct serial fixture color test. Do not run concurrently with OLA on the same adapter. |
| `dmx_ola_color_test.py` | OLA client-based color/fade test for universe 0. |
| `dmx_restart_test.py` | Adapter/process restart diagnostic. |
| `tempo_fog.py` | Separate tempo/fog experiment, outside the live app pipeline. |

## Implementation constants at a glance

| Constant | Value |
| --- | ---: |
| Engine sleep interval | 2 ms |
| DMX frame publication interval | 40 ms / 25 Hz |
| Meter subscription renewal | 4 s |
| Stale-meter timeout | 1 s |
| UI meter freshness indication | 1.5 s |
| Web bind | `0.0.0.0:8080` |
| OSC UDP port | 10024 |
| OLA universe | 0 |
| DMX channels per fixture | 7 |
| Maximum fixtures | 73 |

These values describe this implementation revision; the authoritative values
are the constants and validation logic in `lights.py`.
