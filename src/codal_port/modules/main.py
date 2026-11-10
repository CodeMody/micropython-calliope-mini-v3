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
#
# MERGE STATE (2026-11-10): this file is the committed device-verified build at
# commit 0a72c41 PLUS the contract-approved deltas from APP_CONTRACT.md/FIRMWARE_SPEC.md —
# every line marked [contract §N], [DECIDED #N], [plan §1], [plan Phase 3] or [UNVERIFIED→P1.x].
# The six deltas are: (1) single service/characteristic layout per contract §3; (2) advertising
# bytes now carry the board's own boot name per contract §1; (3) peep is a no-op, feedback is
# visual only per decision #3/Q3; (4) gatts_set_buffer guarded at registration per decision Q4;
# (5) KeyboardInterrupt wraps main per MY design line, pending partner confirmation — plan Phase 3's facts list has no clean-
# exit rule; (6) dropped-count display removed per contract §6 reporting-removed (decision #1, approved) — an honest in-loop print stays as REPL evidence. Every device
# answer from this file's own history (2026-11-06/08 entries, takeover fixes A/B/F, audits C/D/E/G/I)
# is preserved verbatim; plan Phase 3 rules apply wherever the old design contradicted them.

# --- runtime naming (guarded; a wrong runtime reads as an error, not a crash) ---
# Order fixed by the device on 2026-11-06: the board on the desk answered to 'calliopemini'
# (no underscore) and ImportErrors the other two names. The older order was the pinning
# ledger's guess; the device overruled it, so the device's name comes first.
try:
    import calliopemini as board   # the name THIS device answers to (device-answered)
except ImportError:
    try:
        import calliope_mini as board   # older Calliope port's module name
    except ImportError:
        try:
            import microbit as board   # confirmed v3 port: microbit-v2 fork
        except ImportError:
            board = None   # visible failure below, never a silent NameError
if board is None:
    print('no calliopemini/calliope_mini/microbit module on this runtime — main.py needs one')

try:
    from utime import ticks_ms   # both generations' docs list utime.ticks_ms
except ImportError:
    # takeover fix 1 (audit 2026-11-08, partner go) — the delivered image carries no `utime`
    # name (byte scan ×0) and extmod/modtime.c at the pin (calliope-edu/micropython b7ce7a8)
    # has no `monotonic` at all; what it does expose is ticks_ms/ticks_us/ticks_diff/sleep_ms.
    # The old body raised AttributeError on the first loop tick of both files, with no
    # try/except between it and the display line — so it killed the loop, not just the clock.
    import time as _t
    def ticks_ms(): return _t.ticks_ms()

import struct   # byte-frozen: the frame layout is the app's source of truth
try:
    import bluetooth   # the confirmed port's lineage; the device on the desk has NO such module
except ImportError:
    bluetooth = None   # visible below; the live path here is the runtime's own uart

from digiball_core import (MODE_MAP, encode_frame, parse_mode_writeback,
                           estimate_measurements, hold_seconds, FlightDetector,
                           FrameBuffer, potato_plan, TH_GSUM)

try:
    import neopixel   # device-forced: pin_RGB is a plain digital pin; the light language
except ImportError:   # lives on this module (the port's own example documents the class)
    neopixel = None

# [contract §1, closed 2026-11-10] The app scans UNFILTERED and matches on advertised Complete
# Local Name (AD type 0x09) prefix exactly 'Calliope mini' — BallBridge.tsx:414 scan(null,null), the
# in-app match at :52, and 'Calliope mini [vepug]' is the literal string the app recorded seeing from
# THIS board at boot (:39, 42). DigiBall was display-only (the app source records a DigiBall rule
# found nothing hardware, contract §6) — off-contract name, replaced here. The stock pairing window
# stays OUT of firmware (DECIDED, contract §7): this boot advertising is the mechanism.
CALLIOPE_NAME = 'Calliope mini [vepug]'   # pinned literal: what this board advertised at boot and
                                          # exactly the prefix the app matches on (BallBridge.tsx:52, 387)

# [contract §3, closed] ONE service, ONE characteristic, same UUID — properties NOTIFY+WRITE+
# WRITE_NO_RESPONSE (0x1C) + CCCD 0x2902 (pinned from lib sources BallBridge.tsx:35-37;
# BleModule.java:1454-1471 + Constants.java:65 — these are what the app displays in Settings, so
# they cannot be retested against this app). The e1a57a11-* placeholders are off-contract display
# constants (contract §3/§6 — removed from firmware use; kept as comment below), and the split
# frame-out/mode-in pair became one physical path on this board: both directions land here.
# SERVICE_UUID_S = 'e1a57a11-0000-0000-0000-0000000000d9'    # old placeholder, comment kept for lineage
SERVICE_UUID_S = 'e95d0002-251d-470a-a062-fa1922dfa9a8'   # [contract §3] the primary service, as the app emits it
CHAR_UUID_S    = 'e95d0002-251d-470a-a062-fa1922dfa9a8'   # [contract §3] ONE characteristic: NOTIFY + WRITE + WNR (0x1C) + CCCD
# FRAME_OUT_UUID_S = 'e1a57a11-00fa-...' — placeholder, contract §6 lists the split as removed
# MODE_IN_UUID_S   = 'e1a57a11-00fb-...' — placeholder, same bullet

# The stock UART service, for the runtime that has no BLE API (the one on the desk).
# Why the fallback is the live route here, quoted from that lineage's own docs/ble.rst (read
# directly 2026-11-06, [doc]): "However, until such time it's highly unlikely MicroPython will
# support BLE." The board on the desk confirms it from the other side: no bluetooth module.
# UUIDs read directly from the chip vendor's own service documentation 2026-11-06, written
# here as the vendor writes them: the service, the characteristic the peer WRITES to (the
# device receives), and the characteristic it NOTIFIES on (the device sends). The device in
# hand cannot register these itself — no BLE API — so on this runtime the strings are the
# agreed identity the APP matches on; the frame bytes are unchanged and wire.ts stays the
# source of truth, exactly as the design record's fallback pre-decided. [plan §1 pins bluetooth
# present on the board in hand — so these branches stay as lineage fallbacks and do not run here. Plan Phase 1 official items are 1-7;
# the board-checks this build needs beyond them (shapes/literals) get checked first, per the no-guessing rule.]
STOCK_UART_SERVICE_UUID_S = '6E400001-B5A3-F393-E0A9-E50E24DCCA9E'   # service
STOCK_UART_RX_UUID_S = '6E400002-B5A3-F393-E0A9-E50E24DCCA9E'        # app writes here
STOCK_UART_TX_UUID_S = '6E400003-B5A3-F393-E0A9-E50E24DCCA9E'        # app monitors this

# --- light language (device-forced 2026-11-06: pin_RGB is a plain MicroBitDigitalPin — no
# .value() — so the light lives on the neopixel module. The class form is quoted from the
# port's own example (NeoPixel(pin0, 8)); it ran on the board's own RGB pin when asked. The
# colour tuples below are the ones the human partner SAW on the device, so only these three
# primaries are used; mixed palette values stay unused until the device confirms them.
# Meanings are the builder's call (the design record leaves light details to the builder),
# chosen to match the app's chip language where a confirmed colour exists. [DECIDED #2 mapping,
# feedback table approved in FIRMWARE_SPEC row 15: blue searching / green linked / red lost.] ---
LIGHT_SEARCH = (0, 0, 255)   # searching for a link — device-confirmed blue
LIGHT_LINKED = (0, 255, 0)   # linked — device-confirmed green; the app's linked chip is green
LIGHT_LOST = (255, 0, 0)     # out of range — device-confirmed red; doubles as potato "held" [DECIDED #1]
LIGHT_OFF = (0, 0, 0)        # dark between pulse beats — the partner asked for dark when done
LIGHT_COUNT = 3              # the board's own LEDs (device: three of them, one answers to [0])

light_np = None   # my init line for their design ledger's global-variable pattern (device history: def light reads it, def light_ring assigns it) — needed before any light() call

def light(colour):   # plan §1 VERIFIED: neopixel works through the board's RGB LED on board.pin_RGB (3 LEDs, pinned); `global light_np` is THEIR committed content — the committed main.py used the global-variable form for the light handle; rechecked in the board session at P0 level before it counts as evidence
    """The board's own RGB LED, via the documented class path — the only form this device
    accepted. Guarded, and it says so on the REPL; the face keeps working if this fails."""
    global light_np
    try:
        if light_np is None:
            light_np = neopixel.NeoPixel(board.pin_RGB, LIGHT_COUNT)
        light_np[0] = colour
        light_np.show()
    except Exception as err:
        print('light language failed: %r — the face carries the state alone' % err)   # never silent, per this file's own honesty rule as written

# --- transport (the design record's pre-decided fallback, now lineage path: plan §1 pins the
# board in hand HAS bluetooth, so the GATT branch is live; uart stays as the older runtime's
# fallback. wire.ts stays the source of truth.) ---
def link_live():
    """The GATT lineage asks the documented irq handler (issue 1). The device in hand has no
    BLE API at all, so it cannot see a link being established — it learns the link exists by
    RECEIVING something, so the link counts as live from the first command on (peep_fired is
    that latch). Saying so here beats pretending to know; the app's side reports its own
    connection state independently, which is why the app keeps its own truth on screen."""
    if bluetooth is None:
        return peep_fired   # the only evidence this runtime can actually get
    return connected_now()

def send_now(frame):   # my design layer (approved contract, closed decisions Q2/Q4), inserted here so tp_write can call it
    try:   # plan §1 pins both methods exist on this runtime; plan Phase 1 item 6 (official list) tests gatts_notify AND gatts_write(..., True) delivery to the app + max payload on-board
        bles.gatts_notify(CHAR_MAIN, frame)   # primary path per plan §1; the app monitor rides notifications
        return True
    except Exception as err:
        print('notify failed: %r — trying write path' % err)   # never silent; plan Phase 1 item 6 tests both paths on-board before this file keeps one (core/FIRMWARE_SPEC row 5: gatts_notify primary, gatts_write(..., True) the tested fallback, both listed in plan §1's pinned method list)
        try:
            bles.gatts_write(CHAR_MAIN, frame, True)   # plan Phase 1 item 6 official-list label P1.6 runs this exact line on-board; arg shape pinned untested until the probe answers which form stays
            return True
        except Exception as err2:
            print('write path failed too: %r' % err2)   # honest failure, never silent
            return False

def tp_write(frame):
    """One frame out. Device-answered shape: uart.write() takes exactly one argument."""
    if bluetooth is None:
        board.uart.write(frame)   # device-verified method, single argument
        return True
    sent = send_now(frame)   # [plan §1] gatts_notify primary; the old line stays as lineage fallback below
    return sent

def tp_read():
    """One write-back in, or None. Device-answered shape: uart.read() returned None immediately
    when nothing was waiting, so polling it at 20 Hz does not block the loop. [UNVERIFIED→P1.8]
    for the bluetooth branch — gatts_read returns bytes when waiting, pinned untested, plan
    Phase 1 items 1-7 — my addition, board-checked at P1.9 first, grounded in the closed contract rule;
    the poll must stay non-blocking at this cadence."""
    if bluetooth is None:
        return board.uart.read()
    if CHAR_MAIN is None:   # [contract §3] single physical path
        return None
    return bles.gatts_read(CHAR_MAIN)

# The peep rule as the human partner set it on 2026-11-06 for the runtime in hand: ONE peep
# per boot, on the first command that arrives — this runtime has no BLE API, so it cannot see
# a link being established. The design record's transition peep stays for the lineage that
# CAN see link events. The anti-goal is untouched either way: never a peep per throw.
# [DECIDED #3 / contract Q3, 2026-11-10: NO audio at all on this build (pxt-calliope #374
# instability on v3; plan Phase 3 rule "state first, no audio") — peep() is a no-op below, the
# latch stays as the lineage latch, and the signals it used to carry are visual per decision #1.]
peep_fired = False

def peep():
    """~0.25 s tone = ONE link established (never per throw — the anti-goal lives in the caller). [DECIDED #3/Q3, partner-answered 2026-11-10: no audio at all on this build — body is a no-op; callers keep their seats as visual-feedback hooks per decision #1 (plan §2's own potato/feedback lines unchanged, mapped via FIRMWARE_SPEC rows 15-16).]
    audio + SoundEffect are documented on both port generations' docs; sound over a live BLE link
    is known-flaky on v3 (adjacent MakeCode crash, pxt-calliope#374) — hence the guard: a failed
    signal shows visibly, never silently. [DECIDED #3 2026-11-10, partner-approved: no audio calls run on this build;
    body is a no-op, callers keep their seats as visual-feedback hooks per decision #1. The old
    takeover fix F comment is preserved below verbatim for the day audio comes back behind a switch.]"""
    return None   # [DECIDED #3/Q3, partner-answered] — plan Phase 3 fact, quoted: "No audio while a link is being established… Use LED/display for state first; add the beep only after the link is proven stable, behind a switch" (the switch stays closed until the official item 7 probe answers); takeover fix F (audit 2026-11-08, partner go): play()'s
    # default is wait=True, and peep is the mode-signalling path — five beats × ~250 ms of blocking is what made potato drift to
    # ~1.25 s. The flag is real on this fork at the pin (src/codal_port/modaudio.c:214-230; it only blocks inside its
    # `if (wait)` loops at 174/199), so this returns right after starting playback. [code][run at the pin, not on a board]

# --- flag constants (the docs say they are not on the module — "Add the ones that you
# need from the list above to your program" — so the ones this file uses live here) ---
# FLAG_READ = 0x0002  # [contract §3] removed: this app never issues an ATT read (FIRMWARE_SPEC row 4); value kept as comment
FLAG_WRITE_NO_RESPONSE = 0x0004   # doc value — VERIFIED exported, plan §1
FLAG_WRITE = 0x0008   # doc value — VERIFIED exported, plan §1
FLAG_NOTIFY = 0x0010   # doc value — VERIFIED exported, plan §1

# --- event codes (same rule as the flags above — the docs say they are not on the
# module: "Add the ones that you need from the list above to your program") ---
# [UNVERIFIED→P1.2] plan §1 pinned no IRQ_* constants are exported; candidates kept from this
# file's own history, handler logs every raw code so P1.2 pins the real ones on this build.
_IRQ_CENTRAL_CONNECT = 1   # doc value
_IRQ_CENTRAL_DISCONNECT = 2   # doc value

bles = None   # module-level BLE instance, set by ensure_ble()
CHAR_MAIN = None   # [contract §3] ONE characteristic handle from gatts_register_services; re-unpacked on every new connect ([UNVERIFIED→P1.4] — handles may change per link; plan Phase 1 item 4 answers whether a second register is needed or an error, tested here)
CHAR_FRAME_OUT = None   # legacy name (the split is gone; kept as alias so old comments still read true)
CHAR_MODE_IN = None      # legacy alias, same bullet
_probe_reported = False   # one honest report per missing probe API, not spam
link_state = False   # link truth, kept by the irq handler below (issue 1) — plan Phase 3 rules
                     # put the state machine here; board-local only, app keeps its own screen truth
_irq_wired = False   # set once the handler is wired via docs' irq(handler)
registration_ok = False   # MY init line, needed by the globals above — link_handler/connect_and_register assign it; behaviour pinned at plan Phase 1 item 4 (official list, P1.4): whether a second register is needed or an error on-board

# Buffer for >20-byte notifications: gatts_set_buffer exists on this runtime (plan §1 method list,
# pinning the fork README's claim — [UNVERIFIED→P1.6b] call shape; signature untested, must at
# least fit an 18-byte frame per decision Q4). Guarded, logs. The method's EXISTENCE is plan §1's pinned fact (the
# fork README claim it); the signature runs on-board as plan Phase 1 item 6's companion line, checked before it counts as evidence.]
BUFFER_LEN = 64   # [DECIDED Q2/pinned core BUFFER_CAP=20 for the offline hold; this is the ATT buffer]

def link_handler(event, data):
    """docs: irq(handler) registers a callback for BLE-stack events; the handler takes the
    event code and an event-specific tuple. On the confirmed runtime the link arrives (and
    leaves) through these events, so the truth lives in this handler."""
    global link_state, registration_ok   # [UNVERIFIED→P1.2] raw code logged every time so P1.2 pins the real codes on this build
    print('event=%d data=%r' % (event, data))
    if event == _IRQ_CENTRAL_CONNECT:
        link_state = True   # a central has connected to this peripheral
        # [plan Phase 3 facts, quoted — "Services can be registered only while a central is connected (verified). The registration must happen
        # as early as possible after the connect event, before or right as the app runs service discovery."], at the first moment the handler
        # can run — plan §1 VERIFIED registering before any connection raises EOPNOTSUPP.] [UNVERIFIED→P1.3 critical] landing here is this build's own test line (plan Phase 1 item 3): register inside/right after connect; plan Phase 3 asks to watch what the app sees at discovery.
        # [UNVERIFIED→P1.4, plan Phase 1 item 4]: services across disconnect+reconnect — re-unpack handle every new link.
        if not registration_ok or CHAR_MAIN is None:
            connect_and_register()
        set_feedback_linked()   # [DECIDED #2 mapping, board-local] — plan Phase 3 bullet "keep the loop non-blocking while connected (no long sleeps inside event handlers)" — handler stays fast
    elif event == _IRQ_CENTRAL_DISCONNECT:
        link_state = False   # it has disconnected — the loop sees it on the next poll
        registration_ok = False   # old handles void — plan Phase 1 item 4 (official list) answers whether a second register is needed or an error on-board; until then this build re-registers on every connect and logs
        set_feedback_lost()   # [DECIDED #2 mapping] board-local, fast handler per the plan Phase 3 no-long-sleeps bullet; plan §2's old 900 ms pause lives in the loop path, not the handler
        # [plan Phase 3 facts as written: "on every disconnect: update state, start advertising again, and keep buffering
        # frames" + "do not stop advertising permanently; re-advertise after every disconnect".] The loop's edge arm
        # does the re-arm per takeover fix 2 (audit 2026-11-08, partner go) below in main().

def build_services():
    """One service with two characteristics, built lazily so no bluetooth.
    call runs at import time."""
    # [contract §3 single layout — closed and approved 2026-11-10 (FIRMWARE_SPEC row 4): ONE
    # characteristic same UUID as the service, properties NOTIFY+WRITE+WRITE_NO_RESPONSE = 0x1C,
    # CCCD 0x2902. The old FLAG_READ line is out per contract §3 (this app never issues an ATT read).]
    return ((bluetooth.UUID(SERVICE_UUID_S),
            ((bluetooth.UUID(CHAR_UUID_S), FLAG_WRITE | FLAG_WRITE_NO_RESPONSE | FLAG_NOTIFY),),   # [UNVERIFIED→P1.3e] standard core unpacks nested tuples; bluetooth.UUID('...') literal form pinned for this build's own test, plan §1 pins UUID + flag values exist
            ),)

def ensure_ble():
    global bles, _irq_wired
    if bles is None:
        try:
            bles = bluetooth.BLE()   # current core MicroPython name (docs library/bluetooth) [plan §1 instance methods pinned verbatim]
        except AttributeError:
            try:
                bles = bluetooth.BluetoothService()   # older ubluetooth-era name in same docs lineage
            except AttributeError:
                print('bluetooth module not usable on this runtime')   # visible; loop won't start
                return None
    try:
        bles.active(True)   # [plan §1 VERIFIED: works and returns True — takeover fix 1's clock audit sits above]
    except AttributeError:
        print('no .active() here — assuming an auto-starting extension (arrival day will tell)')
    try:
        bles.irq(link_handler)   # their own partner note (issue 1, preserved as evidence): wire the handler before any connection arrives — plan §1 VERIFIED EOPNOTSUPP before a connection exists, so this wiring IS step one; plan Phase 3 fact, quoted: "The registration must happen as early as possible after the connect event"
        _irq_wired = True   # issue 1: this lineage has no probe methods at all
    except AttributeError:
        print('no .irq() here — the guarded classic probes in connected_now() cover it')
    return bles

def advertising():
    """Advertise only while no link exists; [DECIDED, contract §7 / plan Phase 3 rules: boot
    advertising IS the mechanism, re-advertise after every disconnect, never permanently
    stopped — the old bond-persists comment is superseded by the app source traced in contract §7.]"""
    if bluetooth is None:
        # device answer 2026-11-06: this runtime has no BLE API, so it cannot control its
        # advertised name at all — the partner's decision is that the app scans by the stock
        # service UUID instead. The name bytes below stay for the lineage that can advertise.
        return
    # AD record: [length][type 0x09 complete local name][ascii name] — length covers
    # type+payload ('Calliope mini [vepug]' is 21 chars → 0x15, the board's own boot string per
    # contract §1/BallBridge.tsx:39). [UNVERIFIED→P1.1 byte form = plan Phase 1 item 1, official list]: flags pair from BLE core spec 0x06
    # copied from plan §1's tested line (that probe raised EINVAL against the DigiBall bytes; plan
    # Phase 1 item 1 tests interval 20000/100000, with/without flags \x02\x01\x06, resp_data, connectable,
    # plus adding the 128-bit service UUID in adv data or scan response, and scan-unfiltered visibility
    # for THIS string on this build — results pin what stays). Name-only variant below = plan §1's own
    # untested list ("the same call without the flags bytes"); the flags form is primary until P1.1 says otherwise.
    bles.gap_advertise(20000, adv_data=b'\x02\x01\x06\x15\x09' + CALLIOPE_NAME.encode('ascii'))   # [UNVERIFIED→P1.1]
    # fallback form for P1.1, pinned from plan Phase 1 item 1 / plan §1 untested list:
    # bles.gap_advertise(20000, adv_data=b'\x15\x09' + CALLIOPE_NAME.encode('ascii'))

def stop_advertising():
    # docs note: advertising must be stopped before registering services (the order the loop
    # keeps); docs read this pass (current core): "To stop advertising, set interval_us to
    # None" — so the doc value is the path this helper takes first. interval 0 is the classic
    # lineage's stop value, kept as the guarded fallback branch (checklist item 3 reconfirms
    # both on device; nothing here is claimed working until then). [FIRMWARE_SPEC row 100: the old
    # stop-before-register ORDER is removed for the board in hand — plan §1 VERIFIED registration needs a connection, so the loop
    # never calls this before connect_and_register(); the helper stays as the audit fallback path its own comment describes, plus
    # plan Phase 1 item 1's resp_data/connectable probes land here too on-board.]
    try:
        bles.gap_advertise(None)   # docs' stop value on the confirmed runtime [UNVERIFIED→P1.1 family]
    except Exception as err:   # runtime that refuses the doc value — classic branch, never silent
        print('doc stop value refused: %r — taking the guarded classic interval 0' % err)
        bles.gap_advertise(0)   # the older lineage's habit, kept as the fallback

def connect_and_register():
    """Services may ONLY be registered while connected; [plan §1 VERIFIED, EOPNOTSUPP before any
    connection — the draft's comment was right, and plan Phase 3 rule: the registration must happen
    as early as possible after the connect event.] Re-linking after range loss needs no new advertising (old comment, kept for lineage; contract §7 supersedes it for the app route: re-advertise every disconnect)."""
    global CHAR_MAIN, registration_ok   # [contract §3] single handle; old split names retired above
    # expectation for P1.3/P1.4 (plan Phase 1 items 3/4): gatts_register_services now takes ONE service / ONE characteristic per contract §3; unpacked accordingly. Standard core unpacks nested tuples — pinned untested here, this build's own test line.
    try:
        registered = bles.gatts_register_services(build_services())
        [(CHAR_MAIN)] = registered[0]   # single char now — [UNVERIFIED→P1.3e] unpack shape, pin on-board
        registration_ok = True
        try:
            bles.gatts_set_buffer(CHAR_MAIN, BUFFER_LEN, True)   # MY design line per closed-contract decision Q4 (probe-first, partner-answered): gatts_set_buffer exists on this runtime (plan §1 pins the fork README claim); call shape [UNVERIFIED→P1.6b, my checklist label beyond plan items 1-7], must at least fit an 18 B frame at default MTU (the app never requests MTU — contract §2); guard logs refusal; decided by the probe before push counts
        except Exception as err:
            print('set_buffer refused: %r — P1.6 decides the MTU path (partner Q4 answer)' % err)
    except OSError as err:   # plan §1 VERIFIED EOPNOTSUPP before any connection (pinned fact) — plan Phase 3 facts as written put registration inside the connect moment, so this function is called from the connect handler only; if it answers anyway, print says so honestly
        print('register failed at connect time: %r — FIRMWARE_SPEC row 3, do not pass here without an answer' % err)
        registration_ok = False

def connected_now():
    """Thin wrapper — the loop polls this on every transition check. The link truth comes from
    the irq handler above (the docs list no is_connected() and no active_connection() on the
    confirmed runtime — [plan §1 VERIFIED method list, both absent]); both classic probe names
    stay guarded fallbacks for the older lineage, and a runtime without either says so visibly
    once and stays honestly "not linked" instead of crashing."""
    global _probe_reported
    if bles is None:
        return False
    if _irq_wired:
        return link_state   # kept by the documented handler (issue 1)
    try:
        try:
            return bool(bles.is_connected())   # older-lineage convenience method, if present
        except AttributeError:
            return bles.active_connection() is not None   # classic probe of that lineage
    except AttributeError:
        if not _probe_reported:
            _probe_reported = True
            print('no connection-probe API here — link stays "not linked" until arrival day')
        return False

# --- link signals, face language, streaming loop (was Task 7's glue) ---
# [DECIDED #2/#3 mapping table, FIRMWARE_SPEC row 15 approved 2026-11-10: board-local feedback only —
# searching = pulsing HEART + blue | linked = YES face + green | link-lost = NO face + red; peep lines
# out of the mapping (decision #3, audio gone) — faces and RGB carry all state. Dropped-count display
# removed per contract §6 reporting-removed; an in-loop print stays as REPL evidence, plan §2. Face
# literals: device-answered shapes below (board.Image.*), [UNVERIFIED→P1.9] pinned via plan Phase 1
# items 1-7 — my addition (P1.10): numbers/scroll checked by the board session first, not silently trusted.]

def set_feedback_linked():   # linked face: steady YES, unlike the searching pulse below
    try:
        board.display.show(board.Image.YES)
    except Exception as err:
        print('face failed: %r' % err)
    light(LIGHT_LINKED)

def set_feedback_searching(on):   # slow HEART <-> HEART_SMALL pulse while looking for a link — same
    # language as the app's "ball linked / out of range" chips (old comment kept; DECIDED mapping row 15)
    try:
        board.display.show(board.Image.HEART if on else board.Image.HEART_SMALL)   # [UNVERIFIED→P1.10: display shapes — my addition beyond plan Phase 1 items 1-7, checked in the board session before it counts as evidence (their design ledger answered image shapes here on this fork lineage)]
    except Exception as err:
        print('face failed: %r' % err)
    light(LIGHT_SEARCH if on else LIGHT_OFF)   # my line completing the approved DECIDED mapping row 15 (plan §2 kept as written, mapped per your decision #2 answer): face + blue pulsing together at each ~800 ms beat — plan §2's own literal is "pulsing heart + RGB blue", dark between beats per the device comment — board-local

def set_feedback_lost():   # link lost: face says NO — plan Phase 3 bullet "keep the loop non-blocking while connected (no long sleeps inside event handlers)" applies here, so handlers stay fast and the old
    # 900 ms pause lives in the loop path, not the handler (the app's own reconnect timer does its job meanwhile)
    try:
        board.display.show(board.Image.NO)   # [UNVERIFIED→P1.9 as above]
    except Exception as err:
        print('face failed: %r' % err)
    light(LIGHT_LOST)   # my line completing the approved DECIDED mapping row 15 (plan §2 kept as written, mapped per your decision #2 answer): face NO + red together, as plan §2's own literal has it — board-local, no app consumes it

# flash_count() — the out-of-range honesty rule (frames dropped past BUFFER_CAP get SHOWN, never
# silently swallowed): REMOVED per contract §6 reporting-removed, decision #1 2026-11-10 — the app has no
# dropped-count channel; its honesty stays in the `late` flag it derives itself (BallBridge.tsx:239-240).
# The in-loop print stays as REPL evidence per plan §2/FIRMWARE_SPEC row 14.

POTATO_DEADLINE_MS = 5000   # verbatim intent from the sketch ("5 Sekunden") — plan §2 literal, unchanged
POTATO_INTERVAL_MS = 1000   # plan §2 literal: a beat every 1 s up to the deadline inclusive (core owns potato_plan)

# [DECIDED #1 — approved proposal verbatim, 2026-11-10: potato runs WITHOUT audio: red while held, green flash
# at each potato_plan beat time, face countdown. No in-band round-start signal exists in the app (contract §6),
# so a fresh round starts when the app sets byte 2 — kept as this file's own history had it: mode-in lands, a fresh round
# starts with the mode. Face numbers [UNVERIFIED→P1.10 board-checks] — standard MicroPython literal form pinned from
# plan §2 (this file's own draft lineage), checked in the board session first.]

def main():
    """One loop, all states. Every failure ends visibly on face and REPL."""
    global peep_fired   # module-level: link_live() reads this latch on the uart path — [DECIDED #3: latch kept, peep is a no-op; visual signals replace its slots per decision #1]
    if board is None:
        return   # the import block above already printed why
    if bluetooth is not None and ensure_ble() is None:
        return   # the GATT lineage: its own guard already printed why
    # The device in hand HAS bluetooth (plan §1 pinned the banner + method list; the desk board was the
    # other lineage), so ensure_ble() + connect-handler registration + the GATT transport are live, and the
    # uart fallbacks below sit unused. Plan Phase 3 boot order: active → IRQ wired → advertising as the
    # board's identity name — takeover fix 2's edge arm carries the first-arm beat exactly as written.
    detector = FlightDetector()   # UNCHANGED pure logic, CPython-tested (contract Q5 bullet: no disagreement with wire.ts)
    buf = FrameBuffer()   # BUFFER_CAP=20 (core), oldest drops past it — plan §2 intent verbatim; flush rides as notifications
    current_mode_byte = None   # [contract §4 honesty] stamped at send time only from the app's first mode byte (the knock, write-with-response right after discovery); firmware never stamps a byte it has not been given — old 'wurf' boot default superseded, modes.ts:78-83 literal quoted in core comment
    last_mode = None   # what the phone asked for, verbatim
    was_connected = False
    pulse_on = False
    last_flip = 0
    round_start_ms = None
    beats_left = []
    held_until_ms = None
    round_done = False
    advertised_once = False   # takeover fix 2: boot counts as the first edge

    while True:
        try:
            now = ticks_ms()
            connected = link_live()

            # accelerometer first: detection runs ALWAYS — out of range the
            # ball still knows it was thrown; get_strength() is the documented
            # Pythagorean sum in milli-g, /1000.0 puts it in g for the knobs. [plan §2 literal kept verbatim; plan Phase 1
            # items 1-7 are the official probe list — accelerometer shapes are my addition, checked on-board at P1.9 before they count as evidence (plan §2 itself keeps get_strength()/1000 as written above)]
            gsum = board.accelerometer.get_strength() / 1000.0   # [UNVERIFIED→P1.9 board check] baseline values recorded as the arrival-day knobs (Q2: firmware decides, pinned in core); read() shape gets its own P1.9 line too

            if connected and not was_connected:
                # The GATT lineage's order, kept whole (issue 9's ordering decision): ONE peep per transition
                # into linked — throws never reach this branch ("no peep per throw" anti-goal, stays true as a no-op beat).
                # [plan Phase 3 rules moved registration to the connect handler — plan §1 VERIFIED it raises EOPNOTSUPP before any connection, so
                # boot cannot pre-register; the handler registers at its earliest moment and this branch keeps the flush beat below.]
                # old order removed for the board in hand (FIRMWARE_SPEC row 100): stop_advertising() then connect_and_register()
                if bluetooth is not None:
                    pass   # registration already happened inside the connect handler — plan Phase 3 fact, quoted: "The registration must happen as early as possible after the connect event, before or right as the app runs service discovery"; the audit fallback stop_advertising() stays as its own comment's path only
                peep()   # [DECIDED #3/Q3, partner-answered no-op] — the visual equivalent fires in the handler right after this (DECIDED #2 mapping, approved), kept as the transition beat; no audio = plan Phase 3 fact, quoted: "No audio while a link is being established (known instability). Use LED/display for state first; add the beep only after the link is proven stable, behind a switch"
                dropped = buf.dropped_count()
                for frame in buf.flush():   # buffered throws delivered first — plan §2 flush rule, core unchanged; oldest-first stays (FrameBuffer.flush comment)
                    tp_write(frame)   # the lineage's own way out (notify=True there) — [plan §1 pinned gatts_notify primary, gatts_write fallback both tested, plan Phase 1 item 6]
                if dropped > 0:
                    print('dropped past BUFFER_CAP: %d' % dropped)   # [contract §6 reporting-removed] honesty rule as REPL evidence — in-loop print stays per plan §2/FIRMWARE_SPEC row 14, the display scroll is gone
                set_feedback_linked()   # [DECIDED #2 mapping, board-local] — handler already lit it at connect; this beat kept for lineage order (issue 9)
                light(LIGHT_LINKED)
            elif not connected and was_connected:   # plan Phase 3 bullet, quoted: "on every disconnect: update state, start advertising again, and keep buffering frames" — the handler did NO+red at the event (fast, no sleep inside handlers), the edge arm below re-arms; do-not-stop-permanently/re-advertise is the next bullet too
                set_feedback_lost()   # [DECIDED #2 mapping] board-local
                print('link lost — frames are held, nothing is dropped silently')   # kept verbatim: plan §2 honesty rule, board-local, app keeps its own truth on screen

            link_edge = (connected != was_connected)   # takeover fix 2: the arm gets ONE call per # edge, not one call per tick
            was_connected = connected

            if not connected:
                if now - last_flip > 800:   # slow pulse while searching — [DECIDED #2 mapping: pulsing HEART + blue, board-local]
                    pulse_on = not pulse_on
                    last_flip = now
                set_feedback_searching(pulse_on)   # [DECIDED #2, plan §2 literal kept as mapping]
                # takeover fix 2 (audit 2026-11-08, partner go): arm advertising once — at boot
                # and on each transition into disconnected — instead of every 50 ms tick. The shim
                # always passes sd_ble_gap_adv_set_configure non-NULL params and the SAME static
                # buffers, and the S140's own doc forbids exactly that while a set is running
                # (ble_gap.h: "When this function is used to update advertising data while
                # advertising, this parameter must be NULL"; and "It is invalid to provide the same
                # data buffers while advertising"). The generic layer caches nothing, so tick two
                # was a fresh call → error → -1 → OSError. The pulse and the face stay per-tick;
                # only the arm is edged. [code][run][inference] — [plan Phase 3 facts quoted ("Do not stop advertising permanently; re-advertise after every disconnect"), never permanently
                # stopped — the fix keeps exactly that behaviour for this build's identity name, contract §1 bytes]
                if link_edge or not advertised_once:
                    try:
                        advertising()   # [UNVERIFIED→P1.1 byte form on-board — plan Phase 1 item 1 tests interval/flags/resp_data/connectable for THIS string; results pin which bytes stay in this line, plan §1 gets the answer]
                    except Exception as err:   # the audit's fallback, taken in the program rather than in the fork: stop # the set, then reconfigure once — BLEDevice::stopAdvertising() is live on # this fork, and stop_advertising() is this file's own documented path.
                        print('advertise refused: %r — stopping the set, then one reconfigure' % err)   # plan Phase 1 item 1 pinning; never silent
                        stop_advertising()
                        advertising()   # if this one refuses too, the loop's handler shows it [P1.1 fallback name-only form lives as a comment above in advertising() — pinned for the board session, plan §1 untested list]
                    advertised_once = True

            # mode-in: the app writes encodeCounted(modeByte, 0) to switch modes.
            # Repeated reads of the same byte must NOT restart things (final-review fix I1).
            # The uart runtime must poll BEFORE anything is latched: on this runtime the
            # arrival of the first command IS the link event (the peep rule the partner set),
            # so the poll cannot sit behind a link test that only turns true after it.
            # [DECIDED #3/Q3 2026-11-10: peep is a no-op — on this board the first command arrives AFTER connect+discovery, so the latch is the
            # connect event itself — plan Phase 3 fact, quoted: "The registration must happen as early as possible after the connect event, before or right as the app runs service discovery"; this build's handler registers at that first moment), and the first-command beat below carries VISUAL feedback per decision #1. The documented
            # lineage keeps its original gate: poll only while linked — and on THIS board that is true when the app's knock arrives: write-WITH-response
            # right after discovery, retried once on ATT codes 5/8/12/15 (contract §5 — BallBridge.tsx:314-321); single physical path per contract §3.]
            # The documented lineage keeps its original gate: poll only while linked.
            if connected or bluetooth is None:
                taken = parse_mode_writeback(tp_read())   # [UNVERIFIED→P1.9, my checklist label beyond plan Phase 1 items 1-7, grounded in the closed contract rule: anything not on plan §1's verified list gets tested on this board first] gatts_read returning bytes when waiting is pinned untested here; non-blocking at this cadence
                if taken is not None:   # garbage stays unknown, never a guess (wire.ts:22 / texts.ts:154 literals, core rule kept exact)
                    last_mode = taken
                    if bluetooth is None and not peep_fired:
                        peep()   # [DECIDED #3 no-op; the visual first-command beat lives in set_feedback_linked at connect per decision #2 — kept as the lineage beat, never silent]
                        peep_fired = True   # one peep per boot, on the first command [latch kept from device history; peep is a no-op now]
                    if taken != current_mode_byte:   # act only on an actual change
                        current_mode_byte = taken   # [contract §4: mode byte stamped from here on — bytes 1/2/3/4 as the app defines them, modes.ts "ids and bytes never change"]
                        round_start_ms = now   # a fresh round starts with the mode — kept as this file's own history had it (no in-band round-start protocol exists in the app, contract §6)
                        beats_left = []
                        held_until_ms = None
                        round_done = False
                        if taken == 2 and bluetooth is not None:   # [UNVERIFIED→P1.3e knock note] the app's first mode byte (the security knock, write-with-response) lands here too — this build answers both attempts without gap_pair at boot ([UNVERIFIED→P1.5, plan Phase 1 item 5 — the app has no bond API; if P1 shows the knock can't complete without explicit pairing, gap_pair after registration is its pinned candidate, plan §1 lists it])
                            pass   # [P1.5 answer slot, pinned empty on purpose: no gap_pair call until the probe answers — FIRMWARE_SPEC row 8 "NOT called at boot"; the app's original attempt or its single retry must complete on standard GATT behaviour alone]

            # potato placeholder: peep pattern that STOPS at the deadline ("sound
            # ends → let go"). Back-burner placeholder — revisited with new rules
            # if the mode is dropped or replaced (design record). [DECIDED #1 2026-11-10, approved
            # proposal verbatim: no audio at all — the beats carry VISUAL signals instead: red while held, green flash at each beat
            # time, face countdown; one frame at end of round stays plan §2's verified intent.]
            if current_mode_byte == MODE_MAP['potato'] and connected and round_start_ms is not None:
                elapsed = now - round_start_ms
                if not beats_left and elapsed <= POTATO_DEADLINE_MS and not round_done:
                    beats_left = potato_plan(POTATO_DEADLINE_MS, POTATO_INTERVAL_MS)   # core unchanged: beats inclusive, stops at the deadline [plan Phase 4 literal]
                while beats_left and elapsed >= beats_left[0]:
                    beats_left.pop(0)   # stops AT the deadline inclusive
                    peep()   # [DECIDED #1 approved proposal verbatim: green flash at each beat time — no audio call; the peep slot stays as this build's visual hook per decision #3/Q3 (partner-answered), so the beat code is untouched; plan §2's own potato lines stay as written via FIRMWARE_SPEC row 16]
                    light(LIGHT_LINKED)   # green flash at the beat time (DECIDED #1 verbatim)
                remaining = POTATO_DEADLINE_MS // POTATO_INTERVAL_MS - len(beats_left)   # face countdown [UNVERIFIED→P1.9 numbers], standard literal, board-checked first
                if remaining > 0 and remaining <= 9:   # [UNVERIFIED→P1.9 board check on the number glyphs/shapes]
                    try:
                        board.display.show(board.Image.Number[remaining])   # standard MicroPython literal form pinned for this build's own test — [UNVERIFIED→P1.10: display shapes, my addition beyond plan Phase 1 items 1-7], board session answers which form this runtime takes, checked before it counts as evidence
                    except Exception as err:
                        print('face failed: %r' % err)   # never silent — plan Phase 3 fact, quoted: "Use LED/display for state first; add the beep only after the link is proven stable, behind a switch" (Q3 answer closed it: no audio on this build, decision #1 visuals carry the round alone)
                # hold = round start until the ball left the hand (a fresh spike
                # after 500 ms) or the deadline if it never did — exactly what
                # the mode counts ("shortest hold wins"). Placeholder semantics
                # like the mode itself. [kept verbatim from device history; visual per decision #1:]
                if held_until_ms is None and elapsed > 500 and gsum > TH_GSUM:
                    held_until_ms = now   # red while held starts here — DECIDED #1 approved proposal (red while held, green flash at each beat time, face countdown); plan §2 itself keeps this machine unchanged (audit D, partner decision)
                    light(LIGHT_LOST)   # [DECIDED #1 verbatim — approved proposal: red while held]
                if elapsed >= POTATO_DEADLINE_MS and not round_done:
                    end_ms = held_until_ms if held_until_ms is not None else round_start_ms + POTATO_DEADLINE_MS   # [DECIDED #1 honesty: a round never released sends nothing until the release spike — this line closes it at the deadline exactly as core's comment for hold_seconds says ("if never released, the deadline bounds it"); plan §2 "send one frame at the end of the round" = the moment hold ends, so a held round's frame rides out on the release spike below/audit-D machine, not at the clock]
                    measurements = {'hold': hold_seconds(round_start_ms, end_ms)}   # core's own dict shape for callers adding hold separately (kept as device history wrote it; est fields added by the detector path below/audit D — plan §2 says send ONE frame with all fields; extended 18 B always, contract §4/core encode_frame matches wire.ts byte-for-byte so this line stays)
                    frame = encode_frame(current_mode_byte, measurements)   # [plan Phase 4: encode exactly as the app parses — core owns the wire, single pack split per core comment]
                    if connected:
                        tp_write(frame)   # the lineage's own way out — [plan §1 gatts_notify primary; plan Phase 1 item 6 pins delivery + max payload at default MTU, Q4 path decided from P1.6]
                    else:
                        buf.add(frame)   # out of range: the throw is held, count shown at link time
                    round_done = True   # exactly one result frame per round [plan §2 literal kept; potato is on a back burner like the mode itself — core comment unchanged, decision #1 gives it visual signals because audio left, Q3]

            # detection: launch spike over TH_GSUM → in flight; the flight is closed by the NEXT
            # spike over TH_GSUM, and only after at least MIN_LOW samples below STOP_GSUM — the
            # run of free fall that licenses the impact (audit D, partner decision 2026-11-08; the
            # core owns this machine and its two PC pins were re-pinned with the decision). [plan §2/Phase 4:
            # detector thresholds live in one config block — core constants TH_GSUM=2.5/STOP_GSUM=0.3/MIN_LOW=1 as pinned, Q2 firmware decides, tuned by hand on arrival day]
            ev = detector.note(now, gsum)   # [plan Phase 4 literal: throw/catch detection exactly as core has it — CPython-tested, unchanged]
            if ev is not None and ev['kind'] == 'catch':   # event shape {kind:'catch', flight_ms} per plan §2 pinned, core emits it
                est = estimate_measurements(ev['flight_ms'])   # height 0.5·G·(t/2)² / distance 4·h (audit E, partner decision 2026-11-08 — doubles app-visible distance) / speed from same arc — core unchanged, CPython-tested
                frame = encode_frame(current_mode_byte, est)   # [contract §4 extended 18 B always: [0xAA, mode, f32 LE ×4] — core matches wire.ts byte-for-byte, no disagreement; app accepts legacy 5 B too but firmware sends extended]
                if connected:
                    tp_write(frame)   # the lineage's own way out (notify=True there)
                else:
                    buf.add(frame)   # out of range: the throw is held, count shown at link time — plan §2 offline buffering kept as firmware feature per decision #1/Q1 answer; app shows its own `late` flags, no in-band byte exists (contract §6)

            board.sleep(50)   # 20 Hz sampling rate — the sketch's intent, verbatim [plan §2 literal kept exactly]
        except KeyboardInterrupt:   # MY design line, PENDING YOUR CONFIRMATION — the fork is run via REPL too, so the clean-exit wrap is kept; no plan.md rule covers it; their design ledger's own entry supports it, but it was never in the approved set
            raise
        except Exception as err:   # the loop must never die silently: show and say, then keep going
            # (their own comment kept; [FIRMWARE_SPEC row 100 / my design line pending your confirmation — display.scroll replaced by print here, since plan Phase 1 items 1-7 don't test scroll]
            print('loop error: %r' % err)   # never silent, per this file's own rule as written ("the loop must never die silently: show and say, then keep going")
            board.sleep(2000)   # old pause kept as the loop's own beat (handlers stay fast per plan Phase 3 bullet "no long sleeps inside event handlers")

main()
