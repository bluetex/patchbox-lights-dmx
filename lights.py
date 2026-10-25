"""Midas/Behringer OSC meter to OLA controller for configurable RGB fixtures."""

import array
import ipaddress
import json
import logging
import math
import os
import random
import re
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
TEMPO_TRIGGER_LEVEL = 0.46
TEMPO_RELEASE_LEVEL = 0.22
TEMPO_MIN_INTERVAL_SECONDS = 0.25
TEMPO_MAX_INTERVAL_SECONDS = 2.0
KICK_PULSE_DECAY_SECONDS = 0.32
BAND_CHASE_BASE_STEPS_PER_SECOND = 0.125
BAND_CHASE_LEVEL_STEPS_PER_SECOND = 0.75
BAND_PAIR_ACCENT_GAIN = 0.66
BAND_ROLL_BASE_GAIN = 0.28
BAND_STROBE_DMX = 24
BAND_SWELL_ATTACK_SECONDS = 0.12
BAND_SWELL_RELEASE_SECONDS = 0.50
BAND_PEAK_DIVE_SECONDS = 0.10
BAND_PEAK_SWELL_SECONDS = 0.45
BAND_PEAK_DIVE_GAIN = 0.24
BAND_PEAKS_PER_EFFECT_CHANGE = 32
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
OUTPUT_SPOTLIGHT_SOURCE_FIRST = INPUT_CHANNEL_COUNT + 1
OUTPUT_SPOTLIGHT_SOURCE_LAST = INPUT_CHANNEL_COUNT + 8
SPOTLIGHT_SOURCE_COUNT = OUTPUT_SPOTLIGHT_SOURCE_LAST
OUTPUT_METER_STREAM = "/meters/5"
AVAILABLE_PATTERNS = {"spotlight", "color_cycle", "chase", "blackout"}
LEGACY_PATTERN_MAP = {
    "auto": "spotlight", "ambient": "spotlight", "unison": "spotlight", "mixed": "spotlight",
    "chase_forward": "chase", "chase_reverse": "chase",
}
DEFAULT_CONTROLS = {
    "sensitivity": 1.0,
    "band_sensitivity": 1.0,
    "audio_enabled": True,
    "brightness": 1.0,
    "pattern": "spotlight",
    "blackout": False,
    "cycle_colors": DEFAULT_CYCLE_COLORS,
    "cycle_speed": 1.5,
    "cycle_layout": "unison",
    "behavior_trigger": "timer",
    "trigger_division": 1,
    "tempo_input": 0,
    "kick_sensitivity": 1.0,
    "kick_influence": 0.0,
    "spotlight_overlay": True,
    "chase_direction": "forward",
    "chase_speed": 1.0,
    "chase_color": "Purple",
    "chase_strobe": False,
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


def osc_string(value):
    encoded = value.encode("utf-8") + b"\x00"
    return encoded + bytes((-len(encoded)) % 4)


def parse_osc_string_message(data):
    """Return (OSC address, string argument) for simple string replies."""
    def read_string(offset):
        end = data.find(b"\x00", offset)
        if end < 0:
            return None, len(data)
        try:
            value = data[offset:end].decode("utf-8")
        except UnicodeDecodeError:
            return None, len(data)
        return value, (end + 4) & ~3

    address, offset = read_string(0)
    tags, offset = read_string(offset)
    if not address or not tags or not tags.startswith(",s"):
        return None
    value, _ = read_string(offset)
    return (address, value) if value is not None else None


def read_mixer_input_names(source_ip):
    """Read channel scribble-strip labels from an XR18/MR18 by OSC."""
    names = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("0.0.0.0", 0))
        probe.settimeout(0.08)
        for channel in range(1, INPUT_CHANNEL_COUNT + 1):
            path = f"/ch/{channel:02d}/config/name"
            probe.sendto(osc_string(path) + osc_string(","), (source_ip, XR18_PORT))
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            try:
                response, sender = probe.recvfrom(2048)
            except socket.timeout:
                continue
            if sender[0] != source_ip:
                continue
            parsed = parse_osc_string_message(response)
            if not parsed:
                continue
            address, label = parsed
            match = re.fullmatch(r"/ch/(\d{2})/config/name", address)
            if match and label.strip():
                names[int(match.group(1))] = label.strip()
    return names


def read_mixer_output_names(source_ip):
    """Read mix-bus and main scribble-strip labels used by output meters."""
    requests = {
        f"/bus/{bus}/config/name": ("aux", bus)
        for bus in range(1, 7)
    }
    requests["/lr/config/name"] = ("main", 0)
    names = {"aux": {}, "main": ""}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("0.0.0.0", 0))
        probe.settimeout(0.08)
        for path in requests:
            probe.sendto(osc_string(path) + osc_string(","), (source_ip, XR18_PORT))
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            try:
                response, sender = probe.recvfrom(2048)
            except socket.timeout:
                continue
            if sender[0] != source_ip:
                continue
            parsed = parse_osc_string_message(response)
            if not parsed:
                continue
            address, label = parsed
            target = requests.get(address)
            if target and label.strip():
                kind, index = target
                if kind == "aux":
                    names["aux"][index] = label.strip()
                else:
                    names["main"] = label.strip()
    return names


def suggested_kick_input(labels):
    """Return the best input whose scribble-strip name suggests a kick drum."""
    candidates = []
    for channel, label in labels.items():
        normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
        if normalized == "kick":
            candidates.append((0, channel))
        elif "kick" in normalized or "bass drum" in normalized or normalized == "bd":
            candidates.append((1, channel))
    return min(candidates)[1] if candidates else None


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
            saved_pattern = saved.get("pattern", controls["pattern"])
            pattern = LEGACY_PATTERN_MAP.get(saved_pattern, saved_pattern)
            blackout = saved.get("blackout", pattern == "blackout")
            if pattern == "blackout":
                pattern = controls["pattern"]
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
                    if type(value) is int and 0 <= value <= SPOTLIGHT_SOURCE_COUNT:
                        controls["spotlight_inputs"][index] = value
            if 0.5 <= sensitivity <= 2.0:
                controls["sensitivity"] = sensitivity
            band_sensitivity = float(saved.get("band_sensitivity", controls["sensitivity"]))
            if 0.5 <= band_sensitivity <= 2.0:
                controls["band_sensitivity"] = band_sensitivity
            if type(audio_enabled) is bool:
                controls["audio_enabled"] = audio_enabled
            if 0.0 <= brightness <= 1.0:
                controls["brightness"] = brightness
            if pattern in AVAILABLE_PATTERNS:
                controls["pattern"] = pattern
            if type(blackout) is bool:
                controls["blackout"] = blackout
            legacy_direction = "reverse" if saved_pattern == "chase_reverse" else controls["chase_direction"]
            chase_direction = saved.get("chase_direction", legacy_direction)
            if chase_direction in ("forward", "reverse", "bounce"):
                controls["chase_direction"] = chase_direction
            cycle_layout = saved.get("cycle_layout", controls["cycle_layout"])
            if cycle_layout in ("unison", "spread"):
                controls["cycle_layout"] = cycle_layout
            behavior_trigger = saved.get("behavior_trigger", controls["behavior_trigger"])
            if behavior_trigger in ("timer", "band_peaks", "beat_sync"):
                controls["behavior_trigger"] = behavior_trigger
            trigger_division = saved.get("trigger_division", controls["trigger_division"])
            if type(trigger_division) is int and trigger_division in (1, 2, 4, 8):
                controls["trigger_division"] = trigger_division
            tempo_input = saved.get("tempo_input", controls["tempo_input"])
            if type(tempo_input) is int and 0 <= tempo_input <= INPUT_CHANNEL_COUNT:
                controls["tempo_input"] = tempo_input
            kick_sensitivity = float(saved.get("kick_sensitivity", controls["kick_sensitivity"]))
            if 0.5 <= kick_sensitivity <= 2.0:
                controls["kick_sensitivity"] = kick_sensitivity
            kick_influence = float(saved.get("kick_influence", controls["kick_influence"]))
            if 0.0 <= kick_influence <= 1.0:
                controls["kick_influence"] = kick_influence
            spotlight_overlay = saved.get("spotlight_overlay", controls["spotlight_overlay"])
            if type(spotlight_overlay) is bool:
                controls["spotlight_overlay"] = spotlight_overlay
            chase_speed = float(saved.get("chase_speed", controls["chase_speed"]))
            if 0.1 <= chase_speed <= 8.0:
                controls["chase_speed"] = chase_speed
            chase_color = saved.get("chase_color", controls["chase_color"])
            if chase_color in COLOR_CYCLE_PALETTE:
                controls["chase_color"] = chase_color
            chase_strobe = saved.get("chase_strobe", controls["chase_strobe"])
            if type(chase_strobe) is bool:
                controls["chase_strobe"] = chase_strobe
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


def parse_meter_values(data, meter_stream=METER_STREAM):
    """Decode a selected XR18 OSC meter stream containing one binary blob."""
    address_end = data.find(b"\x00")
    if address_end < 0 or data[:address_end] != meter_stream.encode("ascii"):
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
    minimum_channels = 16 if meter_stream == METER_STREAM else 8
    if channel_count < minimum_channels or values_end > len(blob):
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
        self.input_names = {}
        self.output_names = {"aux": {}, "main": ""}
        self.input_names_ip = None
        self.input_names_read_at = 0.0
        self.fixture_addresses = fixture_addresses_for_count(self.controls["fixture_count"])
        self.dmx_buffer = bytearray(self.fixture_addresses[-1] + FIXTURE_CHANNELS)
        self.source_changed = threading.Event()

        self.voice_peak_levels = [0.0] * SPOTLIGHT_SOURCE_COUNT
        self.voice_peak_hold_until = [0.0] * SPOTLIGHT_SOURCE_COUNT
        self.voice_peak_active = [False] * SPOTLIGHT_SOURCE_COUNT
        self.voice_captured_peaks = [0.0] * SPOTLIGHT_SOURCE_COUNT
        self.white_levels = [0.0] * SPOTLIGHT_SOURCE_COUNT
        self.smoothed_voice = [0.0] * SPOTLIGHT_SOURCE_COUNT
        self.smoothed_band = 0.0
        self.band_peak_level = 0.0
        self.band_swell_level = 0.0
        self.band_peak_hold_until = 0.0
        self.band_dive_started = -1e9
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
        self.band_peak_events = 0
        self.tempo_beat_events = 0
        self.tempo_bpm = None
        self.tempo_intervals = []
        self.tempo_last_beat = None
        self.tempo_armed = True
        self.kick_pulse_level = 0.0
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
            if not enabled or self.controls["blackout"] or self.controls["pattern"] == "blackout"
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
        self.voice_peak_levels[:] = [0.0] * SPOTLIGHT_SOURCE_COUNT
        self.voice_peak_hold_until[:] = [0.0] * SPOTLIGHT_SOURCE_COUNT
        self.voice_peak_active[:] = [False] * SPOTLIGHT_SOURCE_COUNT
        self.voice_captured_peaks[:] = [0.0] * SPOTLIGHT_SOURCE_COUNT
        self.white_levels[:] = [0.0] * SPOTLIGHT_SOURCE_COUNT
        self.band_peak_level = 0.0
        self.kick_pulse_level = 0.0
        self.band_swell_level = 0.0
        self.band_peak_hold_until = 0.0
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
        fixture_count = len(self.fixture_addresses)
        band_ambient_level = (
            self.band_swell_level
            if controls["audio_enabled"] and (self.band_color_active or self.band_peak_level > 0.01)
            else MIN_BAND_BRIGHTNESS
        )
        if pattern == "spotlight":
            kick_weight = controls["kick_influence"] if controls["audio_enabled"] else 0.0
            kick_ambient_level = MIN_BAND_BRIGHTNESS + 0.40 * self.kick_pulse_level
            ambient_level = band_ambient_level * (1.0 - kick_weight) + kick_ambient_level * kick_weight
        else:
            kick_weight = 0.0
            ambient_level = 0.55 if pattern in ("color_cycle", "chase") else band_ambient_level
        trigger = controls["behavior_trigger"]
        division = controls["trigger_division"]
        if trigger == "band_peaks":
            event_step = self.band_peak_events // division
        elif trigger == "beat_sync":
            event_step = self.tempo_beat_events // division
        else:
            if pattern == "chase":
                event_step = int((now - self.color_cycle_started) * controls["chase_speed"])
            else:
                event_step = int((now - self.color_cycle_started) / max(0.1, controls["cycle_speed"]))

        colors = controls["cycle_colors"]
        base_color_index = event_step % len(colors)
        chase_position = event_step % fixture_count
        spotlight_effect = pattern == "spotlight" and controls["audio_enabled"] and (
            (self.band_color_active and kick_weight < 1.0)
            or (self.kick_pulse_level > 0.02 and kick_weight > 0.0)
        )
        band_phase = self.band_chase_phase * self.band_chase_direction
        kick_phase = float(self.tempo_beat_events // division)
        spotlight_phase = band_phase * (1.0 - kick_weight) + kick_phase * kick_weight
        roll_position = spotlight_phase % fixture_count
        bright_pair = int(spotlight_phase * 0.5) % 2
        palette_turn = int(spotlight_phase / fixture_count) % len(AMBIENT_COLORS)
        dive_age = now - self.band_dive_started
        if dive_age < BAND_PEAK_DIVE_SECONDS:
            dive_gain = BAND_PEAK_DIVE_GAIN
        elif dive_age < BAND_PEAK_DIVE_SECONDS + BAND_PEAK_SWELL_SECONDS:
            progress = (dive_age - BAND_PEAK_DIVE_SECONDS) / BAND_PEAK_SWELL_SECONDS
            progress = progress * progress * (3.0 - 2.0 * progress)
            dive_gain = BAND_PEAK_DIVE_GAIN + (1.0 - BAND_PEAK_DIVE_GAIN) * progress
        else:
            dive_gain = 1.0
        if controls["chase_direction"] == "reverse":
            chase_position = fixture_count - 1 - chase_position
        elif controls["chase_direction"] == "bounce" and fixture_count > 1:
            period = 2 * (fixture_count - 1)
            phase = event_step % period
            chase_position = phase if phase < fixture_count else period - phase

        for index, address in enumerate(self.fixture_addresses):
            spotlight_input = controls["spotlight_inputs"][index]
            white_level = (
                self.white_levels[spotlight_input - 1]
                if controls["audio_enabled"] and spotlight_input
                else 0.0
            )
            if controls["blackout"] or pattern == "blackout" or not controls["fixture_enabled"][index]:
                self.set_fixture_color(address, (0, 0, 0))
                continue
            if white_level > 0.0 and (pattern == "spotlight" or controls["spotlight_overlay"]):
                white = int(white_level * 255)
                self.set_fixture_color(address, (white, white, white))
                continue

            if pattern == "color_cycle":
                color_index = (base_color_index + (index if controls["cycle_layout"] == "spread" else 0)) % len(colors)
                rgb = COLOR_CYCLE_PALETTE[colors[color_index]]
                rgb = tuple(int(channel * ambient_level * AMBIENT_INTENSITY) for channel in rgb)
            elif pattern == "chase":
                rgb = COLOR_CYCLE_PALETTE[controls["chase_color"]]
                distance = abs(index - chase_position)
                gain = 1.0 if distance == 0 else (0.18 if distance == 1 else 0.05)
                rgb = tuple(int(channel * gain * ambient_level) for channel in rgb)
            else:
                rgb = self.ambient_rgb(
                    ambient_level,
                    index,
                    palette_offset=palette_turn if spotlight_effect else 0,
                )
                if spotlight_effect:
                    distance = abs(index - roll_position)
                    distance = min(distance, fixture_count - distance)
                    crest = max(0.0, 1.0 - distance / max(1.0, fixture_count / 2.0))
                    roll_gain = BAND_ROLL_BASE_GAIN + (1.0 - BAND_ROLL_BASE_GAIN) * crest
                    pair_gain = 1.0 if index % 2 == bright_pair else BAND_PAIR_ACCENT_GAIN
                    gain = roll_gain * pair_gain * dive_gain
                    rgb = tuple(int(channel * gain) for channel in rgb)

            strobe = 0
            if pattern == "chase" and controls["chase_strobe"] and index == chase_position:
                strobe = BAND_STROBE_DMX
            self.set_fixture_color(address, rgb, strobe=strobe)

    def process_meter(self, values, now):
        voice_values = values[0:INPUT_CHANNEL_COUNT]
        if self.controls["tempo_input"]:
            self.update_tempo_meter(values[self.controls["tempo_input"] - 1], now)
        sensitivity_db = 12.0 * math.log2(self.controls["sensitivity"])
        band_sensitivity_db = 12.0 * math.log2(self.controls["band_sensitivity"])
        if not self.controls["audio_enabled"]:
            for index, meter_value in enumerate(voice_values):
                level = clean_vu(int(meter_value + sensitivity_db * 256.0))
                current = self.smoothed_voice[index]
                self.smoothed_voice[index] = level if level > current else max(0.0, current - 0.08)
            band_channels = effective_band_inputs(self.controls)
            if band_channels:
                band_meter = max(values[channel - 1] for channel in band_channels)
                band_target = clean_vu(int(band_meter + band_sensitivity_db * 256.0))
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
            band_target = clean_vu(int(band_meter + band_sensitivity_db * 256.0))
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
                    self.band_peak_events += 1

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

    def process_output_meter(self, values, now):
        """Use Aux 1-6 and Main L/R meters as selectable spotlight triggers."""
        sensitivity_db = 12.0 * math.log2(self.controls["sensitivity"])
        # /meters/5 starts with Aux 1-6, then Main L and Main R.
        for offset, meter_value in enumerate(values[:8]):
            index = INPUT_CHANNEL_COUNT + offset
            adjusted_meter = int(meter_value + sensitivity_db * 256.0)
            level = clean_vu(adjusted_meter)
            level_db = meter_db(adjusted_meter)
            current = self.smoothed_voice[index]
            self.smoothed_voice[index] = level if level > current else max(0.0, current - 0.08)
            if not self.controls["audio_enabled"]:
                self.voice_peak_active[index] = False
                self.voice_captured_peaks[index] = 0.0
                continue
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

    def update_tempo_meter(self, meter_value, now):
        """Estimate tempo from transient intervals on the selected kick input."""
        if not self.controls["audio_enabled"] or not self.controls["tempo_input"]:
            return
        sensitivity_db = 12.0 * math.log2(self.controls["kick_sensitivity"])
        level = clean_vu(int(meter_value + sensitivity_db * 256.0))
        if level <= TEMPO_RELEASE_LEVEL:
            self.tempo_armed = True
            return
        if level < TEMPO_TRIGGER_LEVEL or not self.tempo_armed:
            return

        self.tempo_armed = False
        if self.tempo_last_beat is not None:
            interval = now - self.tempo_last_beat
            if interval > TEMPO_MAX_INTERVAL_SECONDS:
                self.tempo_intervals.clear()
                self.tempo_bpm = None
            elif interval < TEMPO_MIN_INTERVAL_SECONDS:
                return
            else:
                self.tempo_intervals.append(interval)
                self.tempo_intervals = self.tempo_intervals[-7:]
                ordered = sorted(self.tempo_intervals)
                median_interval = ordered[len(ordered) // 2]
                measured_bpm = 60.0 / median_interval
                self.tempo_bpm = (
                    measured_bpm
                    if self.tempo_bpm is None
                    else 0.35 * measured_bpm + 0.65 * self.tempo_bpm
                )
        self.tempo_last_beat = now
        self.tempo_beat_events += 1
        self.kick_pulse_level = 1.0

    def update(self, now):
        with self.controls_lock:
            self._update_locked(now)

    def _update_locked(self, now):
        elapsed = max(0.0, now - self.last_engine_update)
        self.last_engine_update = now
        pattern = self.controls["pattern"]
        self.kick_pulse_level = max(
            0.0,
            self.kick_pulse_level - elapsed / KICK_PULSE_DECAY_SECONDS,
        )
        if pattern == "spotlight" and (self.band_color_active or self.band_peak_level > 0.01):
            chase_rate = BAND_CHASE_BASE_STEPS_PER_SECOND + self.smoothed_band * BAND_CHASE_LEVEL_STEPS_PER_SECOND
            self.band_chase_phase += elapsed * chase_rate

        for index in range(SPOTLIGHT_SOURCE_COUNT):
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
            self.voice_peak_active[:] = [False] * SPOTLIGHT_SOURCE_COUNT
            self.voice_captured_peaks[:] = [0.0] * SPOTLIGHT_SOURCE_COUNT
            self.band_color_active = False
            self.band_captured_peak = 0.0
        if self.tempo_last_beat is not None and now - self.tempo_last_beat > TEMPO_MAX_INTERVAL_SECONDS:
            self.tempo_bpm = None
            self.tempo_intervals.clear()

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

        received_input_meter = None
        received_output_meter = None
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
            input_values = parse_meter_values(data, METER_STREAM)
            output_values = parse_meter_values(data, OUTPUT_METER_STREAM)
            if input_values is not None:
                received_input_meter = input_values
                self.valid_meter_packets_received += 1
            elif output_values is not None:
                received_output_meter = output_values
                self.valid_meter_packets_received += 1

        if received_input_meter is not None or received_output_meter is not None:
            self.last_valid_meter_time = now
            if received_input_meter is not None:
                self.process_meter(received_input_meter, now)
            if received_output_meter is not None:
                self.process_output_meter(received_output_meter, now)

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
            for packet in meters_packets:
                controller.socket.sendto(packet, (source_ip, XR18_PORT))
            controller.last_meter_subscription = time.monotonic()
            controller.meter_subscription_requests += len(meters_packets)
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
:root{color-scheme:dark;font:17px system-ui,-apple-system,sans-serif;background:#10131b;color:#f3f5fa}*{box-sizing:border-box}body{margin:0 auto;max-width:760px;padding:22px 18px 40px}h1{font-size:27px;margin:4px 0 5px}p.sub{margin:0 0 22px;color:#aeb6c7}.card{background:#1b202c;border:1px solid #30384a;border-radius:18px;padding:18px;margin:14px 0}.label{display:flex;justify-content:space-between;gap:14px;font-weight:650;margin:0 0 12px}.value{color:#91caff;font-variant-numeric:tabular-nums}input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:64px;margin:0;background:transparent;touch-action:pan-y;cursor:pointer;accent-color:#63aaff}input[type=range]::-webkit-slider-runnable-track{height:12px;background:#3b465c;border-radius:12px}input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:34px;height:34px;margin-top:-11px;border:3px solid #f3f5fa;border-radius:50%;background:#63aaff;box-shadow:0 2px 8px #0008}input[type=range]::-moz-range-track{height:12px;background:#3b465c;border-radius:12px}input[type=range]::-moz-range-thumb{width:28px;height:28px;border:3px solid #f3f5fa;border-radius:50%;background:#63aaff;box-shadow:0 2px 8px #0008}input[type=range]:focus-visible{outline:2px solid #91caff;outline-offset:2px}input[type=range]:disabled{opacity:.4;cursor:not-allowed}select,button{font:inherit;color:inherit;background:#252d3d;border:1px solid #46516a;border-radius:12px;padding:13px 14px}select{width:100%;min-height:52px}button{min-height:54px;font-weight:700;cursor:pointer}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.blackout{background:#8f2835;border-color:#bb5360}.blackout.active{background:#d13e4d;box-shadow:0 0 0 2px #ff9aa455}.meter{display:grid;grid-template-columns:minmax(115px,150px) 1fr 48px;align-items:center;gap:10px;margin:12px 0}.meter-name{font-size:14px;line-height:1.15}.meter-name strong,.meter-name small{display:block}.meter-name small{margin-top:3px;color:#aeb6c7;font-size:12px}.track{height:13px;background:#303749;border-radius:10px;overflow:hidden}.fill{height:100%;width:0;background:linear-gradient(90deg,#55c6bd,#ffd267,#ff6581);transition:width .15s}.small{font-size:14px;color:#aeb6c7;line-height:1.5}.status{display:flex;justify-content:space-between;gap:12px;align-items:center}.audio-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:10px}.audio-toggle{position:relative;display:flex;align-items:center;cursor:pointer;padding:4px}.audio-toggle input{position:absolute;opacity:0;width:1px;height:1px}.switch-track{position:relative;display:block;width:46px;height:26px;border-radius:16px;background:#46516a;transition:background .15s}.switch-track:after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#f3f5fa;transition:transform .15s}.audio-toggle input:checked+.switch-track{background:#3d9a72}.audio-toggle input:checked+.switch-track:after{transform:translateX(20px)}.audio-toggle input:focus-visible+.switch-track{outline:2px solid #91caff;outline-offset:3px}.dot{width:9px;height:9px;display:inline-block;border-radius:50%;background:#f0ba55;margin-right:8px}.dot.ok{background:#49d49a}.page-header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.settings-link{display:inline-flex;align-items:center;min-height:44px;margin:0;color:#91caff;font-weight:650;text-decoration:none;white-space:nowrap}.cycle-settings{margin-top:14px}.cycle-settings[hidden]{display:none}.cycle-intro{margin:0 0 10px}.color-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.color-choice{display:flex;align-items:center;gap:7px;min-height:48px;padding:7px 9px;background:#252d3d;border:1px solid #46516a;border-radius:10px;font-size:14px}.color-choice input{width:20px;height:20px;accent-color:#63aaff}.color-dot{width:15px;height:15px;border-radius:50%;box-shadow:0 0 0 1px #fff5}.cycle-speed{margin-top:14px}.toast{min-height:21px;color:#9bc9ff;font-size:14px;margin-top:10px}@media(max-width:500px){body{padding:16px 12px}.card{padding:15px}.meter{grid-template-columns:minmax(90px,110px) 1fr 42px;gap:7px}.color-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body>
<header class="page-header"><h1>Patchbox Lights</h1><a class="settings-link" href="/settings">Settings →</a></header>
<p class="sub">Live controls · same private network</p>
<section class="card"><div class="label"><span>Mode</span><span class="value" id="patternName">Spotlight</span></div>
<select id="pattern" aria-label="Lighting mode"><option value="spotlight">Spotlight · meter driven</option><option value="color_cycle">Color cycle</option><option value="chase">Chase</option></select>
<div id="effectSettings" hidden><div id="triggerSettings"><label class="small">Effect advances on<select id="behaviorTrigger"><option value="timer">Timer</option><option value="band_peaks">Band peaks</option><option value="beat_sync">Kick beat sync</option></select></label><p class="small" id="triggerHelp">Kick beat sync uses the selected kick input to advance color and chase effects. White spotlights keep following their assigned meters.</p><label class="small" id="divisionWrap" hidden>Advance effect every<select id="triggerDivision"><option value="1">1 beat / peak</option><option value="2">2 beats / peaks</option><option value="4">4 beats / peaks</option><option value="8">8 beats / peaks</option></select></label></div>
<div id="cycleSettings" hidden><p class="small cycle-intro">Choose the colors and whether fixtures change together or spread across the palette.</p><div class="color-grid" id="cycleColors"><label class="color-choice"><input type="checkbox" value="Purple"><i class="color-dot" style="background:#a030ff"></i>Purple</label><label class="color-choice"><input type="checkbox" value="Green"><i class="color-dot" style="background:#00ff3c"></i>Green</label><label class="color-choice"><input type="checkbox" value="Teal"><i class="color-dot" style="background:#00d2be"></i>Teal</label><label class="color-choice"><input type="checkbox" value="Magenta"><i class="color-dot" style="background:#ff00b4"></i>Magenta</label><label class="color-choice"><input type="checkbox" value="Blue"><i class="color-dot" style="background:#1450ff"></i>Blue</label><label class="color-choice"><input type="checkbox" value="Cyan"><i class="color-dot" style="background:#00dcff"></i>Cyan</label><label class="color-choice"><input type="checkbox" value="Amber"><i class="color-dot" style="background:#ff9600"></i>Amber</label><label class="color-choice"><input type="checkbox" value="Red"><i class="color-dot" style="background:#ff1818"></i>Red</label></div><label class="small">Fixture layout<select id="cycleLayout"><option value="unison">Unison colors</option><option value="spread">Spread colors</option></select></label><div class="cycle-speed"><div class="label"><span>Seconds per color</span><span class="value" id="cycleSpeedValue">1.50 s</span></div><input id="cycleSpeed" type="range" min="0.25" max="15" step="0.25" value="1.5" aria-label="Seconds per color"></div></div>
<div id="chaseSettings" hidden><label class="small">Chase color<select id="chaseColor"></select></label><label class="small">Direction<select id="chaseDirection"><option value="forward">Forward · 1 to 4</option><option value="reverse">Reverse · 4 to 1</option><option value="bounce">Bounce</option></select></label><div class="cycle-speed"><div class="label"><span>Chase speed</span><span class="value" id="chaseSpeedValue">1.00×</span></div><input id="chaseSpeed" type="range" min="0.1" max="8" step="0.1" value="1" aria-label="Chase speed"></div><label class="small"><input id="chaseStrobe" type="checkbox"> Strobe the active fixture</label></div>
<label class="small"><input id="spotlightOverlay" type="checkbox" checked> Keep meter triggered white spotlights over this effect</label></div>
<div class="toast" id="toast"></div></section>
<section class="card"><div class="label"><span>Brightness</span><span class="value" id="brightnessValue">100%</span></div><input id="brightness" type="range" min="0" max="100" value="100" aria-label="Brightness"></section>
<section class="card"><div class="audio-heading"><strong>Audio response</strong><label class="audio-toggle" title="Toggle audio response"><input id="audioEnabled" type="checkbox" checked aria-label="Audio response"><span class="switch-track" aria-hidden="true"></span></label></div><div class="label"><span>Audio sensitivity</span><span class="value" id="sensitivityValue">1.00×</span></div><input id="sensitivity" type="range" min="50" max="200" value="100" aria-label="Audio sensitivity"><div class="small">Adjust how strongly spotlight meters react. Higher values respond to quieter signals.</div><div id="spotlightInfluenceWrap" hidden style="margin-top:18px"><div class="label"><span id="kickInfluenceLabel">Spotlight Band ↔ Kick influence</span><span class="value" id="kickInfluenceValue">Band 100% · Kick 0%</span></div><input id="kickInfluence" type="range" min="0" max="100" value="0" aria-label="Influence of Kick vs. Band input"><div class="small" id="kickInfluenceHelp">Balances ambient band lighting between the assigned band inputs and selected kick input. White spotlights still follow their assigned meter inputs.</div></div></section>
<section class="card"><div class="status"><strong>Meter input</strong><span class="small" id="meterState"><i class="dot"></i>Connecting</span></div><div id="fixtureMeters"></div><div class="meter"><span>Band</span><div class="track"><div class="fill" id="band"></div></div><span id="bandt">0%</span></div><div class="small" id="detail">Waiting for status…</div></section>
<div class="row"><button id="auto">Spotlight mode</button><button class="blackout" id="blackout" aria-pressed="false">Blackout</button></div>
<script>
const $=id=>document.getElementById(id), names={spotlight:'Spotlight',color_cycle:'Color cycle',chase:'Chase',blackout:'Blackout'},palette=['Purple','Green','Teal','Magenta','Blue','Cyan','Amber','Red'];let timer,currentControls=null,inputNames={},outputNames={aux:{},main:''};
async function control(values){try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});const j=await r.json();if(!r.ok)throw Error(j.error||'Request failed');$('toast').textContent='Control updated';setTimeout(()=>{$('toast').textContent=''},1300)}catch(e){$('toast').textContent='Could not update: '+e.message}}
function delayed(value){clearTimeout(timer);timer=setTimeout(()=>control(value),110)}
$('brightness').addEventListener('input',e=>{$('brightnessValue').textContent=e.target.value+'%';delayed({brightness:Number(e.target.value)/100})});
$('sensitivity').addEventListener('input',e=>{const value=Number(e.target.value);$('sensitivityValue').textContent=(value/100).toFixed(2)+'×';delayed({sensitivity:value/100})});
$('kickInfluence').addEventListener('input',e=>{const value=Number(e.target.value);$('kickInfluenceValue').textContent=`Band ${100-value}% · ${kickInputLabel()} ${value}%`;delayed({kick_influence:value/100})});
$('audioEnabled').addEventListener('change',e=>{const enabled=e.target.checked;$('sensitivity').disabled=!enabled;$('kickInfluence').disabled=!enabled||!currentControls.tempo_input;control({audio_enabled:enabled})});
$('pattern').addEventListener('change',e=>{if(currentControls){currentControls.pattern=e.target.value;currentControls.blackout=false}renderMode();control({pattern:e.target.value,blackout:false})});$('auto').onclick=()=>{$('pattern').value='spotlight';if(currentControls){currentControls.pattern='spotlight';currentControls.blackout=false}renderMode();control({pattern:'spotlight',blackout:false})};$('blackout').onclick=()=>{const enabled=$('blackout').getAttribute('aria-pressed')!=='true';if(currentControls)currentControls.blackout=enabled;renderBlackout(enabled);control({blackout:enabled})};
document.querySelectorAll('#cycleColors input').forEach(input=>input.onchange=()=>{const colors=Array.from(document.querySelectorAll('#cycleColors input:checked')).map(item=>item.value);if(!colors.length){input.checked=true;$('toast').textContent='Select at least one color';return}control({cycle_colors:colors})});
function setCycleSpeed(value,save){$('cycleSpeed').value=value;$('cycleSpeedValue').textContent=Number(value).toFixed(2)+' s';if(save)delayed({cycle_speed:Number(value)})}
$('cycleSpeed').addEventListener('input',e=>setCycleSpeed(e.target.value,true));
function renderBlackout(active){const button=$('blackout');button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));button.textContent=active?'Blackout ON':'Blackout'}
function renderMode(){const pattern=$('pattern').value;$('patternName').textContent=names[pattern]||pattern;$('effectSettings').hidden=pattern==='blackout';$('triggerSettings').hidden=pattern==='spotlight';$('cycleSettings').hidden=pattern!=='color_cycle';$('chaseSettings').hidden=pattern!=='chase';$('behaviorTrigger').options[0].text='Timer';$('divisionWrap').hidden=$('behaviorTrigger').value==='timer';$('triggerHelp').hidden=$('behaviorTrigger').value!=='beat_sync';renderBlackout(Boolean(currentControls&&currentControls.blackout));renderAudioSlider()}
function kickInputLabel(input=Number(currentControls?.tempo_input||0)){if(!input)return 'Kick input not selected';return inputNames[input]?`${inputNames[input]} · Input ${input}`:`Input ${input} (name unavailable)`}
function renderAudioSlider(){if(!currentControls)return;const spotlight=$('pattern').value==='spotlight',kickSelected=Number(currentControls.tempo_input)>0,kickName=kickInputLabel();$('sensitivity').value=String(Math.round(currentControls.sensitivity*100));$('sensitivity').disabled=!currentControls.audio_enabled;$('sensitivityValue').textContent=currentControls.sensitivity.toFixed(2)+'×';$('spotlightInfluenceWrap').hidden=!spotlight;$('kickInfluence').value=String(Math.round(currentControls.kick_influence*100));$('kickInfluence').disabled=!currentControls.audio_enabled||!kickSelected;$('kickInfluenceLabel').textContent=`Spotlight Band input ↔ ${kickName} influence`;$('kickInfluenceValue').textContent=`Band ${100-Math.round(currentControls.kick_influence*100)}% · ${kickName} ${Math.round(currentControls.kick_influence*100)}%`;$('kickInfluenceHelp').textContent=kickSelected?`Balances ambient band lighting between assigned band inputs and ${kickName}. White spotlights still follow their assigned meter inputs.`:'Find or select a kick input in Audio settings to enable Kick influence.';$('behaviorTrigger').options[2].textContent=`Kick beat sync · ${kickName}`}
function bindSelect(id,key){$(id).onchange=()=>control({[key]:key==='trigger_division'?Number($(id).value):$(id).value})}bindSelect('behaviorTrigger','behavior_trigger');bindSelect('triggerDivision','trigger_division');bindSelect('cycleLayout','cycle_layout');bindSelect('chaseDirection','chase_direction');bindSelect('chaseColor','chase_color');$('behaviorTrigger').onchange=()=>{renderMode();control({behavior_trigger:$('behaviorTrigger').value})};$('spotlightOverlay').onchange=()=>control({spotlight_overlay:$('spotlightOverlay').checked});$('chaseStrobe').onchange=()=>control({chase_strobe:$('chaseStrobe').checked});$('chaseSpeed').oninput=e=>{$('chaseSpeedValue').textContent=Number(e.target.value).toFixed(1)+'×';delayed({chase_speed:Number(e.target.value)})};palette.forEach(color=>$('chaseColor').add(new Option(color,color)));
function renderFixtureMeters(addresses){const root=$('fixtureMeters'),count=addresses.length;if(root.dataset.count===String(count))return;root.dataset.count=count;root.innerHTML='';addresses.forEach((address,index)=>{const row=document.createElement('div');row.className='meter';row.innerHTML=`<span class="meter-name"><strong>Fixture ${index+1}</strong><small id="source${index}"></small></span><div class="track"><div class="fill" id="v${index}"></div></div><span id="v${index}t">0%</span>`;root.appendChild(row)})}
function renderKickMeter(status){const input=Number(status.controls.tempo_input||0),root=$('fixtureMeters');let row=$('kickMeterRow');if(!input){if(row)row.remove();return}if(!row){row=document.createElement('div');row.className='meter';row.id='kickMeterRow';const label=document.createElement('span');label.className='meter-name';const title=document.createElement('strong');title.id='kickMeterTitle';const source=document.createElement('small');source.id='kickMeterSource';label.append(title,source);const track=document.createElement('div');track.className='track';const fill=document.createElement('div');fill.className='fill';fill.id='kickMeterFill';track.appendChild(fill);const value=document.createElement('span');value.id='kickMeterValue';value.textContent='0%';row.append(label,track,value);root.appendChild(row)}const level=Math.round((status.vocal[input-1]||0)*100);$('kickMeterTitle').textContent=inputNames[input]||`Input ${input}`;$('kickMeterSource').textContent=`Kick sync · Input ${input}`;$('kickMeterFill').style.width=level+'%';$('kickMeterValue').textContent=level+'%'}
async function loadInputNames(){try{const r=await fetch('/api/input-names',{cache:'no-store'}),j=await r.json();inputNames=j.inputs||{};outputNames=j.outputs||{aux:{},main:''}}catch(e){inputNames={};outputNames={aux:{},main:''}}}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'}),s=await r.json();const addresses=s.fixture_addresses||[];currentControls=s.controls;renderFixtureMeters(addresses);renderKickMeter(s);$('pattern').value=s.controls.pattern;$('behaviorTrigger').value=s.controls.behavior_trigger;renderMode();$('triggerDivision').value=String(s.controls.trigger_division);$('cycleLayout').value=s.controls.cycle_layout;$('chaseDirection').value=s.controls.chase_direction;$('chaseColor').value=s.controls.chase_color;$('chaseStrobe').checked=s.controls.chase_strobe;$('spotlightOverlay').checked=s.controls.spotlight_overlay;$('chaseSpeed').value=s.controls.chase_speed;$('chaseSpeedValue').textContent=Number(s.controls.chase_speed).toFixed(1)+'×';$('audioEnabled').checked=s.controls.audio_enabled;$('sensitivity').disabled=!s.controls.audio_enabled;$('cycleSpeed').value=s.controls.cycle_speed;$('cycleSpeedValue').textContent=Number(s.controls.cycle_speed).toFixed(2)+' s';document.querySelectorAll('#cycleColors input').forEach(input=>input.checked=s.controls.cycle_colors.includes(input.value));$('brightness').value=Math.round(s.controls.brightness*100);$('brightnessValue').textContent=Math.round(s.controls.brightness*100)+'%';renderAudioSlider();for(let i=0;i<addresses.length;i++){const input=s.controls.spotlight_inputs[i],level=input?Math.round(s.vocal[input-1]*100):0,sourceName=!input?'No spotlight':input<=16?(inputNames[input]?`${inputNames[input]} (Input ${input})`:`Input ${input}`):input<=22?(outputNames.aux[input-16]?`Aux ${input-16} · ${outputNames.aux[input-16]}`:`Aux ${input-16}`):(outputNames.main?`Main ${input===23?'L':'R'} · ${outputNames.main}`:`Main ${input===23?'L':'R'} · ${outputNames.main}`);$('source'+i).textContent=`DMX d${String(addresses[i]).padStart(3,'0')} · ${sourceName}`;$('v'+i).style.width=level+'%';$('v'+i+'t').textContent=level+'%'}const band=Math.round(s.band*100);$('band').style.width=band+'%';$('bandt').textContent=band+'%';const live=s.meter_age!==null&&s.meter_age<1.5;$('meterState').innerHTML='<i class="dot '+(live?'ok':'')+'"></i>'+(live?'Live':'No meter data');$('detail').textContent=`${addresses.length} fixtures · DMX updates ${s.dmx_frames} · tempo ${s.tempo_bpm?Math.round(s.tempo_bpm)+' BPM':'—'} · last error: ${s.error||'none'}`}catch(e){$('meterState').innerHTML='<i class="dot"></i>Panel disconnected'}}
loadInputNames().then(refresh);setInterval(refresh,1000);setInterval(loadInputNames,60000);
</script></body></html>'''

SETTINGS_HTML = r'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#10131b"><title>Settings · Patchbox Lights</title>
<style>
:root{color-scheme:dark;font:17px system-ui,-apple-system,sans-serif;background:#10131b;color:#f3f5fa}*{box-sizing:border-box}body{margin:0 auto;max-width:760px;padding:22px 18px 40px}a{color:#91caff;text-decoration:none;font-weight:650;display:inline-flex;align-items:center;min-height:44px}h1{font-size:27px;margin:8px 0 5px}.settings-tabs{display:flex;gap:8px;margin:8px 0 14px}.settings-tabs a{padding:8px 14px;border:1px solid #46516a;border-radius:12px}.settings-tabs a.active{background:#252d3d;color:#fff}.sub,.hint{color:#aeb6c7;line-height:1.5}.card{background:#1b202c;border:1px solid #30384a;border-radius:18px;padding:16px;margin:14px 0}.fixture{display:grid;grid-template-columns:1fr 1.1fr;gap:14px;align-items:center;padding:14px 0;border-bottom:1px solid #30384a}.fixture:last-child{border-bottom:0}.fixture-title{font-weight:700}.fixture-address{display:block;color:#aeb6c7;font-size:14px;margin-top:3px}.enable{display:flex;align-items:center;gap:10px;min-height:52px}.enable input{width:28px;height:28px;accent-color:#63aaff}.source-label{font-size:13px;color:#aeb6c7;margin-bottom:5px}select,button,input[type=text]{font:inherit;color:inherit;background:#252d3d;border:1px solid #46516a;border-radius:12px;padding:12px 13px}select,input[type=text]{width:100%;min-height:52px}button{min-height:52px;font-weight:700;cursor:pointer}.action-row{display:flex;gap:10px}.action-row button{flex:1}.secondary{background:#30384a}.hint{font-size:14px}.band-mode{display:flex;align-items:center;gap:12px;min-height:58px;font-weight:650}.band-mode input{width:28px;height:28px;accent-color:#63aaff}.channel-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.channel{display:flex;align-items:center;gap:7px;min-height:48px;padding:7px 9px;background:#252d3d;border:1px solid #46516a;border-radius:10px;font-size:14px}.channel input{width:21px;height:21px;accent-color:#63aaff}.channel input:disabled{opacity:.75}.setting-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:end}.band-text-list{margin:10px 0 0;padding-left:24px;line-height:1.65}.toast{min-height:24px;color:#9bc9ff;font-size:14px;margin:8px 0}@media(max-width:500px){body{padding:16px 12px}.fixture{grid-template-columns:1fr;gap:5px}.card{padding:12px}.channel-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.setting-row{grid-template-columns:1fr}}
</style></head><body>
<a href="/settings">← Settings</a><nav class="settings-tabs"><a class="active" href="/settings/fixtures">Fixtures</a><a href="/settings/audio">Audio</a></nav><h1>Fixtures</h1><p class="sub">Configure fixture count and spotlight sources.</p>
<section class="card"><h2>Fixtures</h2><label><div class="source-label">Number of fixtures</div><select id="fixtureCount"></select></label><p class="hint">Fixtures use the current seven-channel RGB layout and get sequential DMX start addresses: d001, d008, d015, d022, and so on. Up to 73 fit in one DMX universe.</p><p class="hint">Spotlight source assignments are used in Spotlight mode. In Color cycle and Chase, they are used only when “Keep meter triggered white spotlights over this effect” is enabled; otherwise, the assignments are ignored.</p><div id="fixtures"></div></section>
<section class="card"><h2>Band inputs</h2><p class="hint">Band assignments are configured on the <a href="/settings/audio">Audio settings</a> page. The list below shows the active Band inputs.</p><ul class="band-text-list" id="bandSummary"><li>Loading inputs…</li></ul></section>
<p class="hint">Turning a fixture off blacks it out. Set Spotlight source to Off to keep its ambient color but remove its white meter response. Settings are saved on the Pi and survive app restarts.</p><div class="toast" id="toast">Loading settings…</div>
<script>
const MAX_FIXTURES=73,$=id=>document.getElementById(id);let enabled=[],sources=[],bandMode='auto',bandInputs=[],inputNames={},outputNames={aux:{},main:''};function escapeHtml(value){return String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))}function inputLabel(input){return inputNames[input]?`${escapeHtml(inputNames[input])} · Input ${input}`:`Input ${input}`}function auxLabel(number){const name=outputNames.aux[number];return name?`Aux ${number} · ${escapeHtml(name)}`:`Aux ${number}`}function mainLabel(side){return outputNames.main?`Main ${side} · ${escapeHtml(outputNames.main)}`:`Main ${side}`}
for(let count=1;count<=MAX_FIXTURES;count++)$('fixtureCount').add(new Option(String(count),String(count)));
async function post(values,message){try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}),j=await r.json();if(!r.ok)throw Error(j.error||'Save failed');$('toast').textContent=message}catch(e){$('toast').textContent='Could not save settings: '+e.message}}
function saveFixtures(){return post({fixture_count:enabled.length,fixture_enabled:enabled,spotlight_inputs:sources},'Fixture settings saved on the Pi')}
function renderFixtures(){const root=$('fixtures');root.innerHTML='';const sourceOptions='<option value="0">Off</option><optgroup label="Inputs">'+Array.from({length:16},(_,n)=>`<option value="${n+1}">${inputLabel(n+1)}</option>`).join('')+'</optgroup><optgroup label="Aux outputs">'+Array.from({length:6},(_,n)=>`<option value="${17+n}">${auxLabel(n+1)}</option>`).join('')+'</optgroup><optgroup label="Main outputs"><option value="23">'+mainLabel("L")+'</option><option value="24">'+mainLabel("R")+'</option></optgroup>';for(let i=0;i<enabled.length;i++){const address=1+i*7,row=document.createElement('div');row.className='fixture';row.innerHTML=`<label class="enable"><input type="checkbox" data-enable="${i}" ${enabled[i]?'checked':''}><span><span class="fixture-title">Fixture ${i+1}</span><span class="fixture-address">DMX d${String(address).padStart(3,'0')}</span></span></label><label><div class="source-label">Spotlight source</div><select data-source="${i}">${sourceOptions}</select></label>`;root.appendChild(row);row.querySelector('[data-source]').value=sources[i]}root.querySelectorAll('[data-enable]').forEach(el=>el.onchange=()=>{enabled[Number(el.dataset.enable)]=el.checked;saveFixtures()});root.querySelectorAll('[data-source]').forEach(el=>el.onchange=()=>{sources[Number(el.dataset.source)]=Number(el.value);renderBandSummary();saveFixtures()})}
function renderBandSummary(){const selected=bandMode==='auto'?Array.from({length:16},(_,i)=>i+1).filter(input=>!sources.includes(input)):bandInputs,root=$('bandSummary');root.innerHTML=selected.length?selected.map(input=>`<li>${inputLabel(input)}</li>`).join(''):'<li>No Band inputs selected.</li>'}
$('fixtureCount').onchange=()=>{const count=Number($('fixtureCount').value);enabled=Array.from({length:count},(_,i)=>enabled[i]??true);sources=Array.from({length:count},(_,i)=>sources[i]??0);renderFixtures();renderBandSummary();saveFixtures()};
async function loadNames(){try{const r=await fetch('/api/input-names',{cache:'no-store'}),j=await r.json();inputNames=j.inputs||{};outputNames=j.outputs||{aux:{},main:''}}catch(e){inputNames={};outputNames={aux:{},main:''}}renderFixtures();renderBandSummary()}
async function load(){try{const r=await fetch('/api/status',{cache:'no-store'}),s=await r.json();enabled=s.controls.fixture_enabled;sources=s.controls.spotlight_inputs;bandMode=s.controls.band_mode||'auto';bandInputs=s.controls.band_inputs||[];$('fixtureCount').value=s.controls.fixture_count;renderFixtures();renderBandSummary();$('toast').textContent='Ready';loadNames()}catch(e){$('toast').textContent='Could not load settings: '+e.message}}
load();
</script></body></html>'''

SETTINGS_HOME_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#10131b"><title>Settings · Patchbox Lights</title>
<style>:root{color-scheme:dark;font:17px system-ui,-apple-system,sans-serif;background:#10131b;color:#f3f5fa}*{box-sizing:border-box}body{margin:0 auto;max-width:760px;padding:22px 18px 40px}a{color:#91caff;text-decoration:none;font-weight:650}.back{display:inline-flex;align-items:center;min-height:44px}h1{font-size:27px;margin:8px 0 5px}.sub{color:#aeb6c7;line-height:1.5;margin:0 0 22px}.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.settings-card{display:block;min-height:150px;padding:22px;background:#1b202c;border:1px solid #30384a;border-radius:18px}.settings-card strong{display:block;color:#f3f5fa;font-size:21px;margin-bottom:8px}.settings-card span{display:block;color:#aeb6c7;line-height:1.5}.arrow{color:#91caff!important;margin-top:16px}@media(max-width:500px){body{padding:16px 12px}.settings-grid{grid-template-columns:1fr}}</style></head><body>
<a class="back" href="/">← Live controls</a><h1>Settings</h1><p class="sub">Choose what you want to configure.</p>
<div class="settings-grid"><a class="settings-card" href="/settings/fixtures"><strong>Fixtures</strong><span>Fixture count and spotlight sources.</span><span class="arrow">Configure fixtures →</span></a><a class="settings-card" href="/settings/audio"><strong>Audio</strong><span>Mixer connection, Band assignments, and audio response.</span><span class="arrow">Configure audio →</span></a></div>
</body></html>'''

AUDIO_SETTINGS_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#10131b"><title>Audio Settings · Patchbox Lights</title>
<style>:root{color-scheme:dark;font:17px system-ui,-apple-system,sans-serif;background:#10131b;color:#f3f5fa}*{box-sizing:border-box}body{margin:0 auto;max-width:760px;padding:22px 18px 40px}a{color:#91caff;text-decoration:none;font-weight:650;display:inline-flex;align-items:center;min-height:44px}.settings-tabs{display:flex;gap:8px;margin:8px 0 14px}.settings-tabs a{padding:8px 14px;border:1px solid #46516a;border-radius:12px}.settings-tabs a.active{background:#252d3d;color:#fff}h1{font-size:27px;margin:8px 0 5px}.sub,.hint{color:#aeb6c7;line-height:1.5}.card{background:#1b202c;border:1px solid #30384a;border-radius:18px;padding:16px;margin:14px 0}.setting-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:end}.source-label{font-size:13px;color:#aeb6c7;margin-bottom:5px}select,button,input[type=text]{font:inherit;color:inherit;background:#252d3d;border:1px solid #46516a;border-radius:12px;padding:12px 13px}select,input[type=text]{width:100%;min-height:52px}button{min-height:52px;font-weight:700;cursor:pointer}.range-ends{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:#aeb6c7;margin-top:-4px}.range-ends span:last-child{text-align:right}.band-mode{display:flex;align-items:center;gap:12px;min-height:52px;font-weight:650}.band-mode input{width:24px;height:24px;accent-color:#63aaff}.band-input-list{list-style:none;padding:0;margin:10px 0;display:grid;gap:6px}.band-input-item{display:flex;align-items:center;gap:10px;min-height:44px;padding:8px 10px;background:#252d3d;border:1px solid #46516a;border-radius:10px}.band-input-item input{width:22px;height:22px;accent-color:#63aaff}.band-input-item input:disabled{opacity:.75}.hint{font-size:14px}.toast{min-height:24px;color:#9bc9ff;font-size:14px;margin:8px 0}@media(max-width:500px){body{padding:16px 12px}.card{padding:12px}.setting-row{grid-template-columns:1fr}}</style></head><body>
<a href="/settings">← Settings</a><nav class="settings-tabs"><a href="/settings/fixtures">Fixtures</a><a class="active" href="/settings/audio">Audio</a></nav><h1>Audio</h1><p class="sub">Set the mixer IP address used for audio metering.</p><p><a href="/settings/audio/help">OSC setup help →</a></p>
<section class="card"><h2>OSC audio source</h2><p class="hint">Works with Behringer XR18 and Midas MR18. Both use the same OSC meter connection.</p><label><div class="source-label">Mixer IP address</div><input id="oscIp" type="text" inputmode="decimal" autocomplete="off" placeholder="192.168.0.18"></label><p class="hint">Meter data uses UDP port 10024. Test connection checks whether the mixer replies to OSC; it does not confirm that live meters are arriving. Check the Meter input on the main page for live meter status.</p><div class="action-row"><button class="secondary" id="testConnection">Test connection</button><button id="saveSource">Save audio source</button></div><p class="hint" id="oscTestStatus" aria-live="polite"></p></section>
<section class="card"><h2>Band inputs</h2><p class="hint">Choose which mixer inputs respond as the Band group. In automatic mode, spotlight-assigned inputs are excluded. See the Fixtures page for the current Band input list.</p><label class="band-mode"><input id="bandAuto" type="checkbox"><span>Automatically use inputs not assigned as spotlights</span></label><ul id="bandChannels" class="band-input-list"></ul><div class="action-row"><button class="secondary" id="refreshBandNames">Refresh mixer input names</button></div><p class="hint" id="bandNameStatus">Loading mixer input names…</p></section>
<section class="card"><h2>Band sensitivity</h2><p class="hint">Adjust how strongly the Band inputs selected above react.</p><div class="label"><span>Band sensitivity</span><span class="value" id="bandSensitivityValue">1.00×</span></div><input id="bandSensitivity" type="range" min="50" max="200" value="100" aria-label="Band sensitivity"></section>
<section class="card"><h2>Kick input for beat sync</h2><p class="hint">When mixer input names load or refresh, a channel named “Kick” (or a clear kick variant) is selected automatically. You can change it here any time, or use Find Kick to scan now.</p><label><div class="source-label">Kick meter input</div><select id="tempoInput"><option value="0">Off</option></select></label><div class="action-row"><button class="secondary" id="findKick">Find Kick</button></div><p class="hint" id="kickNameStatus">Loading names from mixer…</p><div class="label"><span>Kick beat-sync sensitivity</span><span class="value" id="kickSensitivityValue">1.00×</span></div><input id="kickSensitivity" type="range" min="50" max="200" value="100" aria-label="Kick beat-sync sensitivity"><div class="range-ends"><span>Higher signal needed</span><span>Quieter signal triggers</span></div><p class="hint">Slide right to let quieter kicks trigger beat detection; slide left to require a stronger signal. This is independent of audio sensitivity and Spotlight’s Band ↔ Kick influence.</p><p class="hint">Beat sync estimates tempo from repeated meter peaks. Channel-name lookup reads the XR/MR18 input names over OSC.</p></section>
<div class="toast" id="toast">Loading settings…</div>
<script>
const $=id=>document.getElementById(id);let bandMode='auto',bandInputs=[],spotlightInputs=[],inputNames={};
function populateKickInputs(names,selected){const select=$('tempoInput');select.innerHTML='';select.add(new Option('Off','0'));for(let i=1;i<=16;i++){const label=names[i]||`Input ${i}`;select.add(new Option(`${label} · Input ${i}`,String(i)))}select.value=String(selected||0)}
async function loadKickNames(force=false,selected=Number($('tempoInput').value)){try{const r=await fetch(force?'/api/input-names/refresh':'/api/input-names',{method:force?'POST':'GET',cache:'no-store'}),j=await r.json();if(!r.ok)throw Error(j.error||'Could not load mixer names');inputNames=j.inputs||{};const activeInput=Number(j.tempo_input??selected)||0;populateKickInputs(inputNames,activeInput);renderBandInputs();const count=Object.keys(inputNames).length;if(j.kick_auto_assigned&&j.kick_suggestion){$('kickNameStatus').textContent=`Suggested Kick found: ${inputNames[j.kick_suggestion]||'Kick'} · Input ${j.kick_suggestion}. Selected automatically; you can change it below.`}else{$('kickNameStatus').textContent=count?`Loaded ${count} mixer input names. You can change the Kick input below.`:'Mixer returned no input names; showing channel numbers.'}$('bandNameStatus').textContent=count?`Loaded ${count} mixer input names.`:'Mixer returned no names; showing channel numbers.'}catch(e){inputNames={};populateKickInputs({},selected);renderBandInputs();$('kickNameStatus').textContent='Could not read mixer names; showing channel numbers.';$('bandNameStatus').textContent='Could not read mixer names; showing channel numbers.'}}
for(let i=1;i<=16;i++)$('tempoInput').add(new Option(`Input ${i}`,String(i)));
function automaticBandInputs(){const spotlight=new Set(spotlightInputs.filter(value=>value>0));return Array.from({length:16},(_,i)=>i+1).filter(input=>!spotlight.has(input))}
function renderBandInputs(){const automatic=bandMode==='auto',selected=automatic?automaticBandInputs():bandInputs,root=$('bandChannels');root.innerHTML='';$('bandAuto').checked=automatic;for(let input=1;input<=16;input++){const item=document.createElement('li'),checkbox=document.createElement('input'),name=document.createElement('span');item.className='band-input-item';checkbox.type='checkbox';checkbox.value=String(input);checkbox.checked=selected.includes(input);checkbox.disabled=automatic;name.textContent=inputNames[input]?`${inputNames[input]} · Input ${input}`:`Input ${input}`;checkbox.onchange=()=>{bandInputs=Array.from(root.querySelectorAll('input:checked')).map(el=>Number(el.value));bandMode='custom';renderBandInputs();saveBandInputs()};item.append(checkbox,name);root.appendChild(item)}}
async function saveBandInputs(){try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({band_mode:bandMode,band_inputs:bandInputs})}),j=await r.json();if(!r.ok)throw Error(j.error||'Save failed');$('toast').textContent='Band inputs saved'}catch(e){$('toast').textContent='Could not save Band inputs: '+e.message}}
$('bandAuto').onchange=()=>{if($('bandAuto').checked)bandMode='auto';else{if(bandMode==='auto'&&!bandInputs.length)bandInputs=automaticBandInputs();bandMode='custom'}renderBandInputs();saveBandInputs()};
async function load(){try{const r=await fetch('/api/status',{cache:'no-store'}),s=await r.json();$('oscIp').value=s.controls.osc_ip;$('tempoInput').value=String(s.controls.tempo_input||0);$('kickSensitivity').value=String(Math.round(s.controls.kick_sensitivity*100));$('kickSensitivityValue').textContent=s.controls.kick_sensitivity.toFixed(2)+'×';$('bandSensitivity').value=String(Math.round(s.controls.band_sensitivity*100));$('bandSensitivityValue').textContent=s.controls.band_sensitivity.toFixed(2)+'×';bandMode=s.controls.band_mode||'auto';bandInputs=s.controls.band_inputs||[];spotlightInputs=s.controls.spotlight_inputs||[];renderBandInputs();$('toast').textContent='Ready';loadKickNames(false,s.controls.tempo_input||0)}catch(e){$('toast').textContent='Could not load settings: '+e.message}}
$('refreshBandNames').onclick=async()=>{const button=$('refreshBandNames');button.disabled=true;await loadKickNames(true);button.disabled=false};
$('testConnection').onclick=async()=>{const button=$('testConnection');button.disabled=true;$('oscTestStatus').textContent='Checking for an OSC reply…';try{const r=await fetch('/api/test-osc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({osc_ip:$('oscIp').value.trim()})}),j=await r.json();if(!r.ok)throw Error(j.error||'Connection test failed');$('oscTestStatus').textContent=`OSC test passed: ${j.osc_ip} replied. This confirms the mixer answered; check the main page for live meter data.`}catch(e){$('oscTestStatus').textContent='OSC test failed: '+e.message}finally{button.disabled=false}};$('oscIp').oninput=()=>$('oscTestStatus').textContent='';
$('saveSource').onclick=async()=>{try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({osc_ip:$('oscIp').value.trim()})}),j=await r.json();if(!r.ok)throw Error(j.error||'Save failed');$('toast').textContent='Audio source saved; reconnecting to the console'}catch(e){$('toast').textContent='Could not save audio settings: '+e.message}};
$('tempoInput').onchange=async()=>{try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tempo_input:Number($('tempoInput').value)})}),j=await r.json();if(!r.ok)throw Error(j.error||'Save failed');$('toast').textContent='Kick input saved'}catch(e){$('toast').textContent='Could not save kick input: '+e.message}};
$('bandSensitivity').oninput=e=>{const value=Number(e.target.value)/100;$('bandSensitivityValue').textContent=value.toFixed(2)+'×';clearTimeout(window.bandSensitivityTimer);window.bandSensitivityTimer=setTimeout(async()=>{try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({band_sensitivity:value})}),j=await r.json();if(!r.ok)throw Error(j.error||'Save failed');$('toast').textContent='Band sensitivity saved'}catch(e){$('toast').textContent='Could not save band sensitivity: '+e.message}},180)};
$('kickSensitivity').oninput=e=>{const value=Number(e.target.value)/100;$('kickSensitivityValue').textContent=value.toFixed(2)+'×';clearTimeout(window.kickSensitivityTimer);window.kickSensitivityTimer=setTimeout(async()=>{try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kick_sensitivity:value})}),j=await r.json();if(!r.ok)throw Error(j.error||'Save failed');$('toast').textContent='Kick beat-sync sensitivity saved'}catch(e){$('toast').textContent='Could not save kick sensitivity: '+e.message}},180)};
$('findKick').onclick=async()=>{const button=$('findKick');button.disabled=true;$('toast').textContent='Reading mixer channel names…';try{const r=await fetch('/api/find-kick',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({osc_ip:$('oscIp').value.trim()})}),j=await r.json();if(!r.ok)throw Error(j.error||'Kick not found');await loadKickNames(false,j.input);$('tempoInput').value=String(j.input);const saved=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tempo_input:j.input})}),result=await saved.json();if(!saved.ok)throw Error(result.error||'Could not save input');$('toast').textContent=`Found “${j.label}” on Input ${j.input}; beat sync is ready`}catch(e){$('toast').textContent=e.message}finally{button.disabled=false}};
load();
</script></body></html>'''


OSC_HELP_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#10131b"><title>OSC Setup Help · Patchbox Lights</title>
<style>:root{color-scheme:dark;font:17px system-ui,-apple-system,sans-serif;background:#10131b;color:#f3f5fa}*{box-sizing:border-box}body{margin:0 auto;max-width:760px;padding:22px 18px 40px}a{color:#91caff;text-decoration:none;font-weight:650;display:inline-flex;align-items:center;min-height:44px}.settings-tabs{display:flex;gap:8px;margin:8px 0 14px}.settings-tabs a{padding:8px 14px;border:1px solid #46516a;border-radius:12px}.settings-tabs a.active{background:#252d3d;color:#fff}h1{font-size:27px;margin:8px 0 5px}h2{font-size:19px;margin:0 0 10px}.sub,.hint,li{color:#aeb6c7;line-height:1.55}.card{background:#1b202c;border:1px solid #30384a;border-radius:18px;padding:16px;margin:14px 0}.card p{line-height:1.55}.card ol,.card ul{padding-left:24px}.card li{margin:8px 0}.pill{display:inline-block;padding:2px 8px;border-radius:8px;background:#252d3d;color:#dce8ff;font-family:ui-monospace,monospace}</style></head><body>
<a href="/settings/audio">← Audio settings</a><nav class="settings-tabs"><a href="/settings/fixtures">Fixtures</a><a class="active" href="/settings/audio">Audio</a></nav><h1>OSC setup help</h1><p class="sub">Connect Patchbox Lights to a Behringer XR18 or Midas MR18 meter stream.</p>
<section class="card"><h2>1. Put the mixer and Pi on the same network</h2><p>Connect the XR18/MR18 and Raspberry Pi to the same private network, such as BANDNET. The XR18 can join an existing network in Wi-Fi Client mode or connect by Ethernet; the MR18 has the same M AIR remote network workflow. If the mixer is in Access Point mode, devices that need to talk to it must join that mixer network.</p><p>Use the mixer’s network page in the X AIR or M AIR control app to find its IP address. Check that the Pi is reachable on the same network.</p></section>
<section class="card"><h2>2. Enter the console address here</h2><ol><li>Open <a href="/settings/audio">Audio settings</a>.</li><li>Enter the mixer’s current IPv4 address, then select <strong>Save audio source</strong>.</li></ol><p>The IP entered here must be the mixer address, not the Pi address. If the router assigns addresses automatically, a DHCP reservation helps keep the mixer address stable.</p></section>
<section class="card"><h2>3. Check that meters are arriving</h2><p>Return to the live controls page. The meter status should change to <strong>Live</strong>. Audio sensitivity and spotlight response use incoming meter levels.</p><p>Patchbox Lights requests the channel meter stream and the Aux/Main output meter stream from UDP port <span class="pill">10024</span>. There is no separate OSC enable switch in this app’s settings.</p><ul><li>If status says <strong>No meter data</strong>, re-check the mixer IP entered in Audio settings.</li><li>Confirm both devices are on the same reachable private network and that Wi-Fi client isolation or firewall rules are not blocking UDP traffic.</li><li>Confirm the console is powered on and its network control app can connect from the same network.</li></ul></section>
<section class="card"><h2>What the app reads</h2><p><span class="pill">/meters/1</span> supplies channel meters used for Input 1–16. <span class="pill">/meters/5</span> supplies Aux 1–6 and Main L/R meters for spotlight-source assignments.</p><p class="hint">The XR/MR OSC protocol uses UDP port 10024. Consult the <a href="https://mediadl.musictribe.com/download/software/behringer/XAIR/X%20AIR%20Remote%20Control%20Protocol.pdf" target="_blank" rel="noopener">X AIR OSC protocol document</a> and <a href="https://www.behringer.com/en/products/0605-AAD" target="_blank" rel="noopener">XR18 network overview</a> for manufacturer details.</p></section>
</body></html>'''



def start_control_panel(controller):
    app = Flask("lights_control_panel")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    patterns = AVAILABLE_PATTERNS

    @app.get("/")
    def panel_home():
        return PANEL_HTML

    @app.get("/settings")
    def panel_settings():
        return SETTINGS_HOME_HTML

    @app.get("/settings/fixtures")
    def panel_fixture_settings():
        return SETTINGS_HTML

    @app.get("/settings/audio")
    def panel_audio_settings():
        return AUDIO_SETTINGS_HTML

    @app.get("/settings/audio/help")
    def panel_osc_help():
        return OSC_HELP_HTML

    def assign_suggested_kick(labels):
        suggestion = suggested_kick_input(labels)
        if suggestion is None:
            return None, False
        with controller.controls_lock:
            if controller.controls["tempo_input"] != suggestion:
                updated = dict(controller.controls)
                updated["tempo_input"] = suggestion
                save_control_settings(updated)
                controller.controls = updated
                controller.tempo_beat_events = 0
                controller.tempo_bpm = None
                controller.tempo_intervals.clear()
                controller.tempo_last_beat = None
                controller.tempo_armed = True
                controller.kick_pulse_level = 0.0
        return suggestion, True

    @app.post("/api/test-osc")
    def panel_test_osc():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send a JSON object with osc_ip"), 400
        source_ip = parse_ipv4_address(data.get("osc_ip"))
        if source_ip is None:
            return jsonify(error="Enter a valid mixer IPv4 address"), 400

        info_request = b"/info\x00\x00\x00,\x00\x00\x00"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.settimeout(1.5)
                probe.sendto(info_request, (source_ip, XR18_PORT))
                while True:
                    response, sender = probe.recvfrom(2048)
                    if sender[0] != source_ip:
                        continue
                    address_end = response.find(b"\x00")
                    if address_end >= 0 and response[:address_end] in (b"/info", b"/xinfo"):
                        return jsonify(ok=True, osc_ip=source_ip)
        except socket.timeout:
            return jsonify(error=f"No OSC reply from {source_ip}:{XR18_PORT} within 1.5 seconds"), 504
        except OSError as exc:
            return jsonify(error=f"OSC probe failed: {type(exc).__name__}: {exc}"), 502

    @app.post("/api/find-kick")
    def panel_find_kick():
        data = request.get_json(silent=True) or {}
        source_ip = parse_ipv4_address(data.get("osc_ip", controller.controls["osc_ip"]))
        if source_ip is None:
            return jsonify(error="Enter a valid mixer IPv4 address"), 400
        try:
            labels = read_mixer_input_names(source_ip)
            controller.input_names = labels
            controller.output_names = read_mixer_output_names(source_ip)
            controller.input_names_ip = source_ip
            controller.input_names_read_at = time.monotonic()
            channel = suggested_kick_input(labels)
            if channel is None:
                return jsonify(error="No channel named Kick was found. Name the kick channel ‘Kick’ on the mixer, or select its input manually."), 404
            return jsonify(input=channel, label=labels[channel])
        except OSError as exc:
            return jsonify(error=f"Could not read mixer channel names: {type(exc).__name__}: {exc}"), 502

    @app.get("/api/input-names")
    def panel_input_names():
        source_ip = controller.controls["osc_ip"]
        now = time.monotonic()
        auto_assigned = False
        if controller.input_names_ip != source_ip or now - controller.input_names_read_at > 60:
            try:
                controller.input_names = read_mixer_input_names(source_ip)
                controller.output_names = read_mixer_output_names(source_ip)
                controller.input_names_ip = source_ip
                controller.input_names_read_at = time.monotonic()
                suggestion, auto_assigned = assign_suggested_kick(controller.input_names)
            except OSError as exc:
                return jsonify(inputs=controller.input_names, outputs=controller.output_names, error=f"Could not read mixer names: {type(exc).__name__}: {exc}"), 200
        else:
            suggestion = suggested_kick_input(controller.input_names)
        return jsonify(inputs=controller.input_names, outputs=controller.output_names, refreshed_at=controller.input_names_read_at, tempo_input=controller.controls["tempo_input"], kick_suggestion=suggestion, kick_auto_assigned=auto_assigned)

    @app.post("/api/input-names/refresh")
    def panel_refresh_input_names():
        source_ip = controller.controls["osc_ip"]
        try:
            controller.input_names = read_mixer_input_names(source_ip)
            controller.output_names = read_mixer_output_names(source_ip)
            controller.input_names_ip = source_ip
            controller.input_names_read_at = time.monotonic()
            suggestion, auto_assigned = assign_suggested_kick(controller.input_names)
        except OSError as exc:
            return jsonify(error=f"Could not read mixer input names: {type(exc).__name__}: {exc}"), 502
        return jsonify(inputs=controller.input_names, outputs=controller.output_names, refreshed_at=controller.input_names_read_at, tempo_input=controller.controls["tempo_input"], kick_suggestion=suggestion, kick_auto_assigned=auto_assigned)

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
            "tempo_bpm": controller.tempo_bpm,
            "tempo_input": controller.controls["tempo_input"],
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
            "sensitivity", "band_sensitivity", "audio_enabled", "brightness", "pattern", "blackout",
            "cycle_colors", "cycle_speed", "fixture_count", "fixture_enabled",
            "spotlight_inputs", "band_mode", "band_inputs", "osc_model", "osc_ip",
            "cycle_layout", "behavior_trigger", "trigger_division", "tempo_input", "kick_sensitivity",
            "kick_influence", "spotlight_overlay", "chase_direction", "chase_speed",
            "chase_color", "chase_strobe",
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
            if "band_sensitivity" in data:
                try:
                    value = float(data["band_sensitivity"])
                except (TypeError, ValueError):
                    return jsonify(error="Band sensitivity must be a number"), 400
                if not 0.5 <= value <= 2.0:
                    return jsonify(error="Band sensitivity must be between 0.50× and 2.00×"), 400
                updated["band_sensitivity"] = value
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
                if value == "blackout":
                    updated["blackout"] = True
                else:
                    updated["pattern"] = value
                    updated["blackout"] = False
            if "blackout" in data:
                value = data["blackout"]
                if type(value) is not bool:
                    return jsonify(error="blackout must be true or false"), 400
                updated["blackout"] = value
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
                if not isinstance(value, list) or len(value) != count or any(type(item) is not int or not 0 <= item <= SPOTLIGHT_SOURCE_COUNT for item in value):
                    return jsonify(error=f"spotlight_inputs must contain {count} source values from 0 to {SPOTLIGHT_SOURCE_COUNT}"), 400
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
            enum_fields = {
                "cycle_layout": ("unison", "spread"),
                "behavior_trigger": ("timer", "band_peaks", "beat_sync"),
                "chase_direction": ("forward", "reverse", "bounce"),
            }
            for key, choices in enum_fields.items():
                if key in data:
                    if data[key] not in choices:
                        return jsonify(error=f"Invalid {key.replace('_', ' ')}"), 400
                    updated[key] = data[key]
            if data.get("behavior_trigger") == "beat_sync" and not data.get("tempo_input", updated["tempo_input"]):
                return jsonify(error="Choose or find a kick input in Audio settings before using beat sync"), 400
            if "trigger_division" in data:
                value = data["trigger_division"]
                if type(value) is not int or value not in (1, 2, 4, 8):
                    return jsonify(error="trigger_division must be 1, 2, 4, or 8"), 400
                updated["trigger_division"] = value
            if "tempo_input" in data:
                value = data["tempo_input"]
                if type(value) is not int or not 0 <= value <= INPUT_CHANNEL_COUNT:
                    return jsonify(error="tempo_input must be Off or Input 1–16"), 400
                updated["tempo_input"] = value
            if "kick_sensitivity" in data:
                try:
                    value = float(data["kick_sensitivity"])
                except (TypeError, ValueError):
                    return jsonify(error="Kick sensitivity must be a number"), 400
                if not 0.5 <= value <= 2.0:
                    return jsonify(error="Kick sensitivity must be between 0.50× and 2.00×"), 400
                updated["kick_sensitivity"] = value
            if "kick_influence" in data:
                try:
                    value = float(data["kick_influence"])
                except (TypeError, ValueError):
                    return jsonify(error="Kick influence must be a percentage"), 400
                if not 0.0 <= value <= 1.0:
                    return jsonify(error="Kick influence must be between 0% and 100%"), 400
                updated["kick_influence"] = value
            if "spotlight_overlay" in data or "chase_strobe" in data:
                for key in ("spotlight_overlay", "chase_strobe"):
                    if key in data:
                        if type(data[key]) is not bool:
                            return jsonify(error=f"{key} must be true or false"), 400
                        updated[key] = data[key]
            if "chase_speed" in data:
                try:
                    value = float(data["chase_speed"])
                except (TypeError, ValueError):
                    return jsonify(error="Chase speed must be a number"), 400
                if not 0.1 <= value <= 8.0:
                    return jsonify(error="Chase speed must be between 0.1 and 8"), 400
                updated["chase_speed"] = value
            if "chase_color" in data:
                if data["chase_color"] not in COLOR_CYCLE_PALETTE:
                    return jsonify(error="Unknown chase color"), 400
                updated["chase_color"] = data["chase_color"]
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
                "audio_enabled", "pattern", "blackout", "cycle_colors", "cycle_speed", "band_sensitivity",
                "fixture_count", "fixture_enabled", "spotlight_inputs",
                "band_mode", "band_inputs", "osc_model", "osc_ip",
                "cycle_layout", "behavior_trigger", "trigger_division", "tempo_input", "kick_sensitivity",
                "kick_influence", "spotlight_overlay", "chase_direction", "chase_speed",
                "chase_color", "chase_strobe",
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
            if updated["tempo_input"] != previous["tempo_input"]:
                controller.tempo_beat_events = 0
                controller.tempo_bpm = None
                controller.tempo_intervals.clear()
                controller.tempo_last_beat = None
                controller.tempo_armed = True
            if (
                updated["pattern"] != previous["pattern"]
                or updated["cycle_colors"] != previous["cycle_colors"]
                or updated["cycle_speed"] != previous["cycle_speed"]
                or updated["chase_speed"] != previous["chase_speed"]
            ):
                controller.color_cycle_started = time.monotonic()
            if updated["osc_ip"] != previous["osc_ip"] or updated["osc_model"] != previous["osc_model"]:
                controller.last_valid_meter_time = None
                controller.last_network_error = None
                controller.smoothed_voice[:] = [0.0] * SPOTLIGHT_SOURCE_COUNT
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


meters_packets = (
    b"/meters\x00,s\x00\x00/meters/1\x00\x00\x00",
    b"/meters\x00,s\x00\x00/meters/5\x00\x00\x00",
)


def main():
    try:
        controller = LightingController()
    except Exception as exc:
        print(f"Startup error: {type(exc).__name__}: {exc}", flush=True)
        return 1

    print(
        f"Headless lighting active: XR18/MR18 OSC "
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
