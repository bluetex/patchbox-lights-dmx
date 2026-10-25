"""Fade four fixed-color fixtures through OLA without opening the serial port."""

import array
import math
import time

from ola.ClientWrapper import ClientWrapper


UNIVERSE = 0
FIXTURE_ADDRESSES = (1, 8, 15, 22)
CHANNEL_COUNT = FIXTURE_ADDRESSES[-1] + 6
TICK_MS = 40  # 25 updates per second; OLA owns the DMX refresh stream.
CYCLE_SECONDS = 2.5  # 0% -> 100% -> 0%.
REPORT_INTERVAL = 1.0

COLORS = (
    ("green", (0, 255, 0)),
    ("red", (255, 0, 0)),
    ("blue", (0, 0, 255)),
    ("orange", (255, 96, 0)),
)


class OlaFadeTest:
    def __init__(self) -> None:
        self.wrapper = ClientWrapper()
        self.client = self.wrapper.Client()
        self.started = time.monotonic()
        self.last_report = -REPORT_INTERVAL
        self.frames_sent = 0
        self.stopping = False

    @staticmethod
    def frame_at(level: float, cycle: int) -> array.array:
        """Build a 28-slot frame; strobe, mode, and effect speed stay at zero."""
        channels = array.array("B", [0] * CHANNEL_COUNT)
        dimmer = round(max(0.0, min(1.0, level)) * 255)
        for index, address in enumerate(FIXTURE_ADDRESSES):
            _name, (red, green, blue) = COLORS[(index + cycle) % len(COLORS)]
            start = address - 1
            channels[start] = dimmer
            channels[start + 1] = red
            channels[start + 2] = green
            channels[start + 3] = blue
            # address + 4: strobe off; +5: manual mode; +6: effect speed off.
        return channels

    def on_sent(self, status) -> None:
        if not status.Succeeded():
            print("OLA reported a DMX send failure; stopping.", flush=True)
            self.stopping = True
            self.wrapper.Stop()

    def send_frame(self) -> None:
        if self.stopping:
            return

        elapsed = time.monotonic() - self.started
        cycle = int(elapsed // CYCLE_SECONDS)
        phase = (elapsed % CYCLE_SECONDS) / CYCLE_SECONDS
        # A cosine curve reaches 0%, 100%, and 0% with no abrupt level steps.
        level = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
        self.client.SendDmx(UNIVERSE, self.frame_at(level, cycle), self.on_sent)
        self.frames_sent += 1

        if elapsed - self.last_report >= REPORT_INTERVAL:
            percent = round(level * 100)
            fixture_colors = [
                COLORS[(index + cycle) % len(COLORS)][0].upper()
                for index in range(len(FIXTURE_ADDRESSES))
            ]
            print(
                f"{elapsed:7.1f}s {percent:6d}% "
                f"{fixture_colors[0]:>12} {fixture_colors[1]:>10} "
                f"{fixture_colors[2]:>10} {fixture_colors[3]:>12}",
                flush=True,
            )
            self.last_report = elapsed

        self.wrapper.AddEvent(TICK_MS, self.send_frame)

    def send_blackout(self) -> None:
        self.stopping = True

        def blackout_sent(_status) -> None:
            self.wrapper.Stop()

        self.client.SendDmx(
            UNIVERSE,
            array.array("B", [0] * CHANNEL_COUNT),
            blackout_sent,
        )
        # Run the event loop long enough for OLA to deliver the final blackout.
        self.wrapper.Run()

    def run(self) -> None:
        print(
            "OLA fade test on universe 0; colors rotate each 2.5-second cycle. "
            "Press Ctrl+C to blackout and stop.",
            flush=True,
        )
        print(
            f"{'Elapsed':>8} {'Level':>7} {'F1':>12} "
            f"{'F2':>10} {'F3':>10} {'F4':>12}",
            flush=True,
        )
        try:
            self.wrapper.AddEvent(1, self.send_frame)
            self.wrapper.Run()
        except KeyboardInterrupt:
            print("\nInterrupted; sending blackout through OLA.", flush=True)
            self.send_blackout()
        finally:
            print(f"DMX updates sent: {self.frames_sent}", flush=True)


if __name__ == "__main__":
    OlaFadeTest().run()
