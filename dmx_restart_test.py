"""Repeatedly run and restart dmx_color_test.py to test DMX recovery."""

import signal
import subprocess
import sys
import time
from pathlib import Path


CHILD_SCRIPT = Path(__file__).with_name("dmx_color_test.py")
RUN_SECONDS = 12.0
RESTART_PAUSE = 0.5
STOP_TIMEOUT = 3.0


def stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("Child did not stop cleanly; terminating it.", flush=True)
        child.terminate()
        try:
            child.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main() -> None:
    run_number = 0
    child: subprocess.Popen[bytes] | None = None
    print("Restarting the color test every 12 seconds. Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            run_number += 1
            print(f"\n=== Starting color-test process {run_number} ===", flush=True)
            child = subprocess.Popen([sys.executable, str(CHILD_SCRIPT)])
            try:
                return_code = child.wait(timeout=RUN_SECONDS)
                print(f"=== Child exited early ({return_code}) ===", flush=True)
            except subprocess.TimeoutExpired:
                print(f"=== Stopping child after {RUN_SECONDS:.0f}s ===", flush=True)
                stop_child(child)
                return_code = child.returncode
            child = None
            print(f"=== Child closed; restarting shortly (exit {return_code}) ===", flush=True)
            time.sleep(RESTART_PAUSE)
    except KeyboardInterrupt:
        print("\nStopping restart loop.", flush=True)
    finally:
        if child is not None:
            stop_child(child)
        print("Restart wrapper stopped.", flush=True)


if __name__ == "__main__":
    main()
