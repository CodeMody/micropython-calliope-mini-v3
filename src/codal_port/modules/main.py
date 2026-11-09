
try:
    import calliopemini as board
except ImportError:
    try:
        import calliope_mini as board
    except ImportError:
        try:
            import microbit as board
        except ImportError:
            board = None
if board is None:
    print('no calliopemini/calliope_mini/microbit module on this runtime — main.py needs one')

try:
    from utime import ticks_ms
except ImportError:
    import time as _t
    def ticks_ms(): return _t.ticks_ms()

import struct
try:
    import bluetooth
except ImportError:
    bluetooth = None
from digiball_core import (MODE_MAP, encode_frame, parse_mode_writeback,
                           estimate_measurements, hold_seconds, FlightDetector,
                           FrameBuffer, potato_plan, TH_GSUM)

try:
    import neopixel
except ImportError:
    neopixel = None

BALL_NAME_PREFIX = 'DigiBall'
SERVICE_UUID_S = 'e1a57a11-0000-0000-0000-0000000000d9'
FRAME_OUT_UUID_S = 'e1a57a11-00fa-0000-0000-0000000000d9'
MODE_IN_UUID_S = 'e1a57a11-00fb-0000-0000-0000000000d9'

STOCK_UART_SERVICE_UUID_S = '6E400001-B5A3-F393-E0A9-E50E24DCCA9E'
STOCK_UART_RX_UUID_S = '6E400002-B5A3-F393-E0A9-E50E24DCCA9E'
STOCK_UART_TX_UUID_S = '6E400003-B5A3-F393-E0A9-E50E24DCCA9E'

LIGHT_SEARCH = (0, 0, 255)
LIGHT_LINKED = (0, 255, 0)
LIGHT_LOST = (255, 0, 0)
LIGHT_OFF = (0, 0, 0)
LIGHT_COUNT = 3


_light_np = None


def light(colour):
    """The board's own RGB LED, via the documented class path — the only form this device
    accepted. Guarded, and it says so on the REPL; the face keeps working if this fails."""
    global _light_np
    try:
        if _light_np is None:
            _light_np = neopixel.NeoPixel(board.pin_RGB, LIGHT_COUNT)
        _light_np[0] = colour
        _light_np.show()
    except Exception as err:
        print('light language failed: %r — the face carries the state alone' % err)


def link_live():
    """The GATT lineage asks the documented irq handler (issue 1). The device in hand has no
    BLE API at all, so it cannot see a link being established — it learns the link exists by
    RECEIVING something, so the link counts as live from the first command on (peep_fired is
    that latch). Saying so here beats pretending to know; the app's side reports its own
    connection state independently, which is why the app keeps its own truth on screen."""
    if bluetooth is None:
        return peep_fired
    return connected_now()


def tp_write(frame):
    """One frame out. Device-answered shape: uart.write() takes exactly one argument."""
    if bluetooth is None:
        board.uart.write(frame)
    else:
        bles.gatts_write(CHAR_FRAME_OUT, frame, True)
    return True


def tp_read():
    """One write-back in, or None. Device-answered shape: uart.read() returned None
    immediately when nothing was waiting, so polling it at 20 Hz does not block the loop."""
    if bluetooth is None:
        return board.uart.read()
    if CHAR_MODE_IN is None:
        return None
    return bles.gatts_read(CHAR_MODE_IN)


peep_fired = False


FLAG_READ = 0x0002
FLAG_WRITE_NO_RESPONSE = 0x0004
FLAG_WRITE = 0x0008
FLAG_NOTIFY = 0x0010

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2

bles = None
CHAR_FRAME_OUT = None
CHAR_MODE_IN = None
_probe_reported = False
link_state = False
_irq_wired = False


def link_handler(event, data):
    """docs: irq(handler) registers a callback for BLE-stack events; the handler takes
    the event code and an event-specific tuple. On the confirmed runtime the link
    arrives (and leaves) through these events, so the truth lives in this handler."""
    global link_state
    if event == _IRQ_CENTRAL_CONNECT:
        link_state = True
    elif event == _IRQ_CENTRAL_DISCONNECT:
        link_state = False


def build_services():
    """One service with two characteristics, built lazily so no bluetooth.
    call runs at import time."""
    return ((bluetooth.UUID(SERVICE_UUID_S),
             ((bluetooth.UUID(FRAME_OUT_UUID_S),
               FLAG_READ | FLAG_NOTIFY),
              (bluetooth.UUID(MODE_IN_UUID_S),
               FLAG_WRITE | FLAG_WRITE_NO_RESPONSE)),
             ),)


def ensure_ble():
    global bles, _irq_wired
    if bles is None:
        try:
            bles = bluetooth.BLE()
        except AttributeError:
            try:
                bles = bluetooth.BluetoothService()
            except AttributeError:
                print('bluetooth module not usable on this runtime')
                return None
        try:
            bles.active(True)
        except AttributeError:
            print('no .active() here — assuming an auto-starting extension (arrival day will tell)')
        try:
            bles.irq(link_handler)
            _irq_wired = True
        except AttributeError:
            print('no .irq() here — the guarded classic probes in connected_now() cover it')
    return bles


def advertising():
    """Advertise only while no link exists; the bond persists across range
    loss, so this only ever matters until the first pairing."""
    if bluetooth is None:
        return
    bles.gap_advertise(20000, adv_data=b'\x02\x01\x06\x09\x09' + BALL_NAME_PREFIX.encode('ascii'))


def stop_advertising():
    try:
        bles.gap_advertise(None)
    except Exception as err:
        print('doc stop value refused: %r — taking the guarded classic interval 0' % err)
        bles.gap_advertise(0)


def connect_and_register():
    """Services may ONLY be registered while connected; the loop calls this
    only after connected_now() turned true, and advertising stops before it
    runs. Re-linking after range loss needs no new advertising (bond persists)."""
    global CHAR_FRAME_OUT, CHAR_MODE_IN
    registered = bles.gatts_register_services(build_services())
    CHAR_FRAME_OUT, CHAR_MODE_IN = registered[0]


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
        return link_state
    try:
        try:
            return bool(bles.is_connected())
        except AttributeError:
            return bles.active_connection() is not None
    except AttributeError:
        if not _probe_reported:
            _probe_reported = True
            print('no connection-probe API here — link stays "not linked" until arrival day')
        return False



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
        audio.play(effect, wait=False)
    except Exception as err:
        board.display.scroll('FAIL')
        print('link signal failed: %r — falling back to the face' % err)


def linked_face():
    board.display.show(board.Image.YES)


def searching_face(on):
    board.display.show(board.Image.HEART if on else board.Image.HEART_SMALL)


def link_lost_face():
    board.display.show(board.Image.NO)
    board.sleep(900)


def flash_count(dropped):
    board.display.scroll(str(dropped))
    board.sleep(1200)


POTATO_DEADLINE_MS = 5000
POTATO_INTERVAL_MS = 1000


def main():
    """One loop, all states. Every failure ends visibly on face and REPL."""
    global peep_fired
    if board is None:
        return
    if bluetooth is not None and ensure_ble() is None:
        return
    detector = FlightDetector()
    buf = FrameBuffer()
    current_mode_byte = MODE_MAP['wurf']
    last_mode = None
    was_connected = False
    pulse_on = False
    last_flip = 0
    round_start_ms = None
    beats_left = []
    held_until_ms = None
    round_done = False
    advertised_once = False

    while True:
        try:
            now = ticks_ms()
            connected = link_live()

            gsum = board.accelerometer.get_strength() / 1000.0

            if connected and not was_connected:
                if bluetooth is not None:
                    stop_advertising()
                    connect_and_register()
                    peep()
                dropped = buf.dropped_count()
                for frame in buf.flush():
                    tp_write(frame)
                if dropped > 0:
                    flash_count(dropped)
                linked_face()
                light(LIGHT_LINKED)
            elif not connected and was_connected:
                link_lost_face()
                light(LIGHT_LOST)
                print('link lost — frames are held, nothing is dropped silently')

            link_edge = (connected != was_connected)
            was_connected = connected

            if not connected:
                if now - last_flip > 800:
                    pulse_on = not pulse_on
                    last_flip = now
                searching_face(pulse_on)
                if link_edge or not advertised_once:
                    try:
                        advertising()
                    except Exception as err:
                        print('advertise refused: %r — stopping the set, then one reconfigure' % err)
                        stop_advertising()
                        advertising()
                    advertised_once = True

            if connected or bluetooth is None:
                taken = parse_mode_writeback(tp_read())
                if taken is not None:
                    last_mode = taken
                    if bluetooth is None and not peep_fired:
                        peep()
                        peep_fired = True
                    if taken != current_mode_byte:
                        current_mode_byte = taken
                        round_start_ms = now
                        beats_left = []
                        held_until_ms = None
                        round_done = False

            if current_mode_byte == MODE_MAP['potato'] and connected and round_start_ms is not None:
                elapsed = now - round_start_ms
                if not beats_left and elapsed <= POTATO_DEADLINE_MS and not round_done:
                    beats_left = potato_plan(POTATO_DEADLINE_MS, POTATO_INTERVAL_MS)
                while beats_left and elapsed >= beats_left[0]:
                    beats_left.pop(0)
                    peep()
                if held_until_ms is None and elapsed > 500 and gsum > TH_GSUM:
                    held_until_ms = now
                if elapsed >= POTATO_DEADLINE_MS and not round_done:
                    end_ms = held_until_ms if held_until_ms is not None else round_start_ms + POTATO_DEADLINE_MS
                    measurements = {'hold': hold_seconds(round_start_ms, end_ms)}
                    frame = encode_frame(current_mode_byte, measurements)
                    if connected:
                        tp_write(frame)
                    else:
                        buf.add(frame)
                    round_done = True

            ev = detector.note(now, gsum)
            if ev is not None and ev['kind'] == 'catch':
                est = estimate_measurements(ev['flight_ms'])
                frame = encode_frame(current_mode_byte, est)
                if connected:
                    tp_write(frame)
                else:
                    buf.add(frame)
                    print('out of range: throw held (%d buffered)' % buf.pending_count())

            board.sleep(50)
        except Exception as err:
            board.display.scroll('ERR ' + str(err))
            print('loop error: %r' % err)
            board.sleep(2000)


main()
