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

## XR18 OSC to OLA lighting app

`lights.py` subscribes to the XR18 `/meters/1` OSC stream and sends its
fixture values to OLA universe 0. OLA owns the FTDI adapter and refreshes the
DMX stream; `lights.py` does not open `/dev/ttyUSB1`.

Before running it, make sure the FTDI USB DMX plugin is active and output port
1 is patched to universe 0. Then run:

```sh
python3 lights.py
```

The app serves a mobile-friendly web panel on port 8080. Open
`http://<pi-address>:8080/` from a device on the same private network. The panel
provides brightness and sensitivity controls, pattern selection, and fixture
and input assignments. The **Color cycle** pattern lets you select colors and
set the seconds per color; assigned meter peaks still flash their fixture
white. Color-cycle settings are saved on the Pi in
`/home/patch/lights/control_settings.json`.

