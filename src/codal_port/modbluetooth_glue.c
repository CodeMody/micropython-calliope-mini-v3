/*
 * Port-side implementation of the modbluetooth port API
 * (lib/micropython/extmod/modbluetooth.h) for the CODAL shim on the
 * Calliope mini / nRF52833 build, added for DigiBall. The NimBLE
 * implementation file is not compiled in this project: this file plus
 * extmod/modbluetooth.c are the whole bluetooth backend. Every real action
 * goes through the frozen C-ABI bridge (../codal_app/mpbluetooth_codal.h,
 * implemented in mpbluetooth_codal.cpp); this file keeps only the few config
 * values the bridge has no door for.
 *
 * Return convention (extmod/modbluetooth.h: "Any method returning an int
 * returns errno on failure, otherwise zero"; modbluetooth.c turns the errno
 * into OSError(err) and reads MP_ENOTCONN from gap_disconnect as a plain
 * False): the bridge's doors return 0 on success and -1 otherwise (the one
 * exception is mpbt_codal_get_preferred_mtu, which returns the MTU value
 * itself), so each call site below maps a negative result to the errno that
 * call site means.
 *
 * Events: MICROPY_PY_BLUETOOTH_USE_SYNC_EVENTS=1, so the direct call from the
 * post callback into modbluetooth.c's on_* helpers is the contract. The shim
 * fires the callback from CODAL message-bus dispatch (fiber context), which
 * python event handlers may run in directly on this runtime.
 */

#include "py/mpconfig.h"
#include "py/mperrno.h"
#include "py/runtime.h"

#include "extmod/modbluetooth.h"
#include "mpbluetooth_codal.h"

// ---------------------------------------------------------------------------
// The one fixed service the program registers.
//
// Written in the GENERIC layer's byte order: mp_obj_bluetooth_uuid_t.data[]
// is little-endian against the printed string (modbluetooth.c's UUID
// constructor fills data[] from the string's last hex pair down to the
// first), so data[i] is string byte 15-i. The design table:
//   service "e1a57a11-0000-0000-0000-0000000000d9" (the registered base)
//   char    "e1a57a11-00fa-0000-0000-0000000000d9"  READ | NOTIFY          (frame out)
//   char    "e1a57a11-00fb-0000-0000-0000000000d9"  WRITE_NO_RESPONSE | WRITE (mode in)
// ---------------------------------------------------------------------------

#define DIGIBALL_CHAR_OUT_UUID16 0x00fa
#define DIGIBALL_CHAR_IN_UUID16  0x00fb

// The base, i.e. the service as python submits it (type 128 == this array).
static const uint8_t digiball_service_uuid[MP_BLUETOOTH_UUID_TYPE_128] = {
    0xd9, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x11, 0x7a, 0xa5, 0xe1,
};

// Bridge -1 -> the errno this call site means. The shim's -1 carries no
// detail, so callers document their one meaning for it.
static int mpbt_err(int ret) {
    return ret < 0 ? MP_EINVAL : 0;
}

static bool mpbt_uuid128_eq(const uint8_t *a, const uint8_t *b) {
    for (int i = 0; i < MP_BLUETOOTH_UUID_TYPE_128; ++i) {
        if (a[i] != b[i]) {
            return false;
        }
    }
    return true;
}

// Characteristic UUID -> the row's 16-bit slot code (DIGIBALL_CHAR_*_UUID16),
// or -1 when the UUID names neither of the two fixed characteristics.
// All three python spellings of a slot are accepted:
//   - the plain 16-bit form (UUID(0x00fa), data[0] = value low byte as the
//     generic layer stores it),
//   - the field-2 composition the design strings put on record
//     ("e1a57a11-00fa-0000-...": data[10] = slot low, data[11] = slot high),
//   - the composition the SoftDevice rule actually makes for a vendor base
//     (the rule the utility service proves live: e95d0000-... + 0x0001 =
//     e95d0001-...; 16-bit code at string bytes 2-3, so here data[12] = slot
//     low, data[13] = slot high).
// Anything else is outside the table and is rejected by the callers.
static int mpbt_char_slot(const mp_obj_bluetooth_uuid_t *uuid) {
    if (uuid->type == MP_BLUETOOTH_UUID_TYPE_16) {
        uint16_t code = (uint16_t)(uuid->data[0] | (uuid->data[1] << 8));
        if (code == DIGIBALL_CHAR_OUT_UUID16 || code == DIGIBALL_CHAR_IN_UUID16) {
            return (int)code;
        }
        return -1;
    }
    if (uuid->type == MP_BLUETOOTH_UUID_TYPE_128) {
        uint16_t code_field2 = (uint16_t)(uuid->data[10] | (uuid->data[11] << 8));
        uint16_t code_sd = (uint16_t)(uuid->data[12] | (uuid->data[13] << 8));
        uint16_t code;
        uint8_t match[MP_BLUETOOTH_UUID_TYPE_128];

        if (code_field2 == DIGIBALL_CHAR_OUT_UUID16 || code_field2 == DIGIBALL_CHAR_IN_UUID16) {
            code = code_field2;
            for (int i = 0; i < MP_BLUETOOTH_UUID_TYPE_128; ++i) {
                match[i] = digiball_service_uuid[i];
            }
            match[10] = (uint8_t)(code & 0xff);
            match[11] = (uint8_t)(code >> 8);
        } else if (code_sd == DIGIBALL_CHAR_OUT_UUID16 || code_sd == DIGIBALL_CHAR_IN_UUID16) {
            code = code_sd;
            for (int i = 0; i < MP_BLUETOOTH_UUID_TYPE_128; ++i) {
                match[i] = digiball_service_uuid[i];
            }
            match[12] = (uint8_t)(code & 0xff);
            match[13] = (uint8_t)(code >> 8);
        } else {
            return -1;
        }
        return mpbt_uuid128_eq(uuid->data, match) ? (int)code : -1;
    }
    return -1;
}

// ---------------------------------------------------------------------------
// Event post callback (registered once at init; the shim calls it from
// message-bus dispatch). The bridge's event tags mirror the peripheral-role
// IRQ codes, so they line up with the generic layer's event numbers.
// ---------------------------------------------------------------------------

static void mpbt_post_cb(int evt, uint16_t conn_handle, uint32_t param) {
    switch (evt) {
        case MPBT_CODAL_EVT_CONNECT:
        case MPBT_CODAL_EVT_DISCONNECT: {
            // The shim remembers the one peer (single connection by design)
            // and still answers after a disconnect, so both edges get the
            // peer; a failure (empty shim state) leaves the zero-filled
            // defaults. param is 0 on these edges.
            uint8_t addr_type = 0;
            uint8_t addr[6] = {0, 0, 0, 0, 0, 0};
            (void)mpbt_codal_peer(&addr_type, addr);
            mp_bluetooth_gap_on_connected_disconnected(
                evt == MPBT_CODAL_EVT_CONNECT ? MP_BLUETOOTH_IRQ_CENTRAL_CONNECT
                                              : MP_BLUETOOTH_IRQ_CENTRAL_DISCONNECT,
                conn_handle, addr_type, addr);
            break;
        }
        case MPBT_CODAL_EVT_WRITE:
            // param carries the value handle the write landed on.
            mp_bluetooth_gatts_on_write(conn_handle, (uint16_t)param);
            break;
        case MPBT_CODAL_EVT_MTU_EXCHANGED:
            // param carries the agreed MTU (the shim replied with one).
            mp_bluetooth_gatts_on_mtu_exchanged(conn_handle, (uint16_t)param);
            break;
        default:
            // Unknown tag: nothing the generic layer can receive.
            break;
    }
}

// ---------------------------------------------------------------------------
// Port API: general
// ---------------------------------------------------------------------------

int mp_bluetooth_init(void) {
    // The SD attribute table was built by the shim at start (the createShared
    // pattern, service plus the two characteristics); python's registration
    // only validates against the table and hands out the minted handles.
    int err = mpbt_codal_start();
    if (err != 0) {
        return MP_ENOMEM;   // the shim's -1 here means its service allocation failed
    }
    mpbt_codal_set_post_cb(mpbt_post_cb);
    return 0;
}

void mp_bluetooth_deinit(void) {
    // mpbt_codal_stop() is a no-op when not started; the SD has no teardown.
    mpbt_codal_stop();
}

bool mp_bluetooth_is_active(void) {
    return mpbt_codal_is_active();
}

void mp_bluetooth_get_current_address(uint8_t *addr_type, uint8_t *addr) {
    // The bridge hands the local address back big-endian, per the
    // modbluetooth.h convention, doing the SD's little-endian reorder. Zero
    // first so a failure (never seen after a working init) yields zeros, not
    // caller garbage.
    *addr_type = 0;
    addr[0] = addr[1] = addr[2] = addr[3] = addr[4] = addr[5] = 0;
    (void)mpbt_codal_get_address(addr_type, addr);
}

// Config values with no bridge door (the CODAL side keeps none — its storage
// was deliberately deleted): python's runtime values are set-and-remembered
// here for this build. Nothing reads or applies them; the peripheral's
// address mode and pairing behaviour are what the CODAL manager's GAP /
// security setup fixed. External linkage, not static, since nothing reads
// them back — a written-only static has no reason to trip an unused gripe.
uint8_t mpbt_cfg_addr_mode;        // MP_BLUETOOTH_ADDRESS_MODE_*
bool mpbt_cfg_bonding;             // set_bonding
bool mpbt_cfg_mitm_protection;     // set_mitm_protection
bool mpbt_cfg_le_secure;           // set_le_secure
uint8_t mpbt_cfg_io_capability;    // MP_BLUETOOTH_IO_CAPABILITY_*

void mp_bluetooth_set_address_mode(uint8_t addr_mode) {
    // No bridge call: the local address is fixed by the CODAL manager's GAP
    // setup and mpbt_codal_set_address_mode is a stub, so python's value is
    // set-and-remembered only.
    mpbt_cfg_addr_mode = addr_mode;
}

void mp_bluetooth_set_bonding(bool enabled) {
    mpbt_cfg_bonding = enabled;
}

void mp_bluetooth_set_mitm_protection(bool enabled) {
    mpbt_cfg_mitm_protection = enabled;
}

void mp_bluetooth_set_le_secure(bool enabled) {
    mpbt_cfg_le_secure = enabled;
}

void mp_bluetooth_set_io_capability(uint8_t capability) {
    mpbt_cfg_io_capability = capability;
}

// ---------------------------------------------------------------------------
// Port API: GAP
// ---------------------------------------------------------------------------

size_t mp_bluetooth_gap_get_device_name(const uint8_t **buf) {
    // The shim answers from its own static storage; the length comes back as
    // the return value (a negative is contractual only — it cannot fail, and
    // keeps the empty-name defaults).
    const uint8_t *name = (const uint8_t *)"";
    int len = mpbt_codal_name_get(&name);
    *buf = name;
    return len < 0 ? 0 : (size_t)len;
}

int mp_bluetooth_gap_set_device_name(const uint8_t *buf, size_t len) {
    return mpbt_err(mpbt_codal_name_set(buf, len));     // over-cap name -> EINVAL
}

int mp_bluetooth_gap_advertise_start(bool connectable, int32_t interval_us, const uint8_t *adv_data, size_t adv_data_len, const uint8_t *sr_data, size_t sr_data_len) {
    // interval_us is advisory (the SD advertises at its configured interval);
    // connectable is honoured; payload bytes are copied by the bridge.
    return mpbt_err(mpbt_codal_adv_start(connectable, interval_us, adv_data, adv_data_len, sr_data, sr_data_len));
}

void mp_bluetooth_gap_advertise_stop(void) {
    mpbt_codal_adv_stop();      // no-op when already stopped, per the bridge
}

int mp_bluetooth_gap_disconnect(uint16_t conn_handle) {
    // modbluetooth.c special-cases MP_ENOTCONN into False instead of an
    // exception; the bridge's generic -1 here has no other meaning than a
    // missing connection.
    return mpbt_codal_gap_disconnect(conn_handle) != 0 ? MP_ENOTCONN : 0;
}

int mp_bluetooth_gap_pair(uint16_t conn_handle) {
    return mpbt_err(mpbt_codal_gap_pair(conn_handle));
}

int mp_bluetooth_gap_passkey(uint16_t conn_handle, uint8_t action, mp_int_t passkey) {
    // The bridge takes the passkey as uint32_t (python passkeys are 0..999999).
    return mpbt_err(mpbt_codal_gap_passkey(conn_handle, action, (uint32_t)passkey));
}

// ---------------------------------------------------------------------------
// Port API: GATT server.
//
// Attribute storage lives in the SD attribute table (the shim's
// p_init_value buffers) — no generic-side gatts db is kept for this service.
// modbluetooth.c's registration flow does not drive the db_* helpers itself
// and the glue must not double-manage the values, so gatts_read/gatts_write
// go straight to the bridge; the read pointer contract (shim-static buffer,
// valid until the next read) is the bridge header's job.
// ---------------------------------------------------------------------------

int mp_bluetooth_gatts_register_service_begin(bool append) {
    // Nothing to begin: the service already exists in the SD from
    // mp_bluetooth_init. append mode is outside this build's usage (python
    // passes False).
    (void)append;
    return 0;
}

int mp_bluetooth_gatts_register_service(mp_obj_bluetooth_uuid_t *service_uuid, mp_obj_bluetooth_uuid_t **characteristic_uuids, uint16_t *characteristic_flags, mp_obj_bluetooth_uuid_t **descriptor_uuids, uint16_t *descriptor_flags, uint8_t *num_descriptors, uint16_t *handles, size_t num_characteristics) {
    // The two fixed characteristics carry no descriptors.
    (void)descriptor_uuids;
    (void)descriptor_flags;

    // Anything but the one designed table is rejected before the bridge;
    // MP_EOPNOTSUPP is what python sees for an out-of-row UUID/flag.
    if (service_uuid->type != MP_BLUETOOTH_UUID_TYPE_128 ||
            !mpbt_uuid128_eq(service_uuid->data, digiball_service_uuid)) {
        return MP_EOPNOTSUPP;
    }
    if (num_characteristics != 2) {
        return MP_EOPNOTSUPP;
    }

    // Handles arrive pre-initialised to 0xffff by the generic layer and go
    // back to python as the value handles, one per characteristic, in the
    // order python listed the rows.
    for (size_t i = 0; i < num_characteristics; ++i) {
        int code = mpbt_char_slot(characteristic_uuids[i]);
        uint16_t flags;
        if (code == DIGIBALL_CHAR_OUT_UUID16) {
            flags = MP_BLUETOOTH_CHARACTERISTIC_FLAG_READ |
                    MP_BLUETOOTH_CHARACTERISTIC_FLAG_NOTIFY;
        } else if (code == DIGIBALL_CHAR_IN_UUID16) {
            flags = MP_BLUETOOTH_CHARACTERISTIC_FLAG_WRITE |
                    MP_BLUETOOTH_CHARACTERISTIC_FLAG_WRITE_NO_RESPONSE;
        } else {
            return MP_EOPNOTSUPP;
        }
        if (characteristic_flags[i] != flags || num_descriptors[i] != 0) {
            return MP_EOPNOTSUPP;
        }
        uint16_t value_handle, cccd_handle;
        if (mpbt_codal_lookup((uint16_t)code, &value_handle, &cccd_handle) != 0) {
            // The shim has no such attribute, or was never started.
            return MP_EOPNOTSUPP;
        }
        handles[i] = value_handle;      // python only ever sees this handle
        (void)cccd_handle;              // the SD owns the CCCD; no port duty needs it
    }
    if (handles[0] == handles[1]) {
        // Both rows matched but decoded to the same slot: outside the table.
        return MP_EOPNOTSUPP;
    }
    return 0;
}

int mp_bluetooth_gatts_register_service_end(void) {
    // Nothing queues; the handles were already resolved per service above
    // (the SD minted them at init, like the runtime services).
    return 0;
}

int mp_bluetooth_gatts_read(uint16_t value_handle, const uint8_t **value, size_t *value_len) {
    return mpbt_err(mpbt_codal_gatts_read(value_handle, value, value_len));
}

int mp_bluetooth_gatts_write(uint16_t value_handle, const uint8_t *value, size_t value_len, bool send_update) {
    // send_update on this build: if the peer enabled notifications, the shim
    // sends one now (python's gatts_write(h, frame, True) frames-out path).
    return mpbt_err(mpbt_codal_gatts_write(value_handle, value, value_len, send_update));
}

int mp_bluetooth_gatts_notify_indicate(uint16_t conn_handle, uint16_t value_handle, int gatts_op, const uint8_t *value, size_t value_len) {
    return mpbt_err(mpbt_codal_gatts_notify_indicate(conn_handle, value_handle, gatts_op, value, value_len));
}

int mp_bluetooth_gatts_set_buffer(uint16_t value_handle, size_t len, bool append) {
    // Storage size is fixed by the SD attribute (64 bytes); the bridge only
    // checks len against it. Append mode does not exist on this path.
    return mpbt_err(mpbt_codal_gatts_set_buffer(value_handle, len, append));
}

// ---------------------------------------------------------------------------
// Port API: MTU
// ---------------------------------------------------------------------------

int mp_bluetooth_get_preferred_mtu(void) {
    // Value, not an error code: the SD fixes the MTU at init and the shim
    // answers with its stored preference (23 unless python set one).
    return mpbt_codal_get_preferred_mtu();
}

int mp_bluetooth_set_preferred_mtu(uint16_t mtu) {
    return mpbt_err(mpbt_codal_set_preferred_mtu(mtu));     // out of range -> EINVAL
}

// ---------------------------------------------------------------------------
// Port API: never reachable under this build's defines, but defined so any
// re-enable of MICROPY_PY_BLUETOOTH_ENABLE_CENTRAL_MODE, _GATT_CLIENT,
// _L2CAP_CHANNELS or _HCI_CMD links. Each returns the errno the generic
// layer will raise at python.
// ---------------------------------------------------------------------------

int mp_bluetooth_gap_scan_start(int32_t duration_ms, int32_t interval_us, int32_t window_us, bool active_scan) {
    (void)duration_ms;
    (void)interval_us;
    (void)window_us;
    (void)active_scan;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_gap_scan_stop(void) {
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_gap_peripheral_connect(uint8_t addr_type, const uint8_t *addr, int32_t duration_ms, int32_t min_conn_interval_us, int32_t max_conn_interval_us) {
    (void)addr_type;
    (void)addr;
    (void)duration_ms;
    (void)min_conn_interval_us;
    (void)max_conn_interval_us;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_gap_peripheral_connect_cancel(void) {
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_gattc_discover_primary_services(uint16_t conn_handle, const mp_obj_bluetooth_uuid_t *uuid) {
    (void)conn_handle;
    (void)uuid;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_gattc_discover_characteristics(uint16_t conn_handle, uint16_t start_handle, uint16_t end_handle, const mp_obj_bluetooth_uuid_t *uuid) {
    (void)conn_handle;
    (void)start_handle;
    (void)end_handle;
    (void)uuid;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_gattc_discover_descriptors(uint16_t conn_handle, uint16_t start_handle, uint16_t end_handle) {
    (void)conn_handle;
    (void)start_handle;
    (void)end_handle;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_gattc_read(uint16_t conn_handle, uint16_t value_handle) {
    (void)conn_handle;
    (void)value_handle;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_gattc_write(uint16_t conn_handle, uint16_t value_handle, const uint8_t *value, size_t value_len, unsigned int mode) {
    (void)conn_handle;
    (void)value_handle;
    (void)value;
    (void)value_len;
    (void)mode;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_gattc_exchange_mtu(uint16_t conn_handle) {
    (void)conn_handle;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_l2cap_listen(uint16_t psm, uint16_t mtu) {
    (void)psm;
    (void)mtu;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_l2cap_connect(uint16_t conn_handle, uint16_t psm, uint16_t mtu) {
    (void)conn_handle;
    (void)psm;
    (void)mtu;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_l2cap_disconnect(uint16_t conn_handle, uint16_t cid) {
    (void)conn_handle;
    (void)cid;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_l2cap_send(uint16_t conn_handle, uint16_t cid, const uint8_t *buf, size_t len, bool *stalled) {
    (void)conn_handle;
    (void)cid;
    (void)buf;
    (void)len;
    (void)stalled;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_l2cap_recvinto(uint16_t conn_handle, uint16_t cid, uint8_t *buf, size_t *len) {
    (void)conn_handle;
    (void)cid;
    (void)buf;
    (void)len;
    return MP_EOPNOTSUPP;
}

int mp_bluetooth_hci_cmd(uint16_t ogf, uint16_t ocf, const uint8_t *req, size_t req_len, uint8_t *resp, size_t resp_len, uint8_t *status) {
    (void)ogf;
    (void)ocf;
    (void)req;
    (void)req_len;
    (void)resp;
    (void)resp_len;
    (void)status;
    return MP_EOPNOTSUPP;
}
