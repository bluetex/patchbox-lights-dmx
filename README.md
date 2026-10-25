# Patchbox Lights DMX

Raspberry Pi lighting controller for an XR18 meter stream, OLA, and four DMX
fixtures. `lights.py` is the live application; the other scripts are standalone
DMX bring-up and fade tools.

## Setup

```sh
python3 -m pip install pyserial
python3 dmx_basic.py
```

If the adapter is not `/dev/ttyUSB0`, change `PORT` in `dmx_basic.py` (some
adapters appear as `/dev/ttyACM0`).

The included ZQ01148 manual chart shows seven channels per fixture: master
dimmer, red, green, blue, strobe, control mode, and effect speed. The example
sends steady blue with strobe off and manual control selected. It configures
four fixtures at start addresses 1, 8, 15, and 22. Set the fixtures to those
addresses before running it; addresses 1, 5, 9, and 13 overlap for a
seven-channel fixture.

The frame contains 28 DMX slots, enough to reach the last channel of the
fixture at address 22. More effects and per-fixture color controls can be
added next.

## Continuous DMX test

Run the color test directly:

```sh
python3 dmx_color_test.py
```

The test assigns four distinct random colors once, then continuously sends
them at 100% without closing or reopening the serial port. It logs the frame
count once per second. Strobe and effect channels stay off. Press Ctrl+C to
send blackout and stop. `dmx_restart_test.py` remains available for comparing
process restarts.

## OLA fade test

When OLA is managing the FTDI adapter, use the OLA client test instead of
opening `/dev/ttyUSB1` from Python:

```sh
python3 dmx_ola_color_test.py
```

This sends updates to universe 0 through `olad`, with fixtures 1/8/15/22
rotating through green/red/blue/orange assignments each cycle. All four
dimmers follow a smooth repeating 0%-100%-0% cycle over 2.5 seconds. Strobe and
effect channels stay off. Press Ctrl+C to send blackout through OLA and stop.
The adapter must be patched to universe 0, and `olad` must have the FTDI USB
DMX plugin active.

## MR18 / XR18 OSC to OLA lighting app

`lights.py` subscribes to the Midas MR18 or Behringer XR18 OSC meter streams
for input channels (`/meters/1`) and Aux/Main outputs (`/meters/5`), then sends
fixture values to OLA universe 0. OLA owns the FTDI adapter
and refreshes the DMX stream; `lights.py` does not open `/dev/ttyUSB1`.

Before running it, make sure the FTDI USB DMX plugin is active and output port
1 is patched to universe 0. Then run:

```sh
python3 lights.py
```

The app serves a mobile-friendly web panel on port 8080. Open
`http://<pi-address>:8080/` from a device on the same private network. The panel
provides Spotlight, Color cycle, Chase, and Blackout modes, along with
brightness and sensitivity controls. Color cycle and Chase can advance on a
timer, band peaks, or beat estimates from a selected kick input. Name the kick
channel `Kick` in the XR18/MR18 scribble strip and use **Find Kick** in Audio
settings to assign it automatically; manual Input 1–16 selection is also
available. Color cycle supports chosen palettes and unison or spread fixture
colors. Chase supports direction and optional strobing. Meter-triggered white
spotlights can remain overlaid on other effects.

The Settings page lets you select the supported console and its OSC IP address,
and choose the fixture count. The current seven-channel fixture layout assigns
sequential DMX start addresses (d001, d008, d015, d022, …), supporting up to 73
fixtures in one universe. Each fixture can be enabled independently and assigned
a spotlight source: Input 1–16, Aux 1–6, Main L, or Main R. A selected source
creates a white spotlight when its meter crosses the trigger threshold. Settings
are saved on the Pi in
`/home/patch/lights/control_settings.json`.

