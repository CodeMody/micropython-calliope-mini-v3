# DigiBall — real firmware for a confirmed Calliope mini v3 (device layer).
#
# The confirmed v3 MicroPython port (calliope-edu/micropython-calliope-mini-v3)
# is a fork of microbit-v2; the older Calliope port is a fork of the classic
# port whose module answers to calliope_mini. Both share the same microbit-style
# API, so this file uses only calls documented for that generation and guards
# the module name itself. Version-specific things get fallback branches that
# fail VISIBLY (face + REPL print), never silently. Nothing below is claimed
# working until the arrival-day checklist in the design record runs on the real
# mini.
#
# Pure logic lives in digiball_core.py (tested on the PC); this file glues it
# to the board: link handling, GATT registration, streaming, face language.

# --- runtime naming (guarded; a wrong runtime reads as an error, not a crash) ---
# Order fixed by the device on 2026-11-06: the board on the desk answered to 'calliopemini'
# (no underscore) and ImportErrors the other two names. The older order was the pinning
# ledger's guess; the device overruled it, so the device's name comes first.
try:
    import calliopemini as board    # the name THIS device answers to (device-answered)
except ImportError:
    try:
        import calliope_mini as board   # older Calliope port's module name
    except ImportError:
        try:
            import microbit as board    # confirmed v3 port: microbit-v2 fork
        except ImportError:
            board = None            # visible failure below, never a silent NameError
if board is None:
    print('no calliopemini/calliope_mini/microbit module on this runtime — main.py needs one')

try:
    from utime import ticks_ms      # both generations' docs list utime.ticks_ms
except ImportError:
    # takeover fix 1 (audit 2026-11-08, partner go) — the delivered image carries no `utime`
    # name (byte scan ×0) and extmod/modtime.c at the pin (calliope-edu/micropython b7ce7a8)
    # has no `monotonic` at all; what it does expose is ticks_ms/ticks_us/ticks_diff/sleep_ms.
    # The old body raised AttributeError on the first loop tick of both files, with no
    # try/except between it and the display line — so it killed the loop, not just the clock.
    import time as _t
    def ticks_ms(): return _t.ticks_ms()

import struct                # byte-frozen: the frame layout is the app's source of truth
try:
    import bluetooth         # the confirmed port's lineage; the device on the desk has NO such module
except ImportError:
    bluetooth = None         # visible below; the live path here is the runtime's own uart
from digiball_core import (MODE_MAP, encode_frame, parse_mode_writeback,
                           estimate_measurements, hold_seconds, FlightDetector,
                           FrameBuffer, potato_plan, TH_GSUM)

try:
    import neopixel          # device-forced: pin_RGB is a plain digital pin; the light language
except ImportError:          # lives on this module (the port's own example documents the class)
    neopixel = None

# The app scans by advertised name prefix exactly 'DigiBall' (BallBridge.tsx).
BALL_NAME_PREFIX = 'DigiBall'      # the app now scans by service UUID instead — the device
                                   # in hand cannot control its advertised name at all
# Placeholder UUIDs — the real ones are whatever WE register and advertise;
# they only need to be consistent between this file and the app's two
# constants (the BallBridge.tsx update rides along with arrival day).
SERVICE_UUID_S = 'e1a57a11-0000-0000-0000-0000000000d9'
FRAME_OUT_UUID_S = 'e1a57a11-00fa-0000-0000-0000000000d9'   # 16-bit code composed onto
MODE_IN_UUID_S = 'e1a57a11-00fb-0000-0000-0000000000d9'     # the registered base (see above)

# The stock UART service, for the runtime that has no BLE API (the one on the desk).
# Why the fallback is the live route here, quoted from that lineage's own docs/ble.rst (read
# directly 2026-11-06, [doc]): "However, until such time it's highly unlikely MicroPython will support BLE."
# The board on the desk confirms it from the other side: no bluetooth module.
# UUIDs read directly from the chip vendor's own service documentation 2026-11-06, written
# here as the vendor writes them: the service, the characteristic the peer WRITES to (the
# device receives), and the characteristic it NOTIFIES on (the device sends). The device in
# hand cannot register these itself — no BLE API — so on this runtime the strings are the
# agreed identity the APP matches on; the frame bytes are unchanged and wire.ts stays the
# source of truth, exactly as the design record's fallback pre-decided.
STOCK_UART_SERVICE_UUID_S = '6E400001-B5A3-F393-E0A9-E50E24DCCA9E'   # service
STOCK_UART_RX_UUID_S = '6E400002-B5A3-F393-E0A9-E50E24DCCA9E'        # app writes here
STOCK_UART_TX_UUID_S = '6E400003-B5A3-F393-E0A9-E50E24DCCA9E'        # app monitors this

# --- light language (device-forced 2026-11-06: pin_RGB is a plain MicroBitDigitalPin — no
# .value() — so the light lives on the neopixel module. The class form is quoted from the
# port's own example (NeoPixel(pin0, 8)); it ran on the board's own RGB pin when asked. The
# colour tuples below are the ones the human partner SAW on the device, so only these three
# primaries are used; mixed palette values stay unused until the device confirms them.
# Meanings are the builder's call (the design record leaves light details to the builder),
# chosen to match the app's chip language where a confirmed colour exists. ---
LIGHT_SEARCH = (0, 0, 255)     # searching for a link — device-confirmed blue
LIGHT_LINKED = (0, 255, 0)     # linked — device-confirmed green; the app's linked chip is green
LIGHT_LOST = (255, 0, 0)       # out of range — device-confirmed red
LIGHT_OFF = (0, 0, 0)          # dark between pulse beats — the partner asked for dark when done
LIGHT_COUNT = 3                # the board's own LEDs (device: three of them, one answers to [0])


def light(colour):
    """The board's own RGB LED, via the documented class path — the only form this device
    accepted. Guarded, and it says so on the REPL; the face keeps working if this fails."""
    try:
        if light.np is None:
            light.np = neopixel.NeoPixel(board.pin_RGB, LIGHT_COUNT)
        light.np[0] = colour
        light.np.show()
    except Exception as err:
        print('light language failed: %r — the face carries the state alone' % err)


light.np = None


# --- transport (the design record's pre-decided fallback, now live: the device in hand has
# no bluetooth module, so frame bytes ride the runtime's own uart; the GATT branch below
# stays for the lineage the record was written against. wire.ts stays the source of truth.) ---
def link_live():
    """The GATT lineage asks the documented irq handler (issue 1). The device in hand has no
    BLE API at all, so it cannot see a link being established — it learns the link exists by
    RECEIVING something, so the link counts as live from the first command on (peep_fired is
    that latch). Saying so here beats pretending to know; the app's side reports its own
    connection state independently, which is why the app keeps its own truth on screen."""
    if bluetooth is None:
        return peep_fired        # the only evidence this runtime can actually get
    return connected_now()


def tp_write(frame):
    """One frame out. Device-answered shape: uart.write() takes exactly one argument."""
    if bluetooth is None:
        board.uart.write(frame)          # device-verified method, single argument
    else:
        bles.gatts_write(CHAR_FRAME_OUT, frame, True)   # the documented lineage's call
    return True


def tp_read():
    """One write-back in, or None. Device-answered shape: uart.read() returned None
    immediately when nothing was waiting, so polling it at 20 Hz does not block the loop."""
    if bluetooth is None:
        return board.uart.read()
    if CHAR_MODE_IN is None:
        return None
    return bles.gatts_read(CHAR_MODE_IN)


# The peep rule as the human partner set it on 2026-11-06 for the runtime in hand: ONE peep
# per boot, on the first command that arrives — this runtime has no BLE API, so it cannot see
# a link being established. The design record's transition peep stays for the lineage that
# CAN see link events. The anti-goal is untouched either way: never a peep per throw.
peep_fired = False


# --- flag constants (the docs say they are not on the module — "Add the ones that you
# need from the list above to your program" — so the ones this file uses live here) ---
FLAG_READ = 0x0002                  # doc value
FLAG_WRITE_NO_RESPONSE = 0x0004     # doc value
FLAG_WRITE = 0x0008                 # doc value
FLAG_NOTIFY = 0x0010                # doc value

# --- event codes (same rule as the flags above — the docs say they are not on the
# module: "Add the ones that you need from the list above to your program") ---
_IRQ_CENTRAL_CONNECT = 1            # doc value
_IRQ_CENTRAL_DISCONNECT = 2         # doc value

bles = None                # module-level BLE instance, set by ensure_ble()
CHAR_FRAME_OUT = None      # characteristic handles from gatts_register_services
CHAR_MODE_IN = None
_probe_reported = False    # one honest report per missing probe API, not spam
link_state = False         # link truth, kept by the irq handler below (issue 1)
_irq_wired = False         # set once the handler is wired via docs' irq(handler)


def link_handler(event, data):
    """docs: irq(handler) registers a callback for BLE-stack events; the handler takes
    the event code and an event-specific tuple. On the confirmed runtime the link
    arrives (and leaves) through these events, so the truth lives in this handler."""
    global link_state
    if event == _IRQ_CENTRAL_CONNECT:
        link_state = True           # a central has connected to this peripheral
    elif event == _IRQ_CENTRAL_DISCONNECT:
        link_state = False          # it has disconnected — the loop sees it on the next poll


def build_services():
    """One service with two characteristics, built lazily so no bluetooth.
    call runs at import time."""
    return ((bluetooth.UUID(SERVICE_UUID_S),
             ((bluetooth.UUID(FRAME_OUT_UUID_S),
               FLAG_READ | FLAG_NOTIFY),   # frames out: app monitors
              (bluetooth.UUID(MODE_IN_UUID_S),
               # the app writes WITHOUT response (writeCharacteristicWithoutResponseForDevice)
               # — the property must include write-without-response (final-review fix I2)
               FLAG_WRITE | FLAG_WRITE_NO_RESPONSE)),
             ),)


def ensure_ble():
    global bles, _irq_wired
    if bles is None:
        try:
            bles = bluetooth.BLE()       # current core MicroPython name (docs library/bluetooth)
        except AttributeError:
            try:
                bles = bluetooth.BluetoothService()  # older ubluetooth-era name in same docs lineage
            except AttributeError:
                print('bluetooth module not usable on this runtime')  # visible; loop won't start
                return None
        try:
            bles.active(True)           # docs: radio must be active before any other method
        except AttributeError:
            print('no .active() here — assuming an auto-starting extension (arrival day will tell)')
        try:
            bles.irq(link_handler)      # docs: connection status arrives through irq events
            _irq_wired = True           # issue 1: this lineage has no probe methods at all
        except AttributeError:
            print('no .irq() here — the guarded classic probes in connected_now() cover it')
    return bles


def advertising():
    """Advertise only while no link exists; the bond persists across range
    loss, so this only ever matters until the first pairing."""
    if bluetooth is None:
        # device answer 2026-11-06: this runtime has no BLE API, so it cannot control its
        # advertised name at all — the partner's decision is that the app scans by the stock
        # service UUID instead. The name bytes below stay for the lineage that can advertise.
        return
    # AD record: [length][type 0x09 complete local name][ascii name] — length
    # covers type+payload ('DigiBall' is 8 chars → 0x09). Exact bytes
    # reconfirmed on arrival day, checklist item 3.
    bles.gap_advertise(20000, adv_data=b'\x09\x09' + BALL_NAME_PREFIX.encode('ascii'))


def stop_advertising():
    # docs note: advertising must be stopped before registering services (the order the loop
    # keeps); docs read this pass (current core): "To stop advertising, set interval_us to
    # None" — so the doc value is the path this helper takes first. interval 0 is the classic
    # lineage's stop value, kept as the guarded fallback branch (checklist item 3 reconfirms
    # both on device; nothing here is claimed working until then).
    try:
        bles.gap_advertise(None)    # docs' stop value on the confirmed runtime
    except Exception as err:        # runtime that refuses the doc value — classic branch, never silent
        print('doc stop value refused: %r — taking the guarded classic interval 0' % err)
        bles.gap_advertise(0)       # the older lineage's habit, kept as the fallback


def connect_and_register():
    """Services may ONLY be registered while connected; the loop calls this
    only after connected_now() turned true, and advertising stops before it
    runs. Re-linking after range loss needs no new advertising (bond persists)."""
    global CHAR_FRAME_OUT, CHAR_MODE_IN
    # expectation for arrival-day checklist item 4: gatts_register_services must accept this two-characteristic combination
    registered = bles.gatts_register_services(build_services())
    CHAR_FRAME_OUT, CHAR_MODE_IN = registered[0]  # handles for the loop below


def connected_now():
    """Thin wrapper — the loop polls this on every transition check. The link
    truth comes from the irq handler above (the docs list no is_connected() and
    no active_connection() on the confirmed runtime); both classic probe names
    stay guarded fallbacks for the older lineage, and a runtime without either
    says so visibly once and stays honestly "not linked" instead of crashing."""
    global _probe_reported
    if bles is None:
        return False
    if _irq_wired:
        return link_state                 # kept by the documented handler (issue 1)
    try:
        try:
            return bool(bles.is_connected())           # older-lineage convenience method, if present
        except AttributeError:
            return bles.active_connection() is not None  # classic probe of that lineage
    except AttributeError:
        if not _probe_reported:
            _probe_reported = True
            print('no connection-probe API here — link stays "not linked" until arrival day')
        return False


# --- link signals, face language, streaming loop (was Task 7's glue) ---

def peep():
    """~0.25 s tone = ONE link established (never per throw — the anti-goal
    lives in the caller). audio + SoundEffect are documented on both port
    generations' docs; sound over a live BLE link is known-flaky on v3
    (adjacent MakeCode crash, pxt-calliope#374) — hence the guard: a failed
    signal shows visibly, never silently."""
    try:
        import audio
        effect = audio.SoundEffect(freq_start=1100, freq_end=1100, duration=250,
                                   waveform=audio.SoundEffect.WAVEFORM_SINE)
        audio.play(effect, wait=False)   # takeover fix F (audit 2026-11-08, partner go): play()'s
                                         # default is wait=True, and peep is the mode-signalling path —
                                         # five beats × ~250 ms of blocking is what made potato drift to
                                         # ~1.25 s. The flag is real on this fork at the pin
                                         # (src/codal_port/modaudio.c:214-230; it only blocks inside its
                                         # `if (wait)` loops at 174/199), so this returns right after
                                         # starting playback. [code][run at the pin, not on a board]
    except Exception as err:
        board.display.scroll('FAIL')            # visible fallback, never silent — scrolled letters only (the documented set quoted this pass is letters/digits/colon/newline; a period is not among them)
        print('link signal failed: %r — falling back to the face' % err)


def linked_face():
    # linked face: steady YES, unlike the searching pulse below
    board.display.show(board.Image.YES)


def searching_face(on):
    # slow HEART <-> HEART_SMALL pulse while looking for a link — same
    # language as the app's "ball linked / out of range" chips
    board.display.show(board.Image.HEART if on else board.Image.HEART_SMALL)


def link_lost_face():
    # link lost: face says NO briefly, then the searching pulse resumes
    board.display.show(board.Image.NO)
    board.sleep(900)


def flash_count(dropped):
    # out-of-range honesty rule: frames dropped past BUFFER_CAP get shown,
    # never silently swallowed — before the light language resumes.
    board.display.scroll(str(dropped))
    board.sleep(1200)


POTATO_DEADLINE_MS = 5000   # verbatim intent from the sketch ("5 Sekunden")
POTATO_INTERVAL_MS = 1000


def main():
    """One loop, all states. Every failure ends visibly on face and REPL."""
    global peep_fired            # module-level: link_live() reads this latch on the uart path
    if board is None:
        return                 # the import block above already printed why
    if bluetooth is not None and ensure_ble() is None:
        return                 # the GATT lineage: its own guard already printed why
    # The device in hand has no bluetooth module, so it runs on the runtime's own uart
    # (the design record's pre-decided fallback); ensure_ble() is the other lineage's path.
    detector = FlightDetector()
    buf = FrameBuffer()
    current_mode_byte = MODE_MAP['wurf']   # the app's default mode
    last_mode = None                      # what the phone asked for, verbatim
    was_connected = False
    pulse_on = False
    last_flip = 0
    round_start_ms = None
    beats_left = []
    held_until_ms = None
    round_done = False
    advertised_once = False               # takeover fix 2: boot counts as the first edge

    while True:
        try:
            now = ticks_ms()
            connected = link_live()

            # accelerometer first: detection runs ALWAYS — out of range the
            # ball still knows it was thrown; get_strength() is the documented
            # Pythagorean sum in milli-g, /1000.0 puts it in g for the knobs.
            gsum = board.accelerometer.get_strength() / 1000.0

            if connected and not was_connected:
                # The GATT lineage's order, kept whole (issue 9's ordering decision):
                # ONE peep per transition into linked — throws never reach this branch
                # ("no peep per throw" anti-goal). This runtime cannot take this branch's
                # registration steps, so its peep fires on the first command instead.
                if bluetooth is not None:
                    stop_advertising()          # docs order: stop before registering
                    connect_and_register()      # (re-)register while connected
                    peep()
                dropped = buf.dropped_count()
                for frame in buf.flush():   # buffered throws delivered first
                    tp_write(frame)         # the lineage's own way out (notify=True there)
                if dropped > 0:
                    flash_count(dropped)    # honesty rule before face language
                linked_face()
                light(LIGHT_LINKED)
            elif not connected and was_connected:
                link_lost_face()
                light(LIGHT_LOST)
                print('link lost — frames are held, nothing is dropped silently')

            link_edge = (connected != was_connected)   # takeover fix 2: the arm gets ONE call per
                                                       # edge, not one call per tick
            was_connected = connected

            if not connected:
                if now - last_flip > 800:   # slow pulse while searching
                    pulse_on = not pulse_on
                    last_flip = now
                searching_face(pulse_on)
                # takeover fix 2 (audit 2026-11-08, partner go): arm advertising once — at boot
                # and on each transition into disconnected — instead of every 50 ms tick. The shim
                # always passes sd_ble_gap_adv_set_configure non-NULL params and the SAME static
                # buffers, and the S140's own doc forbids exactly that while a set is running
                # (ble_gap.h: "When this function is used to update advertising data while
                # advertising, this parameter must be NULL"; and "It is invalid to provide the same
                # data buffers while advertising"). The generic layer caches nothing, so tick two
                # was a fresh call → error → -1 → OSError. The pulse and the face stay per-tick;
                # only the arm is edged. [code][run][inference]
                if link_edge or not advertised_once:
                    try:
                        advertising()
                    except Exception as err:
                        # the audit's fallback, taken in the program rather than in the fork: stop
                        # the set, then reconfigure once — BLEDevice::stopAdvertising() is live on
                        # this fork, and stop_advertising() is this file's own documented path.
                        print('advertise refused: %r — stopping the set, then one reconfigure' % err)
                        stop_advertising()
                        advertising()         # if this one refuses too, the loop's handler shows it
                    advertised_once = True

            # mode-in: the app writes encodeCounted(modeByte, 0) to switch modes.
            # Repeated reads of the same byte must NOT restart things (final-review fix I1).
            # The uart runtime must poll BEFORE anything is latched: on this runtime the
            # arrival of the first command IS the link event (the peep rule the partner set),
            # so the poll cannot sit behind a link test that only turns true after it.
            # The documented lineage keeps its original gate: poll only while linked.
            if connected or bluetooth is None:
                taken = parse_mode_writeback(tp_read())
                if taken is not None:                   # garbage stays unknown, never a guess
                    last_mode = taken
                    if bluetooth is None and not peep_fired:
                        peep()                          # the partner's rule for this runtime:
                        peep_fired = True               # one peep per boot, on the first command
                    if taken != current_mode_byte:      # act only on an actual change
                        current_mode_byte = taken
                        round_start_ms = now            # a fresh round starts with the mode
                        beats_left = []
                        held_until_ms = None
                        round_done = False

            # potato placeholder: peep pattern that STOPS at the deadline ("sound
            # ends → let go"). Back-burner placeholder — revisited with new rules
            # if the mode is dropped or replaced (design record).
            if current_mode_byte == MODE_MAP['potato'] and connected and round_start_ms is not None:
                elapsed = now - round_start_ms
                if not beats_left and elapsed <= POTATO_DEADLINE_MS and not round_done:
                    beats_left = potato_plan(POTATO_DEADLINE_MS, POTATO_INTERVAL_MS)
                while beats_left and elapsed >= beats_left[0]:
                    beats_left.pop(0)       # stops AT the deadline inclusive
                    peep()
                # hold = round start until the ball left the hand (a fresh spike
                # after 500 ms) or the deadline if it never did — exactly what
                # the mode counts ("shortest hold wins"). Placeholder semantics
                # like the mode itself.
                if held_until_ms is None and elapsed > 500 and gsum > TH_GSUM:
                    held_until_ms = now
                if elapsed >= POTATO_DEADLINE_MS and not round_done:
                    end_ms = held_until_ms if held_until_ms is not None else round_start_ms + POTATO_DEADLINE_MS
                    measurements = {'hold': hold_seconds(round_start_ms, end_ms)}
                    frame = encode_frame(current_mode_byte, measurements)
                    if connected:
                        tp_write(frame)         # the lineage's own way out
                    else:
                        buf.add(frame)
                    round_done = True   # exactly one result frame per round

            # detection: launch spike over TH_GSUM → in flight; the flight is closed by the NEXT
            # spike over TH_GSUM, and only after at least MIN_LOW samples below STOP_GSUM — the
            # run of free fall that licenses the impact (audit D, partner decision 2026-11-08; the
            # core owns this machine and its two PC pins were re-pinned with the decision).
            ev = detector.note(now, gsum)
            if ev is not None and ev['kind'] == 'catch':
                est = estimate_measurements(ev['flight_ms'])
                frame = encode_frame(current_mode_byte, est)
                if connected:
                    tp_write(frame)         # the lineage's own way out
                else:
                    buf.add(frame)  # out of range: the throw is held, count shown at link time
                    print('out of range: throw held (%d buffered)' % buf.pending_count())

            board.sleep(50)   # 20 Hz sampling rate — the sketch's intent, verbatim
        except Exception as err:
            # the loop must never die silently: show and say, then keep going
            board.display.scroll('ERR ' + str(err))
            print('loop error: %r' % err)
            board.sleep(2000)


main()
