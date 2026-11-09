# DigiBall ball firmware — pure logic core (runs on CPython for tests AND on the
# ball's MicroPython; stay inside that shared subset: struct only, no other stdlib.)
#
# Wire format is the app's source of truth, never reinvented here: see
# app/src/bridge/wire.ts ("extended 18 B: [0] 0xAA tag | [1] mode byte |
# [2..5] f32 height m | [6..9] f32 distance m | [10..13] f32 speed km/h |
# [14..17] f32 hold s"). The ball emits extended frames only; the legacy
# 5-byte path stays valid in the decoder for older balls.
# Mode bytes must equal MODES[].byte in app/src/data/modes.ts byte-for-byte.

MODE_MAP = {'wurf': 1, 'potato': 2, 'duell': 3, 'freestyle': 4}

import struct


def encode_frame(mode_byte, measurements):
    """Extended frame (18 bytes) exactly as wire.ts decodes it.

    Two struct.pack calls concatenated on purpose: MicroPython's struct
    subset may not accept a repeat count like '4f', so '<BB' + '<ffff'
    is written out instead of '<BB4f'. Unknown measurements are the app's
    null — on the wire numbers only ever come from real events (Task 7
    only sends those), so a carried 0.0 here means "caller supplied it".
    """
    h = float(measurements.get('height') or 0.0)
    d = float(measurements.get('distance') or 0.0)
    s = float(measurements.get('speed') or 0.0)
    hd = float(measurements.get('hold') or 0.0)
    return bytearray(struct.pack('<BB', 0xAA, mode_byte) + struct.pack('<ffff', h, d, s, hd))


def parse_mode_writeback(data):
    """The app writes encodeCounted(modeByte, 0) back to the ball; a known
    mode byte means "switch to this mode". Garbage arriving from a phone
    must stay unknown per the anti-goal list — never guess a mode."""
    if data is None or len(data) < 5:
        return None  # short buffer: not a frame we may interpret
    first = data[0]
    if first in MODE_MAP.values():
        return first
    return None  # unknown byte — including an extended tag sent by mistake


# Earth gravity m/s² — calibration comment: on arrival day this stays 9.81;
# only the detection thresholds next to it get tuned by hand.
G = 9.81


def estimate_measurements(flight_ms):
    """Honest estimates from flight time only — the ball is the source of
    truth but never claims exactness. Formulas pinned by the design record:
    height = 0.5 * G * (flight_s / 2) ** 2 (the old sketch's intent),
    distance = 4 * height (audit E, partner decision 2026-11-08 — a 45° arc's range is FOUR
    times its apex, R = v0²/g and h = v0²/4g; the 2× line below was the old label and the
    speed line always assumed this model, so the two lines are consistent now),
    speed = 3.6 * (2 * (G * height)) ** 0.5 (initial velocity of that arc,
    km/h). Callers add `hold` separately; 0 means "no throw happened" and
    Task 7 only ever sends frames for real events."""
    flight_s = float(flight_ms) / 1000.0
    height = 0.5 * G * (flight_s / 2.0) ** 2
    distance = 4.0 * height   # audit E (partner decision 2026-11-08): was 2.0 — an estimate on a
                              # wrong model; the app-visible distance doubles with this change.
    # launch speed of the same 45° arc the distance assumes: v0 = 2·√(G·h),
    # km/h — one ballistic model for both numbers (final-review fix).
    speed = 3.6 * 2.0 * (G * height) ** 0.5
    return {'height': height, 'distance': distance, 'speed': speed}


def hold_seconds(round_start_ms, end_ms):
    """Potato placeholder measure: held from round start until released —
    the mode counts the SHORTEST hold, so this is exactly what it needs.
    'Released' is detector-agnostic on the ball side (a fresh spike after
    500 ms means the ball left the hand); if never released, the deadline
    bounds it. Back-burner placeholder like the mode itself."""
    span = int(end_ms) - int(round_start_ms)
    if span < 0:
        span = 0
    return span / 1000.0


# Calibration knobs — tuned by hand on arrival day with real throws in hand.
TH_GSUM = 2.5    # calibration knob — tuned by hand on arrival day
STOP_GSUM = 0.3  # calibration knob — tuned by hand on arrival day
MIN_LOW = 1      # calibration knob — tuned by hand on arrival day (audit D, 2026-11-08: the
                 # minimum run of below-STOP_GSUM samples an impact must be preceded by; pinned
                 # at 1 = one 20 Hz tick of free fall is enough proof the ball was airborne)
BUFFER_CAP = 20  # out-of-range hold-out capacity; oldest drops beyond this


class FlightDetector:
    """Idle → in-flight on a spike over TH_GSUM (the release). The flight is then
    closed by the NEXT spike over TH_GSUM — the landing jolt or the catch — and only
    after at least MIN_LOW samples below STOP_GSUM have been seen. Comparisons are
    exclusive at both bounds.

    Why this moved (audit D, partner decision 2026-11-08 — the byte-frozen semantics
    re-pinned on purpose): at rest the sum is ~1 g, so "below STOP_GSUM" is only ever
    true in free fall. The old rule closed the flight on the first airborne sample —
    one tick after release — which made the /2 in the height formula halve a one-tick
    number (a 10 cm hop reported as 3 mm, and every landing jolt became a phantom
    throw). The impact closes the flight now, so flight_ms spans the whole arc and the
    /2 means what the sketch's intent said."""
    def __init__(self):
        self.in_flight = False
        self.started_ms = None
        self.lows = 0

    def note(self, t_ms, gsum):
        if not self.in_flight:
            if gsum > TH_GSUM:
                self.in_flight = True
                self.started_ms = t_ms
                self.lows = 0
                return {'kind': 'throw', 't_ms': t_ms}
            return None
        if gsum < STOP_GSUM:
            self.lows += 1                # free fall: the run the impact has to come after
            return None
        if self.lows >= MIN_LOW and gsum > TH_GSUM:
            flight = t_ms - self.started_ms  # the impact closes the flight: the arc is whole
            self.in_flight = False
            self.started_ms = None
            self.lows = 0
            return {'kind': 'catch', 'flight_ms': flight}
        return None


class FrameBuffer:
    """A ball beyond Bluetooth range holds its frames; oldest drop first
    past BUFFER_CAP, with the dropped count kept for the display."""
    def __init__(self):
        self.frames = []  # plain list, not collections.deque(maxlen=...):
        self.dropped = 0  # stays inside the CPython∩MicroPython subset — deque
                          # is not guaranteed on the ball's runtime.

    def add(self, frame_bytes):
        self.frames.append(frame_bytes)
        while len(self.frames) > BUFFER_CAP:
            self.frames.pop(0)
            self.dropped += 1

    def pending_count(self):
        return len(self.frames)

    def dropped_count(self):
        return self.dropped

    def flush(self):
        out = list(self.frames)  # oldest-first: throws keep their order
        self.frames = []         # delivering clears — no re-delivery
        return out


# --- Heiße Kartoffel: ON A BACK BURNER per the partner's words ("keep that in
# the back burner, it might be dropped as a mode now that we know we dont have a
# vibration motor"). No motor goes into the ball. This signal is my call — a
# placeholder only; revisited together with new rules if the mode changes. The
# contract bytes (potato=2, hold in every frame) stay fixed while that waits.
def potato_plan(deadline_ms, interval_ms):
    """Peep timestamps every interval_ms up to the deadline INCLUSIVE — the
    pattern stops at the deadline: sound ends → let go."""
    return list(range(interval_ms, deadline_ms + 1, interval_ms))
