"""Small starting point for sending DMX frames from a Raspberry Pi.

Install pyserial first (see README.md). Channel numbers in this file are
DMX slot numbers, starting at 1; slot 0 is the DMX start code.
"""

import time

import serial


PORT = "/dev/ttyUSB1"
BAUD = 250_000
FRAME_INTERVAL = 0.025
FIXTURE_ADDRESSES = (1, 8, 15, 22)
CHANNEL_COUNT = FIXTURE_ADDRESSES[-1] + 6


class DmxOutput:
    """Send a DMX frame long enough for the configured fixtures."""

    def __init__(self, port: str, baud: int = BAUD) -> None:
        self.port = port
        self.baud = baud
        self.serial = self._open_serial()
        self.channels = bytearray(CHANNEL_COUNT)
        self.frames_sent = 0

    def _open_serial(self) -> serial.Serial:
        return serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=1,
            write_timeout=0.2,
            exclusive=True,
        )

    def reopen(self, pause: float = 0.25, retry_interval: float = 1.0) -> None:
        """Reopen the serial device without sending a blackout frame.

        The channel buffer stays intact. If the adapter is briefly unavailable,
        keep retrying here rather than exiting the test and running shutdown
        blackout logic.
        """
        self.serial.close()
        time.sleep(pause)
        while True:
            try:
                self.serial = self._open_serial()
                return
            except serial.SerialException as error:
                print(
                    f"Could not reopen {self.port}: {error}; retrying in "
                    f"{retry_interval:.1f}s.",
                    flush=True,
                )
                time.sleep(retry_interval)

    def set_channels(self, values: dict[int, int]) -> None:
        """Set slots by one-based DMX channel number, clamping values to 0–255."""
        for channel, value in values.items():
            if not 1 <= channel <= len(self.channels):
                raise ValueError(f"channel must be between 1 and {len(self.channels)}")
            self.channels[channel - 1] = max(0, min(255, int(value)))

    def send(self) -> None:
        """Send start code plus configured slots, with a DMX break and MAB."""
        # Do not use send_break(duration=0.00012) on POSIX: pySerial converts
        # that duration to tcsendbreak(..., 0), which Linux holds for 0.25s+.
        self.serial.break_condition = True
        time.sleep(0.00012)
        self.serial.break_condition = False
        time.sleep(0.000015)
        frame = b"\x00" + self.channels
        bytes_written = self.serial.write(frame)
        if bytes_written != len(frame):
            raise IOError(
                f"short DMX write: sent {bytes_written} of {len(frame)} bytes"
            )

        self.frames_sent += 1
        if self.frames_sent % 100 == 0:
            try:
                queued_before_flush = self.serial.out_waiting
            except (AttributeError, NotImplementedError, OSError):
                queued_before_flush = "unavailable"
            self.serial.flush()
            try:
                queued_after_flush = self.serial.out_waiting
            except (AttributeError, NotImplementedError, OSError):
                queued_after_flush = "unavailable"
            print(
                f"DMX TX frames={self.frames_sent} bytes={bytes_written} "
                f"queued={queued_before_flush}->{queued_after_flush}",
                flush=True,
            )

    def close(self) -> None:
        self.serial.close()


def main() -> None:
    # ZQ01148 chart: CH1 dimmer, CH2 red, CH3 green, CH4 blue,
    # CH5 strobe, CH6 control mode, CH7 effect speed. CH6=0 selects
    # manual CH1-CH5 control; CH5=0 leaves strobe off.
    # Four 7-channel fixtures use start addresses 1, 8, 15, and 22.
    output = DmxOutput(PORT)
    for address in FIXTURE_ADDRESSES:
        output.set_channels({
            address: 255,
            address + 1: 0,
            address + 2: 0,
            address + 3: 255,
            address + 4: 0,
            address + 5: 0,
            address + 6: 0,
        })

    print(f"Sending steady blue to fixtures at {FIXTURE_ADDRESSES} on {PORT}.")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            output.send()
            time.sleep(FRAME_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        output.close()
        print("DMX output stopped.")


if __name__ == "__main__":
    main()
