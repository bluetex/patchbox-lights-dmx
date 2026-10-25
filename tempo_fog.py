import time
from datetime import datetime
import pygame
import sys
import socket
import os
from pythonosc import udp_client
import subprocess
import csv
from zoneinfo import ZoneInfo

# Fog machine control uses a relay connected to BCM GPIO 17.
# The relay contacts must be wired across the two remote-switch pins.
FOG_GPIO_PIN = 17
FOG_RELAY_ON_LEVEL = "HIGH"  # Change to "LOW" if your relay module is active-low.
FOG_MOCK_MODE = False  # GPIO17 controls the relay while the screen button is held.
try:
    if sys.platform.startswith("linux"):
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        FOG_ON = GPIO.HIGH if FOG_RELAY_ON_LEVEL.upper() == "HIGH" else GPIO.LOW
        FOG_OFF = GPIO.LOW if FOG_ON == GPIO.HIGH else GPIO.HIGH
        GPIO.setup(FOG_GPIO_PIN, GPIO.OUT, initial=FOG_OFF)
        fog_available = True
    else:
        GPIO = None
        FOG_ON = FOG_OFF = None
        fog_available = False
except (ImportError, RuntimeError) as exc:
    print(f"Fog relay unavailable: {exc}")
    GPIO = None
    FOG_ON = FOG_OFF = None
    fog_available = False

fog_on = False



def set_fog(state):
    """Set the isolated relay output; default to off if GPIO is unavailable."""
    global fog_on
    fog_on = bool(state)
    if fog_available and not FOG_MOCK_MODE:
        GPIO.output(FOG_GPIO_PIN, FOG_ON if fog_on else FOG_OFF)

# ---------------------------------------------------------------------------
# Display setup
# ---------------------------------------------------------------------------
if sys.platform == "darwin":
    os.environ.setdefault("SDL_VIDEO_CENTERED", "1")

pygame.init()
pygame.mouse.set_visible(False)

SCREEN_W, SCREEN_H = 800, 480
fullscreen = os.environ.get("TEMPO_FULLSCREEN", "0" if sys.platform == "darwin" else "1") == "1"
display_flags = pygame.FULLSCREEN if fullscreen else pygame.RESIZABLE
screen = pygame.display.set_mode((SCREEN_W, SCREEN_H), display_flags)
pygame.display.set_caption("Setlist")
clock = pygame.time.Clock()

# Colors
BLACK     = (0,   0,   0)
WHITE     = (255, 255, 255)
YELLOW    = (255, 220,   0)
GREEN     = (0,   200,   0)
RED       = (200,   0,   0)
BLUE      = (30,  120, 220)
DARK_GRAY = (40,  40,  40)
MID_GRAY  = (80,  80,  80)

# Fonts
font_xl  = pygame.font.Font(None, 80)
font_lg  = pygame.font.Font(None, 60)
font_md  = pygame.font.Font(None, 44)
font_sm  = pygame.font.Font(None, 34)
font_xs  = pygame.font.Font(None, 28)

# Button geometry  (x, y, w, h)
BTN_Y      = SCREEN_H - 110
BTN_H      = 95
PREV_RECT  = pygame.Rect(10,  BTN_Y, 235, BTN_H)
MUTE_RECT  = pygame.Rect(283, BTN_Y, 234, BTN_H)
NEXT_RECT  = pygame.Rect(555, BTN_Y, 235, BTN_H)
FOG_RECT   = pygame.Rect(310, 285, 180, 66)

# ---------------------------------------------------------------------------
# Mixer / OSC
# ---------------------------------------------------------------------------
mixer_port = 10024
mixer_client = None


def get_local_ip():
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
        ip = result.stdout.strip().split()[0]
        if ip:
            return ip
    except Exception:
        pass

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect((mixer_ip or '192.168.0.18', mixer_port))
        ip = probe.getsockname()[0]
        probe.close()
        if ip:
            return ip
    except OSError:
        pass

    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        pass

    return None


def load_ip():
    try:
        with open("mixer_ip.txt", "r") as f:
            ip = f.read().strip()
            if ip:
                return ip
    except Exception:
        pass
    return None


def save_ip(ip):
    with open("mixer_ip.txt", "w") as f:
        f.write(ip)


def update_mixer_client(ip):
    global mixer_client
    mixer_client = udp_client.SimpleUDPClient(ip, mixer_port)


UNITY_VOLUME = 0.75

DEFAULT_CHANNEL_STATES = {
    1: {'volume': UNITY_VOLUME, 'muted': False},
    2: {'volume': UNITY_VOLUME, 'muted': False},
    3: {'volume': UNITY_VOLUME, 'muted': False},
    4: {'volume': UNITY_VOLUME, 'muted': False},
    8: {'volume': UNITY_VOLUME, 'muted': False},
}


def set_default_channel_states():
    for channel, state in DEFAULT_CHANNEL_STATES.items():
        time.sleep(0.05)
        mixer_client.send_message(f"/ch/{channel:02d}/mix/on", 0 if state['muted'] else 1)
        time.sleep(0.05)
        mixer_client.send_message(f"/ch/{channel:02d}/mix/fader", state['volume'])


def set_channel_mute(channel_num, mute_state):
    mixer_client.send_message(f"/ch/{channel_num:02d}/mix/on", 0 if mute_state else 1)


def set_bus5_volume(fxVol):
    volume_map = {0: UNITY_VOLUME, 1: UNITY_VOLUME}
    mixer_client.send_message("/bus/5/mix/fader", volume_map.get(fxVol, UNITY_VOLUME))


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def draw_button(rect, label, bg=MID_GRAY, fg=WHITE):
    pygame.draw.rect(screen, bg, rect, border_radius=14)
    pygame.draw.rect(screen, WHITE, rect, 2, border_radius=14)
    surf = font_md.render(label, True, fg)
    screen.blit(surf, (rect.centerx - surf.get_width() // 2,
                        rect.centery - surf.get_height() // 2))


def fit_text(text, max_width, fonts):
    """Return (surface, font) using the first font that fits max_width."""
    for f in fonts:
        s = f.render(text, True, WHITE)
        if s.get_width() <= max_width:
            return s
    # Last resort: truncate with smallest font
    f = fonts[-1]
    while len(text) > 4:
        s = f.render(text + "…", True, WHITE)
        if s.get_width() <= max_width:
            return s
        text = text[:-1]
    return f.render(text, True, WHITE)


def draw_indicator(cx, cy, label, active, active_color=GREEN):
    color = active_color if active else DARK_GRAY
    radius = 18
    pygame.draw.circle(screen, color, (cx, cy), radius)
    if active:
        pygame.draw.circle(screen, WHITE, (cx, cy), radius, 2)
    txt = font_sm.render(label, True, WHITE)
    screen.blit(txt, (cx - txt.get_width() // 2, cy + radius + 4))


def draw_central_clock():
    now_ct = datetime.now(ZoneInfo("America/Chicago"))
    clock_text = now_ct.strftime("%-I:%M %p CT")
    surf = font_xs.render(clock_text, True, WHITE)
    screen.blit(surf, (SCREEN_W - surf.get_width() - 12, 12))


def update_display(song, blink_state, setlist, fog_on):
    screen.fill(BLACK)
    draw_central_clock()

    # --- Parse key + title from songName (first word = key) ---
    parts = song['songName'].split(' ', 1)
    song_key   = parts[0]
    song_title = parts[1] if len(parts) > 1 else parts[0]

    # --- Blink dot (fixed position, title never moves) ---
    BLINK_CX = 28
    BLINK_CY = 48
    BLINK_R  = 14
    dot_color = WHITE if blink_state else BLACK
    pygame.draw.circle(screen, dot_color, (BLINK_CX, BLINK_CY), BLINK_R)

    # --- Song title (vertically centred on the blink dot) ---
    NAME_X = BLINK_CX + BLINK_R + 12
    name_surf = fit_text(song_title, SCREEN_W - NAME_X - 10,
                         [font_xl, font_lg, font_md])
    name_y = BLINK_CY - name_surf.get_height() // 2
    screen.blit(name_surf, (NAME_X, name_y))

    # --- Position + BPM + Key ---
    set_num    = song['setNum']
    song_num   = song['songNum']
    set_total  = sum(1 for s in setlist if s['setNum'] == set_num)
    pos_txt = font_sm.render(
        f"Set {set_num}  Song {song_num} of {set_total}    {song['tempo']} BPM    Key {song_key}",
        True, YELLOW)
    screen.blit(pos_txt, (10, 105))

    # --- Divider ---
    pygame.draw.line(screen, MID_GRAY, (0, 135), (SCREEN_W, 135), 1)

    # --- Status indicators ---
    indicators = [
        ("REV",  bool(song['reverbVal']),           GREEN),
        ("SLAP", bool(song['slapVal']),             GREEN),
        ("FX+",  song['fxVol'] == 1,               BLUE),
        ("CH2",  bool(song['muteCh2']),             RED),
        ("CH8",  bool(song['muteCh8']),             RED),
    ]
    dot_y     = 220
    spacing   = 148
    start_x   = 80
    for i, (label, active, color) in enumerate(indicators):
        draw_indicator(start_x + i * spacing, dot_y, label, active, color)

    # Return clickable rects for all 5 indicator dots
    r = 18
    rev_rect  = pygame.Rect(start_x + 0 * spacing - r - 10, dot_y - r - 10, (r + 10) * 2, (r + 10) * 2)
    slap_rect = pygame.Rect(start_x + 1 * spacing - r - 10, dot_y - r - 10, (r + 10) * 2, (r + 10) * 2)
    fx_rect   = pygame.Rect(start_x + 2 * spacing - r - 10, dot_y - r - 10, (r + 10) * 2, (r + 10) * 2)
    ch2_rect  = pygame.Rect(start_x + 3 * spacing - r - 10, dot_y - r - 10, (r + 10) * 2, (r + 10) * 2)
    ch8_rect  = pygame.Rect(start_x + 4 * spacing - r - 10, dot_y - r - 10, (r + 10) * 2, (r + 10) * 2)

    # --- Fog control ---
    fog_label = "FOG ON" if fog_on else ("FOG OFF" if fog_available or FOG_MOCK_MODE else "FOG N/A")
    fog_color = RED if fog_on else (DARK_GRAY if fog_available or FOG_MOCK_MODE else MID_GRAY)
    draw_button(FOG_RECT, fog_label, bg=fog_color)

    # --- Buttons ---
    draw_button(PREV_RECT, "< PREV")
    draw_button(MUTE_RECT, "MUTE ALL", bg=RED)
    draw_button(NEXT_RECT, "NEXT >")

    pygame.display.flip()
    return rev_rect, slap_rect, fx_rect, ch2_rect, ch8_rect, FOG_RECT


def show_message(line1, line2="", duration=0):
    screen.fill(BLACK)
    draw_central_clock()
    s1 = font_lg.render(line1, True, WHITE)
    screen.blit(s1, (SCREEN_W // 2 - s1.get_width() // 2, 160))
    if line2:
        s2 = font_md.render(line2, True, YELLOW)
        screen.blit(s2, (SCREEN_W // 2 - s2.get_width() // 2, 250))
    pygame.display.flip()
    if duration > 0:
        time.sleep(duration)


# ---------------------------------------------------------------------------
# IP configuration screen
# ---------------------------------------------------------------------------

def configure_ip():
    global mixer_ip
    my_ip = get_local_ip()
    ip_parts = my_ip.split('.')

    show_message("DEVICE IP", my_ip, 4)
    pygame.mouse.set_visible(True)

    if ip_parts[2] == '0':
        mixer_ip = '192.168.0.18'
    elif ip_parts[2] == '1':
        mixer_ip = '192.168.1.187'
    else:
        mixer_ip = '192.168.0.18'

    save_ip(mixer_ip)
    update_mixer_client(mixer_ip)

    START_RECT = pygame.Rect(300, 340, 200, 80)
    while True:
        screen.fill(BLACK)
        draw_central_clock()
        s = font_lg.render("MIXER IP", True, WHITE)
        screen.blit(s, (SCREEN_W // 2 - s.get_width() // 2, 80))
        s2 = font_md.render(mixer_ip, True, YELLOW)
        screen.blit(s2, (SCREEN_W // 2 - s2.get_width() // 2, 180))
        draw_button(START_RECT, "START", bg=BLUE)
        pygame.display.flip()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                pygame.quit()
                sys.exit()
            if event.type == pygame.KEYDOWN and event.key in (pygame.K_RETURN, pygame.K_SPACE):
                show_message("Starting...", "", 1)
                pygame.mouse.set_visible(False)
                return
            if event.type == pygame.MOUSEBUTTONDOWN:
                if START_RECT.collidepoint(event.pos):
                    show_message("Starting...", "", 1)
                    pygame.mouse.set_visible(False)
                    return

        clock.tick(30)


# ---------------------------------------------------------------------------
# Setlist
# ---------------------------------------------------------------------------

def load_setlist():
    setlist = []
    try:
        with open('./setlist.csv', 'r') as f:
            reader = csv.reader(f)
            for row in reader:
                setlist.append({
                    'setNum':   int(row[0]),
                    'songNum':  int(row[1]),
                    'songName': row[2],
                    'reverbVal': int(row[3]),
                    'slapVal':   int(row[4]),
                    'fxVol':     int(row[5]),
                    'tempo':     int(row[6]),
                    'muteCh2': int(row[7]) if len(row) > 7 and row[7].strip() else 0,
                    'muteCh8': int(row[8]) if len(row) > 8 and row[8].strip() else 0,
                })
    except Exception as e:
        print(f"Error loading setlist: {e}")
    return setlist


def apply_song(song):
    # Unmute all mute groups when resuming the setlist
    for group in range(1, 5):
        mixer_client.send_message(f"/config/mute/{group}", 0)
    mixer_client.send_message("/fxsend/1/mix/on", song['reverbVal'])
    mixer_client.send_message("/fxsend/2/mix/on", song['slapVal'])
    set_bus5_volume(song['fxVol'])
    for channel in DEFAULT_CHANNEL_STATES:
        set_channel_mute(channel, DEFAULT_CHANNEL_STATES[channel]['muted'])
        time.sleep(0.05)
    set_channel_mute(2, song['muteCh2'] == 1)
    set_channel_mute(8, song['muteCh8'] == 1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Initialise mixer IP before configure_ip() is called
mixer_ip = load_ip()
if not mixer_ip:
    my_ip = get_local_ip()
    ip_parts = my_ip.split('.')
    mixer_ip = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.1"
update_mixer_client(mixer_ip)

try:
    configure_ip()
    set_default_channel_states()

    setlist = load_setlist()
    if not setlist:
        show_message("Error", "No Setlist Found", 3)
        pygame.quit()
        sys.exit()

    current_song_index = 0
    apply_song(setlist[current_song_index])

    last_blink_time = time.time()
    blink_state     = False


    # Hold-to-quit: long-press MUTE for 3 seconds
    HOLD_QUIT_SECS = 3.0
    mute_pressed_at = None
    mouse_fog_pressed = False
    fog_fingers = set()
    fingers_on_buttons = {}
    both_held_since = None

    def finger_to_screen(fx, fy):
        return int(fx * SCREEN_W), int(fy * SCREEN_H)

    while True:
        now  = time.time()
        song = setlist[current_song_index]
        blink_interval = 30.0 / song['tempo']

        if now - last_blink_time >= blink_interval:
            last_blink_time += blink_interval
            blink_state = not blink_state

        rev_rect, slap_rect, fx_rect, ch2_rect, ch8_rect, fog_rect = update_display(
            song, blink_state, setlist, fog_on)

        # --- Long-press MUTE to quit ---
        if mute_pressed_at is not None:
            held_for = time.time() - mute_pressed_at
            remaining = HOLD_QUIT_SECS - held_for
            if remaining <= 0:
                raise KeyboardInterrupt
            overlay = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, 160))
            screen.blit(overlay, (0, 0))
            msg = font_lg.render(f"Quitting in {remaining:.1f}s", True, WHITE)
            screen.blit(msg, (SCREEN_W // 2 - msg.get_width() // 2,
                              SCREEN_H // 2 - msg.get_height() // 2))
            hint = font_sm.render("Release to cancel", True, MID_GRAY)
            screen.blit(hint, (SCREEN_W // 2 - hint.get_width() // 2,
                               SCREEN_H // 2 + msg.get_height()))
            pygame.display.flip()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                raise KeyboardInterrupt
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                raise KeyboardInterrupt
            if event.type == pygame.KEYDOWN and event.key in (pygame.K_RIGHT, pygame.K_SPACE, pygame.K_RETURN):
                current_song_index = (current_song_index + 1) % len(setlist)
                apply_song(setlist[current_song_index])
                blink_state = False
                last_blink_time = time.time()

            # Multi-touch finger tracking for hold-to-quit
            if event.type == pygame.FINGERDOWN:
                sx, sy = finger_to_screen(event.x, event.y)
                if fog_rect.collidepoint(sx, sy):
                    fog_fingers.add(event.finger_id)
                    set_fog(True)
                elif PREV_RECT.collidepoint(sx, sy):
                    fingers_on_buttons[event.finger_id] = 'prev'
                elif NEXT_RECT.collidepoint(sx, sy):
                    fingers_on_buttons[event.finger_id] = 'next'

            if event.type == pygame.FINGERUP:
                fingers_on_buttons.pop(event.finger_id, None)
                fog_fingers.discard(event.finger_id)
                set_fog(mouse_fog_pressed or bool(fog_fingers))

            if event.type == pygame.MOUSEBUTTONDOWN:
                pos = event.pos
                now = time.time()

                if MUTE_RECT.collidepoint(pos):
                    mute_pressed_at = now

                if fog_rect.collidepoint(pos):
                    mouse_fog_pressed = True
                    set_fog(True)

                elif rev_rect.collidepoint(pos):
                    song['reverbVal'] = 0 if song['reverbVal'] else 1
                    mixer_client.send_message("/fxsend/1/mix/on", song['reverbVal'])

                elif slap_rect.collidepoint(pos):
                    song['slapVal'] = 0 if song['slapVal'] else 1
                    mixer_client.send_message("/fxsend/2/mix/on", song['slapVal'])

                elif fx_rect.collidepoint(pos):
                    song['fxVol'] = 0 if song['fxVol'] else 1
                    set_bus5_volume(song['fxVol'])

                elif ch2_rect.collidepoint(pos):
                    song['muteCh2'] = 0 if song['muteCh2'] else 1
                    set_channel_mute(2, song['muteCh2'] == 1)

                elif ch8_rect.collidepoint(pos):
                    song['muteCh8'] = 0 if song['muteCh8'] else 1
                    set_channel_mute(8, song['muteCh8'] == 1)

                elif NEXT_RECT.collidepoint(pos):
                    current_song_index = (current_song_index + 1) % len(setlist)
                    apply_song(setlist[current_song_index])
                    blink_state = False
                    last_blink_time = now

                elif PREV_RECT.collidepoint(pos):
                    current_song_index = (current_song_index - 1) % len(setlist)
                    apply_song(setlist[current_song_index])
                    blink_state = False
                    last_blink_time = now

            if event.type == pygame.MOUSEBUTTONUP:
                pos = event.pos
                if mouse_fog_pressed:
                    mouse_fog_pressed = False
                    set_fog(bool(fog_fingers))
                now = time.time()
                if MUTE_RECT.collidepoint(pos):
                    if mute_pressed_at is not None and (now - mute_pressed_at) < HOLD_QUIT_SECS:
                        # short press — mute all mute groups 1-4
                        for group in range(1, 5):
                            mixer_client.send_message(f"/config/mute/{group}", 1)
                        show_message("All Channels", "Muted", 1)
                        blink_state = False
                        last_blink_time = time.time()
                    mute_pressed_at = None

        clock.tick(60)

except KeyboardInterrupt:
    pass

finally:
    set_fog(False)
    if GPIO is not None:
        GPIO.cleanup(FOG_GPIO_PIN)
    pygame.quit()