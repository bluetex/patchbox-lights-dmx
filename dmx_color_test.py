"""Continuously send one stable DMX color set without interrupting the port."""

import random
import time

from dmx_basic import DmxOutput, FIXTURE_ADDRESSES, FRAME_INTERVAL, PORT


COLORS = (
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
    ("orange", (255, 96, 0)),
    ("cyan", (0, 255, 255)),
    ("magenta", (255, 0, 255)),
    ("yellow", (255, 255, 0)),
    ("purple", (128, 0, 255)),
    ("white", (255, 255, 255)),
)


def set_fixture(output: DmxOutput, address: int, rgb: tuple[int, int, int], on: bool) -> None:
    red, green, blue = rgb if on else (0, 0, 0)
    output.set_channels({
        address: 255 if on else 0,
        address + 1: red,
        address + 2: green,
        address + 3: blue,
        address + 4: 0,  # strobe off
        address + 5: 0,  # manual control mode
        address + 6: 0,  # effect speed off
    })


def send_until_stopped(output: DmxOutput) -> None:
    started = time.monotonic()
    next_report = started + 1.0
    while True:
        output.send()
        now = time.monotonic()
        if now >= next_report:
            print(
                f"TX frames={output.frames_sent} elapsed={now - started:.1f}s",
                flush=True,
            )
            next_report += 1.0
        time.sleep(FRAME_INTERVAL)


def blackout(output: DmxOutput) -> None:
    for address in FIXTURE_ADDRESSES:
        set_fixture(output, address, (0, 0, 0), on=False)
    for _ in range(3):
        output.send()
        time.sleep(FRAME_INTERVAL)


def apply_random_colors(output: DmxOutput) -> None:
    colors = random.sample(COLORS, len(FIXTURE_ADDRESSES))
    print(f"{'Fixture':<10}{'Color':<12}{'Level':>7}", flush=True)
    for address, (name, rgb) in zip(FIXTURE_ADDRESSES, colors):
        set_fixture(output, address, rgb, on=True)
        print(f"{address:<10}{name:<12}{'100%':>7}", flush=True)


def main() -> None:
    output = DmxOutput(PORT)
    try:
        print(
            f"Continuous DMX on {PORT}; serial port stays open; fixtures {FIXTURE_ADDRESSES}.",
            flush=True,
        )
        apply_random_colors(output)
        send_until_stopped(output)
    except KeyboardInterrupt:
        print("\nTest interrupted.", flush=True)
        print("Sending blackout.", flush=True)
        if getattr(output.serial, "is_open", False):
            blackout(output)
        else:
            print("Port is closed; skipping blackout frame.", flush=True)
    finally:
        output.close()
        print("DMX output closed.", flush=True)


if __name__ == "__main__":
    main()
