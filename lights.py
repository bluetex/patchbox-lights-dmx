"""Midas/Behringer OSC meter to OLA controller for configurable RGB fixtures."""

import array
import ipaddress
import json
import logging
import math
import os
import random
import socket
import struct
import threading
import time

from flask import Flask, jsonify, request
from ola.ClientWrapper import ClientWrapper


# ==================== CONFIGURATION ====================
DEFAULT_OSC_SOURCE_IP = "192.168.0.18"
SUPPORTED_OSC_MODELS = ("Behringer XR18", "Midas MR18")
XR18_PORT = 10024
OLA_UNIVERSE = 0
FIXTURE_CHANNELS = 7
DEFAULT_FIXTURE_COUNT = 4
MAX_FIXTURE_COUNT = 512 // FIXTURE_CHANNELS

MIN_VOICE_FLOOR_DB = -60.0
MAX_VOICE_CEILING_DB = -6.0
VOCAL_THRESHOLD_DB = -10.0
VOCAL_RELEASE_DB = -24.0
VOCAL_PEAK_HOLD_SECONDS = 1.0
VOCAL_PEAK_RETRIGGER_DELTA = 0.08
PEAK_DECAY_PER_SECOND = 2.0
WHITE_ATTACK_SECONDS = 0.2
WHITE_RELEASE_SECONDS = 3.0
METER_STALE_SECONDS = 1.0
METER_STREAM = "/meters/1"
METER_SUBSCRIPTION_INTERVAL = 4.0
ENGINE_UPDATE_INTERVAL = 0.025
DMX_REFRESH_INTERVAL = 0.040  # 25 OLA client updates per second.
BAND_SMOOTHING_ALPHA = 0.20
CONSOLE_STATUS_INTERVAL = 1.0

AMBIENT_COLORS = (
    (255, 64, 24),
    (255, 32, 160),
    (0, 220, 255),
    (0, 255, 64),
    (255, 160, 16),
    (96, 48, 255),
)
BAND_COLOR_TRIGGER = 0.25
BAND_COLOR_RELEASE = 0.10
BAND_CHASE_BASE_STEPS_PER_SECOND = 0.125
BAND_CHASE_LEVEL_STEPS_PER_SECOND = 0.75
BAND_PAIR_ACCENT_GAIN = 0.66
BAND_ROLL_BASE_GAIN = 0.28
BAND_STROBE_DMX = 24
BAND_STROBE_SECONDS = 0.56
BAND_STROBE_COOLDOWN = 2.8
BAND_SWELL_ATTACK_SECONDS = 0.12
BAND_SWELL_RELEASE_SECONDS = 0.50
BAND_PEAK_DIVE_SECONDS = 0.10
BAND_PEAK_SWELL_SECONDS = 0.45
BAND_PEAK_DIVE_GAIN = 0.24
BAND_PEAKS_PER_EFFECT_CHANGE = 32
BAND_STROBE_EVERY_PEAKS = 8
BASELINE_AMBIENT_COLOR = (160, 48, 255)
COLOR_CYCLE_PALETTE = {
    "Purple": (160, 48, 255),
    "Green": (0, 255, 60),
    "Teal": (0, 210, 190),
    "Magenta": (255, 0, 180),
    "Blue": (20, 80, 255),
    "Cyan": (0, 220, 255),
    "Amber": (255, 150, 0),
    "Red": (255, 24, 24),
}
DEFAULT_CYCLE_COLORS = ["Purple", "Green", "Teal", "Magenta"]
MIN_BAND_BRIGHTNESS = 0.50
AMBIENT_INTENSITY = 0.50
BAND_HOLD_BRIGHTNESS = 0.15
CONTROL_PANEL_HOST = "0.0.0.0"
CONTROL_PANEL_PORT = 8080
CONTROL_SETTINGS_PATH = "/home/patch/lights/control_settings.json"
INPUT_CHANNEL_COUNT = 16
AVAILABLE_PATTERNS = {"auto", "ambient", "chase_forward", "chase_reverse", "unison", "mixed", "color_cycle", "blackout"}
DEFAULT_CONTROLS = {
    "sensitivity": 1.0,
    "audio_enabled": True,
    "brightness": 1.0,
    "pattern": "auto",
    "cycle_colors": DEFAULT_CYCLE_COLORS,
    "cycle_speed": 1.5,
    "osc_model": "Behringer XR18",
    "osc_ip": DEFAULT_OSC_SOURCE_IP,
    "fixture_count": DEFAULT_FIXTURE_COUNT,
    "fixture_enabled": [True, True, True, True],
    "spotlight_inputs": [1, 2, 3, 4],
    "band_mode": "auto",
    "band_inputs": [],
}
# =======================================================


def fixture_addresses_for_count(count):
    return tuple(1 + FIXTURE_CHANNELS * index for index in range(count))


def parse_ipv4_address(value):
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError:
        return None
    # ``IPv4Address.is_broadcast`` is not available in every Python version
    # shipped with Raspberry Pi OS, so check the limited-broadcast address
    # directly for compatibility.
    if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
        return None
    return str(address)


def load_control_settings():
    controls = dict(DEFAULT_CONTROLS)
    controls["band_inputs"] = list(DEFAULT_CONTROLS["band_inputs"])
    controls["cycle_colors"] = list(DEFAULT_CYCLE_COLORS)
    controls["fixture_enabled"] = list(DEFAULT_CONTROLS["fixture_enabled"])
    controls["spotlight_inputs"] = list(DEFAULT_CONTROLS["spotlight_inputs"])
    try:
        with open(CONTROL_SETTINGS_PATH, "r", encoding="utf-8") as settings_file:
            saved = json.load(settings_file)
        if isinstance(saved, dict):
            saved_enabled = saved.get("fixture_enabled", [])
            saved_spotlights = saved.get("spotlight_inputs", [])
            fixture_count = saved.get("fixture_count")
            if fixture_count is None:
                legacy_lengths = [
                    len(value) for value in (saved_enabled, saved_spotlights)
                    if isinstance(value, list) and value
                ]
                fixture_count = max(legacy_lengths, default=DEFAULT_FIXTURE_COUNT)
            if type(fixture_count) is not int or not 1 <= fixture_count <= MAX_FIXTURE_COUNT:
                fixture_count = DEFAULT_FIXTURE_COUNT

            sensitivity = float(saved.get("sensitivity", controls["sensitivity"]))
            brightness = float(saved.get("brightness", controls["brightness"]))
            pattern = saved.get("pattern", controls["pattern"])
            cycle_colors = saved.get("cycle_colors", controls["cycle_colors"])
            cycle_speed = float(saved.get("cycle_speed", controls["cycle_speed"]))
            audio_enabled = saved.get("audio_enabled", controls["audio_enabled"])
            osc_model = saved.get("osc_model", controls["osc_model"])
            osc_ip = parse_ipv4_address(saved.get("osc_ip", controls["osc_ip"]))
            band_mode = saved.get("band_mode", controls["band_mode"])
            band_inputs = saved.get("band_inputs", controls["band_inputs"])
            controls["fixture_count"] = fixture_count
            controls["fixture_enabled"] = [True] * fixture_count
            controls["spotlight_inputs"] = [0] * fixture_count
            for index, value in enumerate(DEFAULT_CONTROLS["spotlight_inputs"][:fixture_count]):
                controls["spotlight_inputs"][index] = value
            if isinstance(saved_enabled, list):
                for index, value in enumerate(saved_enabled[:fixture_count]):
                    if type(value) is bool:
                        controls["fixture_enabled"][index] = value
            if isinstance(saved_spotlights, list):
                for index, value in enumerate(saved_spotlights[:fixture_count]):
                    if type(value) is int and 0 <= value <= INPUT_CHANNEL_COUNT:
                        controls["spotlight_inputs"][index] = value
            if 0.5 <= sensitivity <= 2.0:
                controls["sensitivity"] = sensitivity
            if type(audio_enabled) is bool:
                controls["audio_enabled"] = audio_enabled
            if 0.0 <= brightness <= 1.0:
                controls["brightness"] = brightness
            if pattern in AVAILABLE_PATTERNS:
                controls["pattern"] = pattern
            if (
                isinstance(cycle_colors, list)
                and cycle_colors
                and all(isinstance(color, str) and color in COLOR_CYCLE_PALETTE for color in cycle_colors)
                and len(set(cycle_colors)) == len(cycle_colors)
            ):
                controls["cycle_colors"] = cycle_colors
            if 0.25 <= cycle_speed <= 15.0:
                controls["cycle_speed"] = cycle_speed
            if osc_model in SUPPORTED_OSC_MODELS:
                controls["osc_model"] = osc_model
            if osc_ip:
                controls["osc_ip"] = osc_ip
            if band_mode in ("auto", "custom"):
                controls["band_mode"] = band_mode
            if isinstance(band_inputs, list) and all(type(value) is int and 1 <= value <= INPUT_CHANNEL_COUNT for value in band_inputs) and len(set(band_inputs)) == len(band_inputs):
                controls["band_inputs"] = band_inputs
    except (OSError, ValueError, TypeError):
        pass
    return controls


def save_control_settings(controls):
    temporary_path = CONTROL_SETTINGS_PATH + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as settings_file:
        json.dump(controls, settings_file, indent=2)
        settings_file.write("\n")
    os.replace(temporary_path, CONTROL_SETTINGS_PATH)


def effective_band_inputs(controls):
    if controls["band_mode"] == "custom":
        return list(controls["band_inputs"])
    spotlight_sources = {source for source in controls["spotlight_inputs"] if source}
    return [channel for channel in range(1, INPUT_CHANNEL_COUNT + 1) if channel not in spotlight_sources]


class OlaDmxOutput:
    """Submit the latest fixture values to olad; never open the serial port."""

    def __init__(self, initial_frame):
        self._frame_lock = threading.Lock()
        self._frame = bytes(initial_frame)
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self.frames_submitted = 0
        self._thread = threading.Thread(
            target=self._run,
            name="ola-dmx-output",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("Timed out connecting the OLA DMX output thread")
        if self._error:
            raise RuntimeError(f"Could not start OLA DMX output: {self._error}")

    def update(self, frame):
        """Replace the frame that the OLA sender will transmit next."""
        with self._frame_lock:
            self._frame = bytes(frame)

    def error(self):
        return self._error

    def _run(self):
        try:
            wrapper = ClientWrapper()
            client = wrapper.Client()
            self._ready.set()

            def sent(status):
                if not status.Succeeded():
                    self._error = "olad rejected a DMX update"
                    print(f"OLA output error: {self._error}", flush=True)
                    wrapper.Stop()

            def send_latest():
                if self._stop_event.is_set():
                    def blackout_sent(_status):
                        wrapper.Stop()

                    client.SendDmx(
                        OLA_UNIVERSE,
                        array.array("B", [0] * len(self._frame)),
                        blackout_sent,
                    )
                    return

                with self._frame_lock:
                    frame = self._frame
                client.SendDmx(OLA_UNIVERSE, array.array("B", frame), sent)
                self.frames_submitted += 1
                wrapper.AddEvent(
                    int(DMX_REFRESH_INTERVAL * 1000),
                    send_latest,
                )

            wrapper.AddEvent(1, send_latest)
            wrapper.Run()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            print(f"OLA output error: {self._error}", flush=True)

    def close(self):
        """Ask olad to send blackout, then stop the client event loop."""
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            print("OLA output thread did not stop within 2 seconds.", flush=True)


def parse_meter_values(data):
    """Decode an XR18 /meters/1 OSC packet containing one binary blob."""
    address_end = data.find(b"\x00")
    if address_end < 0 or data[:address_end] != METER_STREAM.encode("ascii"):
        return None

    tags_offset = (address_end + 4) & ~3
    if data[tags_offset:tags_offset + 4] != b",b\x00\x00":
        return None

    blob_length_offset = tags_offset + 4
    if len(data) < blob_length_offset + 4:
        return None
    blob_length = struct.unpack(">I", data[blob_length_offset:blob_length_offset + 4])[0]
    blob_start = blob_length_offset + 4
    blob_end = blob_start + blob_length
    if blob_length < 4 or blob_end > len(data):
        return None

    blob = data[blob_start:blob_end]
    channel_count = struct.unpack("<I", blob[:4])[0]
    values_end = 4 + channel_count * 2
    if channel_count < 16 or values_end > len(blob):
        return None
    return struct.unpack(f"<{channel_count}h", blob[4:values_end])


def meter_db(meter_value):
    return meter_value / 256.0


def clean_vu(meter_value):
    level_db = max(-80.0, min(0.0, meter_db(meter_value)))
    if level_db <= MIN_VOICE_FLOOR_DB:
        return 0.0
    if level_db >= MAX_VOICE_CEILING_DB:
        return 1.0
    return (level_db - MIN_VOICE_FLOOR_DB) / (MAX_VOICE_CEILING_DB - MIN_VOICE_FLOOR_DB)


class LightingController:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("0.0.0.0", 0))
        self.socket.settimeout(0.001)
        self.controls_lock = threading.RLock()
        self.controls = load_control_settings()
        self.fixture_addresses = fixture_addresses_for_count(self.controls["fixture_count"])
        self.dmx_buffer = bytearray(self.fixture_addresses[-1] + FIXTURE_CHANNELS)
        self.source_changed = threading.Event()

        self.voice_peak_levels = [0.0] * INPUT_CHANNEL_COUNT
        self.voice_peak_hold_until = [0.0] * INPUT_CHANNEL_COUNT
        self.voice_peak_active = [False] * INPUT_CHANNEL_COUNT
        self.voice_captured_peaks = [0.0] * INPUT_CHANNEL_COUNT
        self.white_levels = [0.0] * INPUT_CHANNEL_COUNT
        self.smoothed_voice = [0.0] * INPUT_CHANNEL_COUNT
        self.smoothed_band = 0.0
        self.band_peak_level = 0.0
        self.band_swell_level = 0.0
        self.band_peak_hold_until = 0.0
        self.band_dive_started = -1e9
        self.band_strobe_until = 0.0
        self.band_last_strobe = -1e9
        self.band_captured_peak = 0.0
        self.band_color_rgb = BASELINE_AMBIENT_COLOR
        self.color_cycle_started = time.monotonic()
        self.band_color_active = False
        self.band_color_unison = False
        self.band_chase_phase = 0.0
        self.band_chase_direction = 1
        self.band_peak_detector_direction = 0
        self.band_peak_candidate = 0.0
        self.band_peak_valley = 1.0
        self.band_effect_peak_count = 0
        self.last_meter_subscription = 0.0
        self.last_valid_meter_time = None
        self.last_engine_update = time.monotonic()
        self.udp_packets_received = 0
        self.valid_meter_packets_received = 0
        self.meter_subscription_requests = 0
        self.dmx_frames_submitted = 0
        self.last_error = None
        self.last_network_error = None
        self.next_dmx_refresh = 0.0

        for address in self.fixture_addresses:
            self.set_fixture_color(address, self.ambient_rgb(0.0))
        try:
            self.dmx_output = OlaDmxOutput(self.dmx_buffer[1:])
        except Exception:
            self.socket.close()
            raise

    def set_fixture_color(self, address, rgb, strobe=0):
        fixture_index = self.fixture_addresses.index(address)
        enabled = self.controls["fixture_enabled"][fixture_index]
        brightness = (
            0.0
            if not enabled or self.controls["pattern"] == "blackout"
            else self.controls["brightness"]
        )
        self.dmx_buffer[address] = int(255 * brightness)
        self.dmx_buffer[address + 1:address + 4] = bytes(
            max(0, min(255, int(value))) for value in rgb
        )
        self.dmx_buffer[address + 4] = max(0, min(255, int(strobe)))
        self.dmx_buffer[address + 5] = 0  # Manual channel control
        self.dmx_buffer[address + 6] = 0  # Effect speed unused

    def clear_audio_response(self):
        self.voice_peak_levels[:] = [0.0] * INPUT_CHANNEL_COUNT
        self.voice_peak_hold_until[:] = [0.0] * INPUT_CHANNEL_COUNT
        self.voice_peak_active[:] = [False] * INPUT_CHANNEL_COUNT
        self.voice_captured_peaks[:] = [0.0] * INPUT_CHANNEL_COUNT
        self.white_levels[:] = [0.0] * INPUT_CHANNEL_COUNT
        self.band_peak_level = 0.0
        self.band_swell_level = 0.0
        self.band_peak_hold_until = 0.0
        self.band_strobe_until = 0.0
        self.band_captured_peak = 0.0
        self.band_color_active = False
        self.band_color_rgb = BASELINE_AMBIENT_COLOR
        self.band_peak_detector_direction = 0
        self.band_peak_candidate = 0.0
        self.band_peak_valley = 1.0

    def ambient_rgb(self, vu_level, fixture_index=None, palette_offset=0):
        color = self.band_color_rgb
        # During a band lift, rotate the palette per fixture so every
        # non-white fixture gets a different color from the same palette.
        if fixture_index is not None and color in AMBIENT_COLORS:
            start = AMBIENT_COLORS.index(color)
            fixture_offset = 0 if self.band_color_unison else fixture_index
            color = AMBIENT_COLORS[(start + fixture_offset + palette_offset) % len(AMBIENT_COLORS)]
        brightness = max(0.0, min(1.0, vu_level))
        return tuple(
            int(channel * brightness * AMBIENT_INTENSITY)
            for channel in color
        )

    def update_outputs(self):
        with self.controls_lock:
            self._update_outputs_locked()

    def _update_outputs_locked(self):
        now = time.monotonic()
        controls = self.controls
        pattern = controls["pattern"]
        effect_active = controls["audio_enabled"] and (
            self.band_color_active or self.band_peak_level > 0.01
        )
        if pattern in ("chase_forward", "chase_reverse"):
            effect_active = True
        elif pattern in ("ambient", "color_cycle"):
            effect_active = False
        ambient_level = self.band_swell_level if effect_active else MIN_BAND_BRIGHTNESS
        if pattern in ("chase_forward", "chase_reverse"):
            ambient_level = max(MIN_BAND_BRIGHTNESS, ambient_level)
            if not controls["audio_enabled"]:
                ambient_level = 0.55
        if pattern in ("ambient", "color_cycle"):
            ambient_level = 0.55
        dive_age = now - self.band_dive_started
        if dive_age < BAND_PEAK_DIVE_SECONDS:
            dive_gain = BAND_PEAK_DIVE_GAIN
        elif dive_age < BAND_PEAK_DIVE_SECONDS + BAND_PEAK_SWELL_SECONDS:
            progress = (dive_age - BAND_PEAK_DIVE_SECONDS) / BAND_PEAK_SWELL_SECONDS
            progress = progress * progress * (3.0 - 2.0 * progress)
            dive_gain = BAND_PEAK_DIVE_GAIN + (1.0 - BAND_PEAK_DIVE_GAIN) * progress
        else:
            dive_gain = 1.0

        directed_phase = self.band_chase_phase * self.band_chase_direction
        fixture_count = len(self.fixture_addresses)
        roll_position = directed_phase % fixture_count
        bright_pair = int(directed_phase * 0.5) % 2
        palette_turn = int(self.band_chase_phase / fixture_count) % len(AMBIENT_COLORS)
        strobe_target = None
        if (
            effect_active
            and self.smoothed_band >= BAND_COLOR_TRIGGER
            and now < self.band_strobe_until
        ):
            strobe_target = int(roll_position + 0.5) % fixture_count

        for index, address in enumerate(self.fixture_addresses):
            spotlight_input = controls["spotlight_inputs"][index]
            peak = (
                self.white_levels[spotlight_input - 1]
                if controls["audio_enabled"] and spotlight_input
                else 0.0
            )
            if peak > 0.0:
                white = int(peak * 255)
                self.set_fixture_color(address, (white, white, white))
                continue

            rgb = self.ambient_rgb(
                ambient_level,
                index,
                palette_offset=palette_turn if effect_active else 0,
            )
            if pattern == "color_cycle":
                colors = controls["cycle_colors"]
                color_index = int((now - self.color_cycle_started) / controls["cycle_speed"]) % len(colors)
                color = COLOR_CYCLE_PALETTE[colors[color_index]]
                rgb = tuple(int(channel * ambient_level * AMBIENT_INTENSITY) for channel in color)
            if pattern in ("unison", "mixed"):
                color = self.band_color_rgb
                if color in AMBIENT_COLORS:
                    offset = 0 if pattern == "unison" else index
                    color = AMBIENT_COLORS[(AMBIENT_COLORS.index(color) + offset) % len(AMBIENT_COLORS)]
                rgb = tuple(int(channel * ambient_level * AMBIENT_INTENSITY) for channel in color)
            if pattern in ("chase_forward", "chase_reverse"):
                direction = 1 if pattern == "chase_forward" else -1
                position = (self.band_chase_phase * direction) % fixture_count
                distance = abs(index - position)
                distance = min(distance, fixture_count - distance)
                gain = max(0.12, 1.0 - distance / 1.5)
                rgb = tuple(int(channel * gain) for channel in rgb)
            elif effect_active:
                # Layer a smooth traveling swell with an alternating-pair pulse.
                distance = abs(index - roll_position)
                distance = min(distance, fixture_count - distance)
                crest = max(0.0, 1.0 - distance / max(1.0, fixture_count / 2.0))
                roll_gain = BAND_ROLL_BASE_GAIN + (1.0 - BAND_ROLL_BASE_GAIN) * crest
                pair_gain = 1.0 if index % 2 == bright_pair else BAND_PAIR_ACCENT_GAIN
                gain = roll_gain * pair_gain * dive_gain
                rgb = tuple(int(channel * gain) for channel in rgb)
            strobe = BAND_STROBE_DMX if pattern == "auto" and index == strobe_target else 0
            self.set_fixture_color(address, rgb, strobe=strobe)

    def process_meter(self, values, now):
        voice_values = values[0:INPUT_CHANNEL_COUNT]
        sensitivity_db = 12.0 * math.log2(self.controls["sensitivity"])
        if not self.controls["audio_enabled"]:
            for index, meter_value in enumerate(voice_values):
                level = clean_vu(int(meter_value + sensitivity_db * 256.0))
                current = self.smoothed_voice[index]
                self.smoothed_voice[index] = level if level > current else max(0.0, current - 0.08)
            band_channels = effective_band_inputs(self.controls)
            if band_channels:
                band_meter = max(values[channel - 1] for channel in band_channels)
                band_target = clean_vu(int(band_meter + sensitivity_db * 256.0))
            else:
                band_target = 0.0
            self.smoothed_band += BAND_SMOOTHING_ALPHA * (band_target - self.smoothed_band)
            self.band_color_rgb = BASELINE_AMBIENT_COLOR
            return
        for index, meter_value in enumerate(voice_values):
            adjusted_meter = int(meter_value + sensitivity_db * 256.0)
            level = clean_vu(adjusted_meter)
            level_db = meter_db(adjusted_meter)
            # Re-arm after the previous white envelope has fully tapered so
            # sustained vocals can produce another held peak.
            if self.voice_peak_levels[index] <= 0.01:
                self.voice_peak_active[index] = False
                self.voice_captured_peaks[index] = 0.0
            if level_db >= VOCAL_THRESHOLD_DB:
                if not self.voice_peak_active[index]:
                    self.voice_peak_active[index] = True
                    self.voice_captured_peaks[index] = level
                    self.voice_peak_levels[index] = max(self.voice_peak_levels[index], level)
                    self.voice_peak_hold_until[index] = now + VOCAL_PEAK_HOLD_SECONDS
                elif level >= self.voice_captured_peaks[index] + VOCAL_PEAK_RETRIGGER_DELTA:
                    self.voice_captured_peaks[index] = level
                    self.voice_peak_levels[index] = max(self.voice_peak_levels[index], level)
                    self.voice_peak_hold_until[index] = now + VOCAL_PEAK_HOLD_SECONDS
            elif level_db <= VOCAL_RELEASE_DB:
                self.voice_peak_active[index] = False
                self.voice_captured_peaks[index] = 0.0

            current = self.smoothed_voice[index]
            self.smoothed_voice[index] = level if level > current else max(0.0, current - 0.08)

        band_channels = effective_band_inputs(self.controls)
        if band_channels:
            band_meter = max(values[channel - 1] for channel in band_channels)
            band_target = clean_vu(int(band_meter + sensitivity_db * 256.0))
        else:
            band_target = 0.0
        self.smoothed_band += BAND_SMOOTHING_ALPHA * (band_target - self.smoothed_band)

        if self.smoothed_band >= BAND_COLOR_TRIGGER and not self.band_color_active:
            colors = tuple(color for color in AMBIENT_COLORS if color != self.band_color_rgb)
            self.band_color_rgb = random.choice(colors)
            self.band_color_active = True
            self.band_chase_phase = 0.0
            self.band_dive_started = now
            self.band_peak_detector_direction = 0
            self.band_peak_candidate = self.smoothed_band
            self.band_peak_valley = self.smoothed_band
            self.band_peak_level = max(self.band_peak_level, self.smoothed_band)
            self.band_peak_hold_until = now + VOCAL_PEAK_HOLD_SECONDS

        if self.band_color_active and self.smoothed_band >= BAND_COLOR_TRIGGER:
            level = self.smoothed_band
            if self.band_peak_detector_direction == 0:
                self.band_peak_detector_direction = 1
                self.band_peak_candidate = level
            elif self.band_peak_detector_direction > 0:
                if level >= self.band_peak_candidate:
                    self.band_peak_candidate = level
                elif self.band_peak_candidate - level >= VOCAL_PEAK_RETRIGGER_DELTA:
                    # A peak is counted after a clear fall from its local high.
                    peak = self.band_peak_candidate
                    self.band_peak_detector_direction = -1
                    self.band_peak_valley = level
                    self.band_captured_peak = peak
                    self.band_peak_level = max(self.band_peak_level, peak)
                    self.band_peak_hold_until = now + VOCAL_PEAK_HOLD_SECONDS
                    self.band_effect_peak_count += 1

                    if (
                        self.band_effect_peak_count % BAND_STROBE_EVERY_PEAKS == 0
                        and now - self.band_last_strobe >= BAND_STROBE_COOLDOWN
                    ):
                        self.band_strobe_until = now + BAND_STROBE_SECONDS
                        self.band_last_strobe = now

                    if self.band_effect_peak_count >= BAND_PEAKS_PER_EFFECT_CHANGE:
                        self.band_effect_peak_count = 0
                        self.band_color_unison = not self.band_color_unison
                        self.band_chase_direction *= -1
                        self.band_chase_phase = 0.0
                        self.band_dive_started = now
                        colors = tuple(color for color in AMBIENT_COLORS if color != self.band_color_rgb)
                        self.band_color_rgb = random.choice(colors)
            else:
                if level <= self.band_peak_valley:
                    self.band_peak_valley = level
                elif level - self.band_peak_valley >= VOCAL_PEAK_RETRIGGER_DELTA:
                    self.band_peak_detector_direction = 1
                    self.band_peak_candidate = level

        if self.band_color_active and self.smoothed_band <= BAND_COLOR_RELEASE:
            self.band_color_active = False
            self.band_captured_peak = 0.0
            self.band_peak_detector_direction = 0
            self.band_peak_candidate = 0.0
            self.band_peak_valley = 1.0

    def update(self, now):
        with self.controls_lock:
            self._update_locked(now)

    def _update_locked(self, now):
        elapsed = max(0.0, now - self.last_engine_update)
        self.last_engine_update = now
        pattern = self.controls["pattern"]
        if self.band_color_active or self.band_peak_level > 0.01 or pattern in ("chase_forward", "chase_reverse"):
            chase_rate = (
                BAND_CHASE_BASE_STEPS_PER_SECOND
                + self.smoothed_band * BAND_CHASE_LEVEL_STEPS_PER_SECOND
            )
            if pattern in ("chase_forward", "chase_reverse"):
                chase_rate = max(0.25, chase_rate)
            self.band_chase_phase += elapsed * chase_rate

        for index in range(INPUT_CHANNEL_COUNT):
            if now >= self.voice_peak_hold_until[index]:
                self.voice_peak_levels[index] = max(
                    0.0, self.voice_peak_levels[index] - PEAK_DECAY_PER_SECOND * elapsed
                )
            target = self.voice_peak_levels[index]
            if target > self.white_levels[index]:
                self.white_levels[index] = min(
                    target,
                    self.white_levels[index] + elapsed / WHITE_ATTACK_SECONDS,
                )
            else:
                self.white_levels[index] = max(
                    target,
                    self.white_levels[index] - elapsed / WHITE_RELEASE_SECONDS,
                )
        if now >= self.band_peak_hold_until:
            self.band_peak_level = max(0.0, self.band_peak_level - PEAK_DECAY_PER_SECOND * elapsed)
        swell_target = (
            max(self.smoothed_band, self.band_peak_level * BAND_HOLD_BRIGHTNESS)
            if self.controls["audio_enabled"]
            else 0.0
        )
        swell_tau = (
            BAND_SWELL_ATTACK_SECONDS
            if swell_target > self.band_swell_level
            else BAND_SWELL_RELEASE_SECONDS
        )
        swell_alpha = 1.0 - math.exp(-elapsed / swell_tau) if elapsed > 0.0 else 0.0
        self.band_swell_level += (swell_target - self.band_swell_level) * swell_alpha

        if self.last_valid_meter_time is not None and now - self.last_valid_meter_time > METER_STALE_SECONDS:
            self.smoothed_band = max(0.0, self.smoothed_band - PEAK_DECAY_PER_SECOND * elapsed)
            self.voice_peak_active[:] = [False] * INPUT_CHANNEL_COUNT
            self.voice_captured_peaks[:] = [0.0] * INPUT_CHANNEL_COUNT
            self.band_color_active = False
            self.band_captured_peak = 0.0

        # Repeat the held band lift while a live band signal remains active.
        if (
            self.band_color_active
            and self.smoothed_band >= BAND_COLOR_TRIGGER
            and self.band_peak_level <= 0.01
            and self.last_valid_meter_time is not None
            and now - self.last_valid_meter_time <= METER_STALE_SECONDS
        ):
            self.band_peak_level = self.smoothed_band
            self.band_captured_peak = self.smoothed_band
            self.band_peak_hold_until = now + VOCAL_PEAK_HOLD_SECONDS

        received_meter = None
        while True:
            try:
                data, sender = self.socket.recvfrom(4096)
            except socket.timeout:
                break
            except OSError as exc:
                self.last_network_error = f"{type(exc).__name__}: {exc}"
                break
            self.udp_packets_received += 1
            if sender[0] != self.controls["osc_ip"]:
                continue
            packet_values = parse_meter_values(data)
            if packet_values is not None:
                received_meter = packet_values
                self.valid_meter_packets_received += 1

        if received_meter is not None:
            self.last_valid_meter_time = now
            self.process_meter(received_meter, now)

        if not self.band_color_active and self.band_peak_level <= 0.0:
            self.band_color_rgb = BASELINE_AMBIENT_COLOR

        self.update_outputs()

        if now >= self.next_dmx_refresh:
            self.dmx_output.update(self.dmx_buffer[1:])
            self.dmx_frames_submitted = self.dmx_output.frames_submitted
            output_error = self.dmx_output.error()
            if output_error:
                self.last_error = f"OLA output error: {output_error}"
            else:
                self.last_error = None
            self.next_dmx_refresh = now + DMX_REFRESH_INTERVAL

    def close(self):
        for address in self.fixture_addresses:
            self.set_fixture_color(address, (0, 0, 0))
        try:
            self.dmx_output.update(self.dmx_buffer[1:])
            self.dmx_output.close()
        finally:
            self.socket.close()

    def print_status(self):
        now = time.monotonic()
        if self.last_valid_meter_time is None:
            meter_age = "none"
        else:
            meter_age = f"{now - self.last_valid_meter_time:.1f}s"

        vocal_text = ",".join(f"{level * 100:.0f}%" for level in self.smoothed_voice[:4])
        color_mode = (
            "UNISON" if self.band_color_unison else "MIXED"
        ) if self.band_color_active or self.band_peak_level > 0.01 else "IDLE"
        fixture_values = []
        for address in self.fixture_addresses:
            channels = self.dmx_buffer[address:address + 7]
            fixture_values.append(f"F@{address}=[{','.join(str(value) for value in channels)}]")

        loop_age = now - self.last_engine_update
        error_text = self.last_error or self.last_network_error or "none"
        print(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | "
            f"meters age={meter_age} UDP={self.udp_packets_received}/{self.valid_meter_packets_received} "
            f"req={self.meter_subscription_requests} | "
            f"vocal%=[{vocal_text}] band={self.smoothed_band * 100:.0f}% "
            f"band_peak={self.band_peak_level * 100:.0f}% color={color_mode} "
            f"peaks={self.band_effect_peak_count}/{BAND_PEAKS_PER_EFFECT_CHANGE} | "
            f"{' '.join(fixture_values)} | OLA updates={self.dmx_frames_submitted} "
            f"loop_age={loop_age:.2f}s error={error_text}",
            flush=True,
        )


def console_monitor(controller, stop_event):
    while not stop_event.wait(CONSOLE_STATUS_INTERVAL):
        try:
            controller.print_status()
        except Exception as exc:
            print(f"Console monitor error: {type(exc).__name__}: {exc}", flush=True)


def meter_renewer(controller, stop_event):
    """Renew the selected Midas/Behringer meter subscription."""
    while not stop_event.is_set():
        try:
            source_ip = controller.controls["osc_ip"]
            controller.socket.sendto(meters_packet, (source_ip, XR18_PORT))
            controller.last_meter_subscription = time.monotonic()
            controller.meter_subscription_requests += 1
            controller.last_network_error = None
        except OSError as exc:
            controller.last_network_error = f"{type(exc).__name__}: {exc}"
        if controller.source_changed.wait(METER_SUBSCRIPTION_INTERVAL):
            controller.source_changed.clear()


PANEL_HTML = r'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#10131b"><title>Patchbox Lights</title>
<style>
:root{color-scheme:dark;font:17px system-ui,-apple-system,sans-serif;background:#10131b;color:#f3f5fa}*{box-sizing:border-box}body{margin:0 auto;max-width:760px;padding:22px 18px 40px}h1{font-size:27px;margin:4px 0 5px}p.sub{margin:0 0 22px;color:#aeb6c7}.card{background:#1b202c;border:1px solid #30384a;border-radius:18px;padding:18px;margin:14px 0}.label{display:flex;justify-content:space-between;gap:14px;font-weight:650;margin:0 0 12px}.value{color:#91caff;font-variant-numeric:tabular-nums}input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:64px;margin:0;background:transparent;touch-action:pan-y;cursor:pointer;accent-color:#63aaff}input[type=range]::-webkit-slider-runnable-track{height:12px;background:#3b465c;border-radius:12px}input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:34px;height:34px;margin-top:-11px;border:3px solid #f3f5fa;border-radius:50%;background:#63aaff;box-shadow:0 2px 8px #0008}input[type=range]::-moz-range-track{height:12px;background:#3b465c;border-radius:12px}input[type=range]::-moz-range-thumb{width:28px;height:28px;border:3px solid #f3f5fa;border-radius:50%;background:#63aaff;box-shadow:0 2px 8px #0008}input[type=range]:focus-visible{outline:2px solid #91caff;outline-offset:2px}input[type=range]:disabled{opacity:.4;cursor:not-allowed}select,button{font:inherit;color:inherit;background:#252d3d;border:1px solid #46516a;border-radius:12px;padding:13px 14px}select{width:100%;min-height:52px}button{min-height:54px;font-weight:700;cursor:pointer}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.blackout{background:#8f2835;border-color:#bb5360}.meter{display:grid;grid-template-columns:minmax(115px,150px) 1fr 48px;align-items:center;gap:10px;margin:12px 0}.meter-name{font-size:14px;line-height:1.15}.meter-name strong,.meter-name small{display:block}.meter-name small{margin-top:3px;color:#aeb6c7;font-size:12px}.track{height:13px;background:#303749;border-radius:10px;overflow:hidden}.fill{height:100%;width:0;background:linear-gradient(90deg,#55c6bd,#ffd267,#ff6581);transition:width .15s}.small{font-size:14px;color:#aeb6c7;line-height:1.5}.status{display:flex;justify-content:space-between;gap:12px;align-items:center}.audio-switch{display:flex;align-items:center;gap:12px;min-height:44px;font-weight:650}.audio-switch input{width:25px;height:25px;accent-color:#63aaff}.dot{width:9px;height:9px;display:inline-block;border-radius:50%;background:#f0ba55;margin-right:8px}.dot.ok{background:#49d49a}.page-header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.settings-link{display:inline-flex;align-items:center;min-height:44px;margin:0;color:#91caff;font-weight:650;text-decoration:none;white-space:nowrap}.cycle-settings{margin-top:14px}.cycle-settings[hidden]{display:none}.cycle-intro{margin:0 0 10px}.color-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.color-choice{display:flex;align-items:center;gap:7px;min-height:48px;padding:7px 9px;background:#252d3d;border:1px solid #46516a;border-radius:10px;font-size:14px}.color-choice input{width:20px;height:20px;accent-color:#63aaff}.color-dot{width:15px;height:15px;border-radius:50%;box-shadow:0 0 0 1px #fff5}.cycle-speed{margin-top:14px}.toast{min-height:21px;color:#9bc9ff;font-size:14px;margin-top:10px}@media(max-width:500px){body{padding:16px 12px}.card{padding:15px}.meter{grid-template-columns:minmax(90px,110px) 1fr 42px;gap:7px}.color-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body>
<header class="page-header"><h1>Patchbox Lights</h1><a class="settings-link" href="/settings">Settings →</a></header>
<p class="sub">Live controls · same private network</p>
<section class="card"><div class="label"><span>Pattern</span><span class="value" id="patternName">Auto</span></div>
<select id="pattern" aria-label="Lighting pattern">
<option value="auto">Auto · meter driven</option><option value="ambient">Ambient wash</option><option value="color_cycle">Color cycle</option><option value="chase_forward">Chase · forward</option><option value="chase_reverse">Chase · reverse</option><option value="unison">Unison colors</option><option value="mixed">Mixed colors</option><option value="blackout">Blackout</option></select>
<div class="cycle-settings" id="cycleSettings" hidden><p class="small cycle-intro">Select colors to include; they cycle in the order shown. Meter peaks still flash their assigned fixture white.</p><div class="color-grid" id="cycleColors"><label class="color-choice"><input type="checkbox" value="Purple"><i class="color-dot" style="background:#a030ff"></i>Purple</label><label class="color-choice"><input type="checkbox" value="Green"><i class="color-dot" style="background:#00ff3c"></i>Green</label><label class="color-choice"><input type="checkbox" value="Teal"><i class="color-dot" style="background:#00d2be"></i>Teal</label><label class="color-choice"><input type="checkbox" value="Magenta"><i class="color-dot" style="background:#ff00b4"></i>Magenta</label><label class="color-choice"><input type="checkbox" value="Blue"><i class="color-dot" style="background:#1450ff"></i>Blue</label><label class="color-choice"><input type="checkbox" value="Cyan"><i class="color-dot" style="background:#00dcff"></i>Cyan</label><label class="color-choice"><input type="checkbox" value="Amber"><i class="color-dot" style="background:#ff9600"></i>Amber</label><label class="color-choice"><input type="checkbox" value="Red"><i class="color-dot" style="background:#ff1818"></i>Red</label></div><div class="cycle-speed"><div class="label"><span>Seconds per color</span><span class="value" id="cycleSpeedValue">1.50 s</span></div><input id="cycleSpeed" type="range" min="0.25" max="15" step="0.25" value="1.5" aria-label="Seconds per color"></div></div>
<div class="toast" id="toast"></div></section>
<section class="card"><div class="label"><span>Brightness</span><span class="value" id="brightnessValue">100%</span></div><input id="brightness" type="range" min="0" max="100" value="100" aria-label="Brightness"></section>
<section class="card"><div class="status"><strong>Audio sensitivity</strong><span class="small" id="audioState">On</span></div><label class="audio-switch"><input id="audioEnabled" type="checkbox" checked><span>Audio response</span></label><div class="label"><span>Sensitivity</span><span class="value" id="sensitivityValue">1.00×</span></div><input id="sensitivity" type="range" min="50" max="200" value="100" aria-label="Audio sensitivity"><div class="small">Raise it to react to quieter signals; turn Audio response off to ignore meter changes.</div></section>
<section class="card"><div class="status"><strong>Meter input</strong><span class="small" id="meterState"><i class="dot"></i>Connecting</span></div><div id="fixtureMeters"></div><div class="meter"><span>Band</span><div class="track"><div class="fill" id="band"></div></div><span id="bandt">0%</span></div><div class="small" id="detail">Waiting for status…</div></section>
<div class="row"><button id="auto">Return to Auto</button><button class="blackout" id="blackout">Blackout</button></div>
<script>
const $=id=>document.getElementById(id), names={auto:'Auto',ambient:'Ambient wash',color_cycle:'Color cycle',chase_forward:'Chase · forward',chase_reverse:'Chase · reverse',unison:'Unison colors',mixed:'Mixed colors',blackout:'Blackout'};let timer;
async function control(values){try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});const j=await r.json();if(!r.ok)throw Error(j.error||'Request failed');$('toast').textContent='Control updated';setTimeout(()=>{$('toast').textContent=''},1300)}catch(e){$('toast').textContent='Could not update: '+e.message}}
function delayed(value){clearTimeout(timer);timer=setTimeout(()=>control(value),110)}
$('brightness').addEventListener('input',e=>{$('brightnessValue').textContent=e.target.value+'%';delayed({brightness:Number(e.target.value)/100})});
$('sensitivity').addEventListener('input',e=>{$('sensitivityValue').textContent=(Number(e.target.value)/100).toFixed(2)+'×';delayed({sensitivity:Number(e.target.value)/100})});
$('audioEnabled').addEventListener('change',e=>{const enabled=e.target.checked;$('audioState').textContent=enabled?'On':'Off';$('sensitivity').disabled=!enabled;control({audio_enabled:enabled})});
$('pattern').addEventListener('change',e=>{$('cycleSettings').hidden=e.target.value!=='color_cycle';control({pattern:e.target.value})});$('auto').onclick=()=>{$('pattern').value='auto';$('cycleSettings').hidden=true;control({pattern:'auto'})};$('blackout').onclick=()=>{$('pattern').value='blackout';$('cycleSettings').hidden=true;control({pattern:'blackout'})};
document.querySelectorAll('#cycleColors input').forEach(input=>input.onchange=()=>{const colors=Array.from(document.querySelectorAll('#cycleColors input:checked')).map(item=>item.value);if(!colors.length){input.checked=true;$('toast').textContent='Select at least one color';return}control({cycle_colors:colors})});
function setCycleSpeed(value,save){$('cycleSpeed').value=value;$('cycleSpeedValue').textContent=Number(value).toFixed(2)+' s';if(save)delayed({cycle_speed:Number(value)})}
$('cycleSpeed').addEventListener('input',e=>setCycleSpeed(e.target.value,true));
function renderFixtureMeters(addresses){const root=$('fixtureMeters'),count=addresses.length;if(root.dataset.count===String(count))return;root.dataset.count=count;root.innerHTML='';addresses.forEach((address,index)=>{const row=document.createElement('div');row.className='meter';row.innerHTML=`<span class="meter-name"><strong>Fixture ${index+1}</strong><small id="source${index}"></small></span><div class="track"><div class="fill" id="v${index}"></div></div><span id="v${index}t">0%</span>`;root.appendChild(row)})}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'}),s=await r.json();const addresses=s.fixture_addresses||[];renderFixtureMeters(addresses);$('pattern').value=s.controls.pattern;$('patternName').textContent=names[s.controls.pattern]||s.controls.pattern;$('cycleSettings').hidden=s.controls.pattern!=='color_cycle';$('audioEnabled').checked=s.controls.audio_enabled;$('audioState').textContent=s.controls.audio_enabled?'On':'Off';$('sensitivity').disabled=!s.controls.audio_enabled;$('cycleSpeed').value=s.controls.cycle_speed;$('cycleSpeedValue').textContent=Number(s.controls.cycle_speed).toFixed(2)+' s';document.querySelectorAll('#cycleColors input').forEach(input=>input.checked=s.controls.cycle_colors.includes(input.value));$('brightness').value=Math.round(s.controls.brightness*100);$('brightnessValue').textContent=Math.round(s.controls.brightness*100)+'%';$('sensitivity').value=Math.round(s.controls.sensitivity*100);$('sensitivityValue').textContent=s.controls.sensitivity.toFixed(2)+'×';for(let i=0;i<addresses.length;i++){const input=s.controls.spotlight_inputs[i],level=input?Math.round(s.vocal[input-1]*100):0;$('source'+i).textContent=`DMX ${addresses[i]} · ${input?'Input '+input:'No spotlight'}`;$('v'+i).style.width=level+'%';$('v'+i+'t').textContent=level+'%'}const band=Math.round(s.band*100);$('band').style.width=band+'%';$('bandt').textContent=band+'%';const live=s.meter_age!==null&&s.meter_age<1.5;$('meterState').innerHTML='<i class="dot '+(live?'ok':'')+'"></i>'+(live?'Live':'No meter data');$('detail').textContent=`${addresses.length} fixtures · DMX updates ${s.dmx_frames} · last error: ${s.error||'none'}`}catch(e){$('meterState').innerHTML='<i class="dot"></i>Panel disconnected'}}
refresh();setInterval(refresh,1000);
</script></body></html>'''

SETTINGS_HTML = r'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#10131b"><title>Settings · Patchbox Lights</title>
<style>
:root{color-scheme:dark;font:17px system-ui,-apple-system,sans-serif;background:#10131b;color:#f3f5fa}*{box-sizing:border-box}body{margin:0 auto;max-width:760px;padding:22px 18px 40px}a{color:#91caff;text-decoration:none;font-weight:650;display:inline-flex;align-items:center;min-height:44px}h1{font-size:27px;margin:8px 0 5px}.sub,.hint{color:#aeb6c7;line-height:1.5}.card{background:#1b202c;border:1px solid #30384a;border-radius:18px;padding:16px;margin:14px 0}.fixture{display:grid;grid-template-columns:1fr 1.1fr;gap:14px;align-items:center;padding:14px 0;border-bottom:1px solid #30384a}.fixture:last-child{border-bottom:0}.fixture-title{font-weight:700}.fixture-address{display:block;color:#aeb6c7;font-size:14px;margin-top:3px}.enable{display:flex;align-items:center;gap:10px;min-height:52px}.enable input{width:28px;height:28px;accent-color:#63aaff}.source-label{font-size:13px;color:#aeb6c7;margin-bottom:5px}select,button,input[type=text]{font:inherit;color:inherit;background:#252d3d;border:1px solid #46516a;border-radius:12px;padding:12px 13px}select,input[type=text]{width:100%;min-height:52px}button{min-height:52px;font-weight:700;cursor:pointer}.hint{font-size:14px}.band-mode{display:flex;align-items:center;gap:12px;min-height:58px;font-weight:650}.band-mode input{width:28px;height:28px;accent-color:#63aaff}.channel-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.channel{display:flex;align-items:center;gap:7px;min-height:48px;padding:7px 9px;background:#252d3d;border:1px solid #46516a;border-radius:10px;font-size:14px}.channel input{width:21px;height:21px;accent-color:#63aaff}.channel input:disabled{opacity:.75}.setting-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:end}.toast{min-height:24px;color:#9bc9ff;font-size:14px;margin:8px 0}@media(max-width:500px){body{padding:16px 12px}.fixture{grid-template-columns:1fr;gap:5px}.card{padding:12px}.channel-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.setting-row{grid-template-columns:1fr}}
</style></head><body>
<a href="/">← Live controls</a><h1>Settings</h1><p class="sub">Configure the console source and the DMX fixtures.</p>
<section class="card"><h2>OSC audio source</h2><p class="hint">Only Behringer XR18 and Midas MR18 meter sources are supported.</p><div class="setting-row"><label><div class="source-label">Console model</div><select id="oscModel"><option>Behringer XR18</option><option>Midas MR18</option></select></label><label><div class="source-label">Console IP address</div><input id="oscIp" type="text" inputmode="decimal" autocomplete="off" placeholder="192.168.0.18"></label></div><p class="hint">The controller subscribes to the selected console’s OSC meter stream on UDP port 10024.</p><button id="saveSource">Save audio source</button></section>
<section class="card"><h2>Fixtures</h2><label><div class="source-label">Number of fixtures</div><select id="fixtureCount"></select></label><p class="hint">Fixtures use the current seven-channel RGB layout and get sequential DMX start addresses: 1, 8, 15, 22, and so on. Up to 73 fit in one DMX universe.</p><div id="fixtures"></div></section>
<section class="card"><h2>Band assignment</h2><label class="band-mode"><input id="bandAuto" type="checkbox"><span>Automatically use inputs not assigned as spotlights</span></label><div id="bandChannels" class="channel-grid"></div><p class="hint">When automatic is on, spotlight inputs are excluded from the band group. Turn it off to choose the band inputs yourself.</p></section>
<p class="hint">Turning a fixture off blacks it out. Set Spotlight source to Off to keep its ambient color but remove its white meter response. Settings are saved on the Pi and survive app restarts.</p><div class="toast" id="toast">Loading settings…</div>
<script>
const MAX_FIXTURES=73,$=id=>document.getElementById(id);let enabled=[],sources=[],bandMode='auto',bandInputs=[];
for(let count=1;count<=MAX_FIXTURES;count++)$('fixtureCount').add(new Option(String(count),String(count)));
function automaticBandInputs(){const spotlight=new Set(sources.filter(value=>value>0));return Array.from({length:16},(_,i)=>i+1).filter(input=>!spotlight.has(input))}
async function post(values,message){try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}),j=await r.json();if(!r.ok)throw Error(j.error||'Save failed');$('toast').textContent=message}catch(e){$('toast').textContent='Could not save settings: '+e.message}}
function saveFixtures(){return post({fixture_count:enabled.length,fixture_enabled:enabled,spotlight_inputs:sources,band_mode:bandMode,band_inputs:bandInputs},'Fixture settings saved on the Pi')}
function renderFixtures(){const root=$('fixtures');root.innerHTML='';const sourceOptions='<option value="0">Off</option>'+Array.from({length:16},(_,n)=>`<option value="${n+1}">Input ${n+1}</option>`).join('');for(let i=0;i<enabled.length;i++){const address=1+i*7,row=document.createElement('div');row.className='fixture';row.innerHTML=`<label class="enable"><input type="checkbox" data-enable="${i}" ${enabled[i]?'checked':''}><span><span class="fixture-title">Fixture ${i+1}</span><span class="fixture-address">DMX start ${address}</span></span></label><label><div class="source-label">Spotlight source</div><select data-source="${i}">${sourceOptions}</select></label>`;root.appendChild(row);row.querySelector('[data-source]').value=sources[i]}root.querySelectorAll('[data-enable]').forEach(el=>el.onchange=()=>{enabled[Number(el.dataset.enable)]=el.checked;saveFixtures()});root.querySelectorAll('[data-source]').forEach(el=>el.onchange=()=>{sources[Number(el.dataset.source)]=Number(el.value);if(bandMode==='auto')renderBandInputs();saveFixtures()})}
function renderBandInputs(){const automatic=bandMode==='auto',selected=automatic?automaticBandInputs():bandInputs,root=$('bandChannels');root.innerHTML='';$('bandAuto').checked=automatic;for(let input=1;input<=16;input++){const label=document.createElement('label');label.className='channel';label.innerHTML=`<input type="checkbox" value="${input}" ${selected.includes(input)?'checked':''} ${automatic?'disabled':''}><span>Input ${input}</span>`;label.querySelector('input').onchange=()=>{bandInputs=Array.from(root.querySelectorAll('input:checked')).map(item=>Number(item.value));bandMode='custom';renderBandInputs();saveFixtures()};root.appendChild(label)}}
$('fixtureCount').onchange=()=>{const count=Number($('fixtureCount').value);enabled=Array.from({length:count},(_,i)=>enabled[i]??true);sources=Array.from({length:count},(_,i)=>sources[i]??0);renderFixtures();renderBandInputs();saveFixtures()};
$('bandAuto').onchange=()=>{if($('bandAuto').checked)bandMode='auto';else{if(bandMode==='auto'&&bandInputs.length===0)bandInputs=automaticBandInputs();bandMode='custom'}renderBandInputs();saveFixtures()};
$('saveSource').onclick=()=>post({osc_model:$('oscModel').value,osc_ip:$('oscIp').value.trim()},'Audio source saved; reconnecting to the console')
async function load(){try{const r=await fetch('/api/status',{cache:'no-store'}),s=await r.json();enabled=s.controls.fixture_enabled;sources=s.controls.spotlight_inputs;bandMode=s.controls.band_mode||'auto';bandInputs=s.controls.band_inputs||[];$('fixtureCount').value=s.controls.fixture_count;$('oscModel').value=s.controls.osc_model;$('oscIp').value=s.controls.osc_ip;renderFixtures();renderBandInputs();$('toast').textContent='Ready'}catch(e){$('toast').textContent='Could not load settings: '+e.message}}
load();
</script></body></html>'''


def start_control_panel(controller):
    app = Flask("lights_control_panel")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    patterns = AVAILABLE_PATTERNS

    @app.get("/")
    def panel_home():
        return PANEL_HTML

    @app.get("/settings")
    def panel_settings():
        return SETTINGS_HTML

    @app.get("/api/status")
    def panel_status():
        now = time.monotonic()
        meter_age = None if controller.last_valid_meter_time is None else now - controller.last_valid_meter_time
        return jsonify({
            "controls": dict(controller.controls),
            "fixture_addresses": list(controller.fixture_addresses),
            "effective_band_inputs": effective_band_inputs(controller.controls),
            "vocal": controller.smoothed_voice,
            "band": controller.smoothed_band,
            "meter_age": meter_age,
            "dmx_frames": controller.dmx_frames_submitted,
            "error": controller.last_error or controller.last_network_error,
        })

    @app.post("/api/control")
    def panel_control():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send a JSON object"), 400
        supported = {
            "sensitivity", "audio_enabled", "brightness", "pattern",
            "cycle_colors", "cycle_speed", "fixture_count", "fixture_enabled",
            "spotlight_inputs", "band_mode", "band_inputs", "osc_model", "osc_ip",
        }
        if not supported.intersection(data):
            return jsonify(error="No supported controls provided"), 400

        with controller.controls_lock:
            previous = controller.controls
            updated = dict(previous)
            if "sensitivity" in data:
                try:
                    value = float(data["sensitivity"])
                except (TypeError, ValueError):
                    return jsonify(error="Sensitivity must be a number"), 400
                if not 0.5 <= value <= 2.0:
                    return jsonify(error="Sensitivity must be between 0.50× and 2.00×"), 400
                updated["sensitivity"] = value
            if "audio_enabled" in data:
                value = data["audio_enabled"]
                if type(value) is not bool:
                    return jsonify(error="audio_enabled must be true or false"), 400
                updated["audio_enabled"] = value
            if "brightness" in data:
                try:
                    value = float(data["brightness"])
                except (TypeError, ValueError):
                    return jsonify(error="Brightness must be a number"), 400
                if not 0.0 <= value <= 1.0:
                    return jsonify(error="Brightness must be between 0% and 100%"), 400
                updated["brightness"] = value
            if "pattern" in data:
                value = data["pattern"]
                if value not in patterns:
                    return jsonify(error="Unknown pattern"), 400
                updated["pattern"] = value
            if "fixture_count" in data:
                value = data["fixture_count"]
                if type(value) is not int or not 1 <= value <= MAX_FIXTURE_COUNT:
                    return jsonify(error=f"fixture_count must be between 1 and {MAX_FIXTURE_COUNT}"), 400
                old_enabled = updated["fixture_enabled"]
                old_spotlights = updated["spotlight_inputs"]
                updated["fixture_count"] = value
                updated["fixture_enabled"] = old_enabled[:value] + [True] * max(0, value - len(old_enabled))
                updated["spotlight_inputs"] = old_spotlights[:value] + [0] * max(0, value - len(old_spotlights))
            count = updated["fixture_count"]
            if "fixture_enabled" in data:
                value = data["fixture_enabled"]
                if not isinstance(value, list) or len(value) != count or any(type(item) is not bool for item in value):
                    return jsonify(error=f"fixture_enabled must contain {count} true/false values"), 400
                updated["fixture_enabled"] = value
            if "spotlight_inputs" in data:
                value = data["spotlight_inputs"]
                if not isinstance(value, list) or len(value) != count or any(type(item) is not int or not 0 <= item <= INPUT_CHANNEL_COUNT for item in value):
                    return jsonify(error=f"spotlight_inputs must contain {count} values from 0 to 16"), 400
                updated["spotlight_inputs"] = value
            if "band_mode" in data:
                value = data["band_mode"]
                if value not in ("auto", "custom"):
                    return jsonify(error="band_mode must be auto or custom"), 400
                updated["band_mode"] = value
            if "band_inputs" in data:
                value = data["band_inputs"]
                if not isinstance(value, list) or any(type(item) is not int or not 1 <= item <= INPUT_CHANNEL_COUNT for item in value) or len(set(value)) != len(value):
                    return jsonify(error="band_inputs must be a list of unique inputs from 1 to 16"), 400
                updated["band_inputs"] = value
            if "cycle_colors" in data:
                value = data["cycle_colors"]
                if (
                    not isinstance(value, list)
                    or not value
                    or any(not isinstance(color, str) or color not in COLOR_CYCLE_PALETTE for color in value)
                    or len(set(value)) != len(value)
                ):
                    return jsonify(error="cycle_colors must be a non-empty list of unique palette colors"), 400
                updated["cycle_colors"] = value
            if "cycle_speed" in data:
                try:
                    value = float(data["cycle_speed"])
                except (TypeError, ValueError):
                    return jsonify(error="Cycle speed must be a number of seconds per color"), 400
                if not 0.25 <= value <= 15.0:
                    return jsonify(error="Cycle speed must be between 0.25 and 15 seconds per color"), 400
                updated["cycle_speed"] = value
            if "osc_model" in data:
                value = data["osc_model"]
                if value not in SUPPORTED_OSC_MODELS:
                    return jsonify(error="Only Behringer XR18 and Midas MR18 are supported"), 400
                updated["osc_model"] = value
            if "osc_ip" in data:
                value = parse_ipv4_address(data["osc_ip"])
                if value is None:
                    return jsonify(error="Enter a valid IPv4 address for the console"), 400
                updated["osc_ip"] = value

            persistent_keys = {
                "audio_enabled", "pattern", "cycle_colors", "cycle_speed",
                "fixture_count", "fixture_enabled", "spotlight_inputs",
                "band_mode", "band_inputs", "osc_model", "osc_ip",
            }
            if persistent_keys.intersection(data):
                try:
                    save_control_settings(updated)
                except OSError as exc:
                    return jsonify(error=f"Could not save settings: {exc}"), 500

            new_addresses = fixture_addresses_for_count(count)
            old_addresses = controller.fixture_addresses
            for address in old_addresses[count:]:
                controller.dmx_buffer[address:address + FIXTURE_CHANNELS] = bytes(FIXTURE_CHANNELS)
            required_length = new_addresses[-1] + FIXTURE_CHANNELS
            if required_length > len(controller.dmx_buffer):
                controller.dmx_buffer.extend(bytes(required_length - len(controller.dmx_buffer)))
            controller.fixture_addresses = new_addresses
            if updated["audio_enabled"] != previous["audio_enabled"]:
                controller.clear_audio_response()
            if (
                updated["pattern"] != previous["pattern"]
                or updated["cycle_colors"] != previous["cycle_colors"]
                or updated["cycle_speed"] != previous["cycle_speed"]
            ):
                controller.color_cycle_started = time.monotonic()
            if updated["osc_ip"] != previous["osc_ip"] or updated["osc_model"] != previous["osc_model"]:
                controller.last_valid_meter_time = None
                controller.last_network_error = None
                controller.smoothed_voice[:] = [0.0] * INPUT_CHANNEL_COUNT
                controller.smoothed_band = 0.0
                controller.clear_audio_response()
            controller.controls = updated
            if updated["osc_ip"] != previous["osc_ip"] or updated["osc_model"] != previous["osc_model"]:
                controller.source_changed.set()
            return jsonify(controls=updated)

    def serve():
        app.run(host=CONTROL_PANEL_HOST, port=CONTROL_PANEL_PORT, debug=False, use_reloader=False, threaded=True)

    thread = threading.Thread(target=serve, name="web-control-panel", daemon=True)
    thread.start()
    print(f"iPad control panel: http://192.168.0.216:{CONTROL_PANEL_PORT}/", flush=True)
    return thread


meters_packet = b"/meters\x00,s\x00\x00/meters/1\x00\x00\x00"


def main():
    try:
        controller = LightingController()
    except Exception as exc:
        print(f"Startup error: {type(exc).__name__}: {exc}", flush=True)
        return 1

    print(
        f"Headless lighting active: {controller.controls['osc_model']} OSC "
        f"{controller.controls['osc_ip']}:{XR18_PORT}, "
        f"{controller.controls['fixture_count']} fixtures on OLA universe {OLA_UNIVERSE}.",
        flush=True,
    )
    print("Press Ctrl+C to stop.", flush=True)
    monitor_stop = threading.Event()
    renewer_stop = threading.Event()
    monitor = threading.Thread(
        target=console_monitor,
        args=(controller, monitor_stop),
        name="console-monitor",
        daemon=True,
    )
    monitor.start()
    panel = start_control_panel(controller)
    renewer = threading.Thread(
        target=meter_renewer,
        args=(controller, renewer_stop),
        name="xr18-meter-renewer",
        daemon=True,
    )
    renewer.start()
    try:
        while True:
            controller.update(time.monotonic())
            time.sleep(0.002)
    except KeyboardInterrupt:
        pass
    finally:
        monitor_stop.set()
        renewer_stop.set()
        renewer.join(timeout=1.0)
        controller.close()
        print("Lighting stopped; fixtures sent blackout.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
