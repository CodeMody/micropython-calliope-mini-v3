/*
 * C-ABI bridge between src/codal_port/modbluetooth_glue.c (inside
 * libmicropython.a) and the CODAL-side shim src/codal_app/mpbluetooth_codal.cpp
 * (compiled into the MICROBIT target). Same pattern as microbithal.h; events
 * travel the other way through a registered function pointer, so neither
 * language's symbols cross directly and no MicroPython headers are needed on
 * the CODAL side.
 *
 * Added for DigiBall: this is what gives the program the bluetooth module.
 */
#ifndef MICROPY_INCLUDED_CODAL_APP_MPBLUETOOTH_CODAL_H
#define MICROPY_INCLUDED_CODAL_APP_MPBLUETOOTH_CODAL_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Event tags passed to the post callback. These mirror the peripheral-role
 * IRQ codes in py/modbluetooth.h (the firmware handler branches on exactly
 * these numbers): central-connect, central-disconnect, gatts-write,
 * mtu-exchanged. */
#define MPBT_CODAL_EVT_CONNECT        1
#define MPBT_CODAL_EVT_DISCONNECT     2
#define MPBT_CODAL_EVT_WRITE          3
#define MPBT_CODAL_EVT_MTU_EXCHANGED 21

/* Registered once by the glue at init; the shim calls it from CODAL message
 * bus dispatch (fiber context — the MicroBitUtilityService event->bus->fiber
 * pattern, which the current runtime already runs on this board). param carries
 * the value handle for WRITE, the new MTU for MTU_EXCHANGED, and 0 for
 * connect/disconnect (the shim remembers the peer — single connection by
 * design). */
typedef void (*mpbt_codal_post_fn)(int evt, uint16_t conn_handle, uint32_t param);
void mpbt_codal_set_post_cb(mpbt_codal_post_fn fn);

/* Lifecycle. start() constructs the service and its two characteristics in the
 * SoftDevice attribute table, exactly as MicroBitUtilityService's
 * createShared() pattern does at runtime (the SD is already up at this point).
 * stop() only clears state — the SD has no teardown. Both run from
 * MicroPython context. */
int  mpbt_codal_start(void);
void mpbt_codal_stop(void);
bool mpbt_codal_is_active(void);

/* GAP. Addresses are big-endian (6 bytes), per the modbluetooth.h convention;
 * the shim does the SD's little-endian reordering behind this door. The peer
 * query is valid while connected and still returns the last peer after a
 * disconnect. */
int  mpbt_codal_get_address(uint8_t *addr_type, uint8_t *addr_be);
int  mpbt_codal_set_address_mode(uint8_t mode);
int  mpbt_codal_peer(uint8_t *addr_type, uint8_t *addr_be);
int  mpbt_codal_name_get(const uint8_t **buf);
int  mpbt_codal_name_set(const uint8_t *buf, size_t len);

/* Advertising: the raw AD-structure bytes (the generic layer hands them
 * through) go to the SD with the legacy adv-data call the CODAL manager
 * already makes at runtime; start/stop then go through the manager, which is
 * the live pairing-mode path on this board. interval_us is advisory (the SD
 * advertises at its configured interval); connectable is honoured. */
int  mpbt_codal_adv_start(bool connectable, int32_t interval_us,
    const uint8_t *adv_data, size_t adv_len, const uint8_t *sr_data, size_t sr_len);
void mpbt_codal_adv_stop(void);

/* GATTS. value_handle is the SD attribute handle handed back from
 * mpbt_codal_lookup. Characteristic storage is the SD's own table (the
 * characteristics are created with p_init_value buffers, as the utility and
 * partial-flash services do). The read path copies into a shim-static buffer;
 * valid until the next read on the same index. send_update on write means: if
 * the peer enabled notifications, send one now (the python gatts_write(h, buf,
 * True) path the firmware uses for frames out). */
int  mpbt_codal_gatts_read(uint16_t value_handle, const uint8_t **value, size_t *value_len);
int  mpbt_codal_gatts_write(uint16_t value_handle, const uint8_t *value, size_t value_len, bool send_update);
int  mpbt_codal_gatts_notify_indicate(uint16_t conn_handle, uint16_t value_handle, int op, const uint8_t *value, size_t value_len);
int  mpbt_codal_gatts_set_buffer(uint16_t value_handle, size_t len, bool append);

/* Maps the design's 16-bit characteristic codes (0x00fa / 0x00fb on the base
 * UUID) to the SD handles minted at start. */
int  mpbt_codal_lookup(uint16_t uuid16, uint16_t *value_handle, uint16_t *cccd_handle);

/* Single connection. pair() authenticates with the CODAL manager's security
 * mode (2 = pairing required, no MITM); the firmware never calls it —
 * encryption on char access is what pairs the peer, through the manager's live
 * pairing path. MTU set stores only (the SD fixes it at init); get answers in
 * bytes. */
int  mpbt_codal_gap_disconnect(uint16_t conn_handle);
int  mpbt_codal_get_preferred_mtu(void);
int  mpbt_codal_set_preferred_mtu(uint16_t mtu);
int  mpbt_codal_gap_pair(uint16_t conn_handle);
int  mpbt_codal_gap_passkey(uint16_t conn_handle, uint8_t action, uint32_t passkey);

#ifdef __cplusplus
}
#endif

#endif /* MICROPY_INCLUDED_CODAL_APP_MPBLUETOOTH_CODAL_H */
