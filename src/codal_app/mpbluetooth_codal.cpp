/*
 * CODAL-side shim for the MicroPython bluetooth module (added for DigiBall).
 * Implements the C-ABI bridge declared in mpbluetooth_codal.h; the C side
 * (src/codal_port/modbluetooth_glue.c) is the only caller.
 *
 * Every mechanism here is one the current runtime already proves on this
 * board: service construction at runtime via the createShared pattern
 * (RegisterBaseUUID + CreateService + CreateCharacteristic), writes arriving
 * at the service with data, notifyChrValue, message-bus listeners running as
 * fibers, the manager advertising through the legacy GAP API, security mode 2
 * pairing via peer_manager. This file is the service that pattern builds, with
 * our own UUIDs:
 *     service  e1a57a11-0000-0000-0000-0000000000d9   (= the registered base)
 *     char     e1a57a11-00fa-0000-0000-0000000000d9   READ|NOTIFY (frames out)
 *     char     e1a57a11-00fb-0000-0000-0000-0000000000d9  WRITE|WRITE_NO_RESPONSE (mode in)
 * The 16-bit codes 0x00fa/0x00fb compose into field 2 of the base — the rule
 * the utility service proves live (base e95d0000-251d-... + 0x0001/0x0002 =
 * the E95D0001/E95D0002 services captured from the board).
 */

#include "MicroBitConfig.h"

#if CONFIG_ENABLED(DEVICE_BLE)

#include "MicroBit.h"
#include "MicroBitBLEService.h"

#include "mpbluetooth_codal.h"

using namespace codal;

// Our own message-bus id; listeners are per-object so the number only has to
// not collide with another listener registered BY THIS OBJECT. Event values
// mirror the MPBT_CODAL_EVT_* tags.
#define DIGIBALL_BLE_BUS_ID      0xFB
#define DIGIBALL_BLE_LISTEN_ON() messageBus.listen(DIGIBALL_BLE_BUS_ID, MICROBIT_EVT_ANY, this, &DigiballBleService::onBusEvent)
#define DIGIBALL_BLE_LISTEN_OFF() messageBus.ignore(DIGIBALL_BLE_BUS_ID, MICROBIT_EVT_ANY, this, &DigiballBleService::onBusEvent)

#define DIGIBALL_CHAR_OUT  0   // 0x00fa READ|NOTIFY
#define DIGIBALL_CHAR_IN   1   // 0x00fb WRITE|WRITE_NO_RESPONSE
#define DIGIBALL_NUM_CHARS 2
#define DIGIBALL_STORE     64  // per-characteristic storage (SD table, p_init_value buffers)

// The base UUID in string byte order, exactly as RegisterBaseUUID wants it
// (it reverses into the SD's little-endian itself). Field 2 is the composition
// slot; service == base + 0x0000, chars == base + 0x00fa/0x00fb.
static const uint8_t digiball_base_uuid[16] =
    { 0xe1,0xa5,0x7a,0x11, 0x00,0x00, 0x00,0x00, 0x00,0x00, 0x00,0x00, 0x00,0x00, 0x00,0xd9 };

// 16-bit codes as CreateCharacteristic sees them (composed into field 2).
#define DIGIBALL_UUID16_OUT 0x00fa
#define DIGIBALL_UUID16_IN  0x00fb

static class DigiballBleService *digiball_service = NULL;
static mpbt_codal_post_fn post_cb = NULL;

static uint8_t digiball_name[16];          // config('name') storage
static size_t digiball_name_len = 0;
static uint16_t digiball_pref_mtu = 23;    // SD fixes MTU at init; set stores only
static bool digiball_started = false;

// Advertising payload storage. The SD keeps pointers into these buffers alive
// for as long as the set is configured (until advertising stops, connects, or
// the data changes), so python's raw AD bytes are copied here first — the
// caller's buffer is never referenced after mpbt_codal_adv_start returns.
// Legacy payloads cap at 31 bytes (BLE_GAP_ADV_SET_DATA_SIZE_MAX).
static uint8_t digiball_adv_payload[BLE_GAP_ADV_SET_DATA_SIZE_MAX];
static uint8_t digiball_sr_payload[BLE_GAP_ADV_SET_DATA_SIZE_MAX];

// The manager owns the one advertising set (adv_set_count == 1 in the vendored
// sdk_config.h), so that sole set is handle index 0; our data is configured on
// that same handle, then the manager starts/stops it.
static uint8_t digiball_adv_handle = 0;


class DigiballBleService : public MicroBitBLEService
{
public:

    DigiballBleService()
    {
        messageBus = &uBit.messageBus;

        memclr(characteristicValue, sizeof(characteristicValue));

        // The runtime-proven construction order, verbatim from the pattern this
        // mirrors (utility service): base, service, characteristics. The base
        // constructor has already added us to MicroBitBLEServices, so SD events
        // for our handles reach onWrite/onConnect/onDisconnect from now on.
        RegisterBaseUUID(digiball_base_uuid);
        CreateService(0x0000);            // base + 0x0000 in field 2 = the base itself

        CreateCharacteristic(DIGIBALL_CHAR_OUT, DIGIBALL_UUID16_OUT,
            characteristicValue[DIGIBALL_CHAR_OUT], 0, DIGIBALL_STORE,
            microbit_propREAD | microbit_propNOTIFY);
        CreateCharacteristic(DIGIBALL_CHAR_IN, DIGIBALL_UUID16_IN,
            characteristicValue[DIGIBALL_CHAR_IN], 0, DIGIBALL_STORE,
            microbit_propWRITE | microbit_propWRITE_WITHOUT);

        DIGIBALL_BLE_LISTEN_ON();
    }

    int              characteristicCount()         { return DIGIBALL_NUM_CHARS; }
    MicroBitBLEChar *characteristicPtr(int idx)    { return &chars[idx]; }

    // ---- events, arriving through the MicroBitBLEServices broadcast ----
    // Observer context; heavy work goes through our message-bus listener
    // (fiber) — the exact deferral the utility service does. We post, the
    // bus-fiber below calls the glue. Returning true keeps the broadcast going
    // to the other services (they filter by handle table, as usual).

    virtual bool onBleEvent(const microbit_ble_evt_t *p_ble_evt)
    {
        // Let the base dispatch connect/disconnect/write to our overrides
        // first, then pick up what it does not route.
        bool keep = MicroBitBLEService::onBleEvent(p_ble_evt);

        switch (p_ble_evt->header.evt_id)
        {
            case BLE_GATTS_EVT_EXCHANGE_MTU_REQUEST:
                // Nobody else in codal answers this request, so the central
                // would otherwise wait out the procedure. Reply with the stored
                // preference (23 unless python set one; the SD's GATT MTU is
                // fixed at init) before telling the glue.
                last_conn = p_ble_evt->evt.gatts_evt.conn_handle;
                last_param = p_ble_evt->evt.gatts_evt.params.exchange_mtu_request.client_rx_mtu;
                MICROBIT_BLE_ECHK(sd_ble_gatts_exchange_mtu_reply(last_conn, digiball_pref_mtu));
                MicroBitEvent(DIGIBALL_BLE_BUS_ID, MPBT_CODAL_EVT_MTU_EXCHANGED);
                break;
            default:
                break;
        }
        return keep;
    }

    virtual void onConnect(const microbit_ble_evt_t *p_ble_evt)
    {
        // microbit_ble_evt_t IS ble_evt_t (a direct typedef in
        // MicroBitBLETypes.h), so the peer_addr fields are reached directly.
        last_conn = p_ble_evt->evt.gap_evt.conn_handle;
        peer_type = p_ble_evt->evt.gap_evt.params.connected.peer_addr.addr_type;
        for (int i = 0; i < 6; i++)                  // SD LE -> big-endian for the glue
            peer_be[i] = p_ble_evt->evt.gap_evt.params.connected.peer_addr.addr[5 - i];
        MicroBitEvent(DIGIBALL_BLE_BUS_ID, MPBT_CODAL_EVT_CONNECT);
    }

    virtual void onDisconnect(const microbit_ble_evt_t *p_ble_evt)
    {
        last_conn = p_ble_evt->evt.gatts_evt.conn_handle;   // peer_* kept from connect
        MicroBitEvent(DIGIBALL_BLE_BUS_ID, MPBT_CODAL_EVT_DISCONNECT);
    }

    // The base class's onWrite already matched the handle and, for a CCCD
    // write, called setCCCD before dispatching here (see
    // MicroBitBLEService::onWrite), so this only picks up value writes.
    virtual void onDataWritten(const microbit_ble_evt_write_t *params)
    {
        microbit_charattr_t type;
        int idx = charHandleToIdx(params->handle, &type);
        if (idx < 0)
            return;                                   // not our handle (broadcast)

        if (type != microbit_charattrVALUE)
            return;                                   // CCCD (base tracked it) or descriptor

        last_conn = getConnectionHandle();            // single connection by design
        last_param = params->handle;                  // the value handle for the glue
        MicroBitEvent(DIGIBALL_BLE_BUS_ID, MPBT_CODAL_EVT_WRITE);
    }

    // ---- fiber context (message bus listener — the utility service's
    // pattern); safe to call the registered MicroPython callback ----

    void onBusEvent(MicroBitEvent e)
    {
        if (post_cb)
            post_cb(e.value, last_conn, last_param);
    }

    // ---- GATTS working set (called from MicroPython context) ----

    int readByCode(uint16_t uuid16, const uint8_t **value, size_t *value_len)
    {
        int idx = idxByCode(uuid16);
        if (idx < 0) return -1;

        // Copy through a static buffer so the pointer we hand the generic
        // layer stays valid until it builds its bytes object.
        uint8_t buf[DIGIBALL_STORE];
        ble_gatts_value_t v;
        v.len = DIGIBALL_STORE;
        v.offset = 0;
        v.p_value = buf;
        if (sd_ble_gatts_value_get(BLE_CONN_HANDLE_INVALID, charHandles(idx)->value, &v) != NRF_SUCCESS)
            return -1;
        memcpy(rxbuf[idx], buf, v.len);
        *value = rxbuf[idx];
        *value_len = v.len;
        return 0;
    }

    int writeByCode(uint16_t uuid16, const uint8_t *value, size_t len, bool send_update)
    {
        int idx = idxByCode(uuid16);
        if (idx < 0 || len > DIGIBALL_STORE) return -1;

        ble_gatts_value_t v;
        v.len = (uint16_t)len;
        v.offset = 0;
        v.p_value = (uint8_t *)value;
        if (sd_ble_gatts_value_set(BLE_CONN_HANDLE_INVALID, charHandles(idx)->value, &v) != NRF_SUCCESS)
            return -1;

        if (send_update && characteristicPtr(idx)->cccdNotify())
        {
            // the firmware's frames-out path: python gatts_write(h, frame, True)
            characteristicPtr(idx)->notifyChrValue(getConnectionHandle(), value, (uint16_t)len);
        }
        return 0;
    }

    int notifyByCode(uint16_t uuid16, uint16_t conn, int op, const uint8_t *value, size_t len)
    {
        int idx = idxByCode(uuid16);
        if (idx < 0 || len > DIGIBALL_STORE) return -1;
        if (conn == BLE_CONN_HANDLE_INVALID) return -1;

        bool ok;
        if (op == 1)          // MP_BLUETOOTH_GATTS_OP_NOTIFY
            ok = characteristicPtr(idx)->notifyChrValue(conn, value, (uint16_t)len);
        else if (op == 2)     // MP_BLUETOOTH_GATTS_OP_INDICATE
            ok = characteristicPtr(idx)->indicateChrValue(conn, value, (uint16_t)len);
        else
            return -1;
        return ok ? 0 : -1;
    }

    int handlesForCode(uint16_t uuid16, uint16_t *value_handle, uint16_t *cccd_handle)
    {
        int idx = idxByCode(uuid16);
        if (idx < 0) return -1;
        *value_handle = charHandles(idx)->value;
        *cccd_handle = charHandles(idx)->cccd;
        return 0;
    }

    // The per-characteristic static buffer the bridge hands out. A read lands
    // the SD value directly in it, so the pointer stays valid until the next
    // read on the same characteristic (the bridge header's contract).
    uint8_t *readStorage(uint16_t value_handle)
    {
        microbit_charattr_t type;
        int idx = charHandleToIdx(value_handle, &type);
        if (idx < 0 || type != microbit_charattrVALUE)
            return NULL;
        return rxbuf[idx];
    }

    static int idxByCode(uint16_t uuid16)
    {
        if (uuid16 == DIGIBALL_UUID16_OUT) return DIGIBALL_CHAR_OUT;
        if (uuid16 == DIGIBALL_UUID16_IN)  return DIGIBALL_CHAR_IN;
        return -1;
    }

    EventModel *messageBus;

private:

    MicroBitBLEChar chars[DIGIBALL_NUM_CHARS];
    uint8_t characteristicValue[DIGIBALL_NUM_CHARS][DIGIBALL_STORE];
    uint8_t rxbuf[DIGIBALL_NUM_CHARS][DIGIBALL_STORE];

    uint16_t last_conn;
    uint32_t last_param;
    uint8_t peer_be[6];
    uint8_t peer_type;
};


// ---------------------------------------------------------------------------
// the C-ABI bridge
// ---------------------------------------------------------------------------

extern "C" {

void mpbt_codal_set_post_cb(mpbt_codal_post_fn fn)
{
    post_cb = fn;
}

int mpbt_codal_start(void)
{
    if (digiball_started)
        return 0;

    // the createShared pattern, run once — at this call the SoftDevice is up
    // (CODAL boot did that), exactly as it is when the utility service builds
    // its service on demand.
    digiball_service = new DigiballBleService();
    if (digiball_service == NULL)
        return -1;

    digiball_started = true;
    return 0;
}

void mpbt_codal_stop(void)
{
    // The SD has no teardown: the service stays registered, the listener
    // goes quiet. Nothing else to undo.
    if (digiball_started && digiball_service)
    {
        digiball_service->messageBus->ignore(DIGIBALL_BLE_BUS_ID, MICROBIT_EVT_ANY,
            digiball_service, &DigiballBleService::onBusEvent);
    }
    digiball_started = false;
}

bool mpbt_codal_is_active(void)
{
    return digiball_started;
}

int mpbt_codal_get_address(uint8_t *addr_type, uint8_t *addr_be)
{
    ble_gap_addr_t a;
    if (sd_ble_gap_addr_get(&a) != NRF_SUCCESS)
        return -1;
    *addr_type = a.addr_type;
    for (int i = 0; i < 6; i++)                      // SD LE -> big-endian
        addr_be[i] = a.addr[5 - i];
    return 0;
}

int mpbt_codal_set_address_mode(uint8_t mode)
{
    // The local address is fixed by the manager's GAP setup; the glue keeps
    // its own copy of the mode, so there is nothing to apply here (python
    // never calls this).
    (void)mode;
    return 0;
}

int mpbt_codal_peer(uint8_t *addr_type, uint8_t *addr_be)
{
    if (digiball_service == NULL) return -1;
    *addr_type = digiball_service->peer_type;
    memcpy(addr_be, digiball_service->peer_be, 6);
    return 0;
}

int mpbt_codal_name_get(const uint8_t **buf)
{
    *buf = digiball_name;                            // storage; the generic layer
    return (int)digiball_name_len;                   // asks only after a set
}

int mpbt_codal_name_set(const uint8_t *buf, size_t len)
{
    if (len > sizeof(digiball_name))
        return -1;
    memcpy(digiball_name, buf, len);
    digiball_name_len = len;
    // GAP device-name update through the real s140 call; the write permission
    // is the one the manager configured for the name at boot (not writable by
    // the peer).
    ble_gap_conn_sec_mode_t write_perm;
    BLE_GAP_CONN_SEC_MODE_SET_NO_ACCESS(&write_perm);
    return sd_ble_gap_device_name_set(&write_perm, buf, (uint16_t)len) == NRF_SUCCESS ? 0 : -1;
}

int mpbt_codal_adv_start(bool connectable, int32_t interval_us,
    const uint8_t *adv_data, size_t adv_len, const uint8_t *sr_data, size_t sr_len)
{
    (void)interval_us;                               // advisory: the SD uses its configured interval

    // the manager only builds connectable advertising; non-connectable legacy
    // advertising has no data slot, and the firmware never asks for it, so
    // refuse loudly rather than advertise the wrong thing
    if (!connectable)
        return -1;

    if (adv_len > BLE_GAP_ADV_SET_DATA_SIZE_MAX ||
        sr_len > BLE_GAP_ADV_SET_DATA_SIZE_MAX)      // legacy payload cap
        return -1;

    // the SD keeps using these buffers while the set is configured, so copy
    // python's payloads out of caller memory first
    if (adv_len)
        memcpy(digiball_adv_payload, adv_data, adv_len);
    if (sr_len)
        memcpy(digiball_sr_payload, sr_data, sr_len);

    ble_gap_adv_data_t gap_adv_data;
    memset(&gap_adv_data, 0, sizeof(gap_adv_data));
    gap_adv_data.adv_data.p_data      = adv_len ? digiball_adv_payload : NULL;
    gap_adv_data.adv_data.len         = (uint16_t)adv_len;
    gap_adv_data.scan_rsp_data.p_data = sr_len ? digiball_sr_payload : NULL;
    gap_adv_data.scan_rsp_data.len    = (uint16_t)sr_len;

    // legacy-PDU connectable setup, filled the way the manager fills it; the
    // interval is the manager's configured default (200 ms / 0.625 ms units)
    ble_gap_adv_params_t gap_adv_params;
    memset(&gap_adv_params, 0, sizeof(gap_adv_params));
    gap_adv_params.properties.type = BLE_GAP_ADV_TYPE_CONNECTABLE_SCANNABLE_UNDIRECTED;
    gap_adv_params.interval        = 320;
    gap_adv_params.filter_policy   = BLE_GAP_ADV_FP_ANY;
    gap_adv_params.primary_phy     = BLE_GAP_PHY_1MBPS;

    // handle index 0: the manager owns the one advertising set (adv_set_count
    // is 1), so its sole set is 0 and its later start/stop targets it
    if (sd_ble_gap_adv_set_configure(&digiball_adv_handle, &gap_adv_data, &gap_adv_params) != NRF_SUCCESS)
        return -1;

    // advertising control through the manager — the live pairing-mode path
    // (verbatim sd_ble_gap_adv_start(m_adv_handle, microbit_ble_CONN_CFG_TAG))
    BLEDevice::getInstance()->advertise();
    return 0;
}

void mpbt_codal_adv_stop(void)
{
    BLEDevice::getInstance()->stopAdvertising();     // live pairing-mode path
}

int mpbt_codal_gatts_read(uint16_t value_handle, const uint8_t **value, size_t *value_len)
{
    if (digiball_service == NULL) return -1;

    // The SD's attribute table holds the value; the service's per-index static
    // rxbuf is where it is copied to and what we hand back, so the pointer
    // stays valid until the next read on the same characteristic (the generic
    // layer builds its bytes object right after).
    uint8_t *storage = digiball_service->readStorage(value_handle);
    if (storage == NULL) return -1;

    ble_gatts_value_t v;
    v.len = DIGIBALL_STORE;
    v.offset = 0;
    v.p_value = storage;
    if (sd_ble_gatts_value_get(BLE_CONN_HANDLE_INVALID, value_handle, &v) != NRF_SUCCESS)
        return -1;

    *value = storage;
    *value_len = v.len;
    return 0;
}

int mpbt_codal_gatts_write(uint16_t value_handle, const uint8_t *value, size_t value_len, bool send_update)
{
    if (digiball_service == NULL || value_len > DIGIBALL_STORE) return -1;

    microbit_charattr_t type;
    int idx = digiball_service->charHandleToIdx(value_handle, &type);
    if (idx < 0 || type != microbit_charattrVALUE) return -1;

    // setChrValue — the utility service's live mechanism
    if (!digiball_service->characteristicPtr(idx)->setChrValue(BLE_CONN_HANDLE_INVALID, value, (uint16_t)value_len))
        return -1;

    if (send_update)
    {
        // python gatts_write(h, frame, True) — the frames-out path: if the
        // peer enabled notifications, send it now (notifyChrValue falls back
        // to storage-set, which we already did)
        digiball_service->characteristicPtr(idx)->notifyChrValue(
            digiball_service->getConnectionHandle(), value, (uint16_t)value_len);
    }
    return 0;
}

int mpbt_codal_gatts_notify_indicate(uint16_t conn_handle, uint16_t value_handle, int op, const uint8_t *value, size_t value_len)
{
    if (digiball_service == NULL || value_len > DIGIBALL_STORE) return -1;

    microbit_charattr_t type;
    int idx = digiball_service->charHandleToIdx(value_handle, &type);
    if (idx < 0 || type != microbit_charattrVALUE) return -1;

    bool ok;
    if (op == 1)                                       // MP_BLUETOOTH_GATTS_OP_NOTIFY
        ok = digiball_service->characteristicPtr(idx)->notifyChrValue(conn_handle, value, (uint16_t)value_len);
    else if (op == 2)                                  // MP_BLUETOOTH_GATTS_OP_INDICATE
        ok = digiball_service->characteristicPtr(idx)->indicateChrValue(conn_handle, value, (uint16_t)value_len);
    else
        return -1;
    return ok ? 0 : -1;
}

int mpbt_codal_gatts_set_buffer(uint16_t value_handle, size_t len, bool append)
{
    (void)append;
    if (digiball_service == NULL) return -1;
    microbit_charattr_t type;
    int idx = digiball_service->charHandleToIdx(value_handle, &type);
    if (idx < 0) return -1;
    return len <= DIGIBALL_STORE ? 0 : -1;             // storage is fixed at start
}

int mpbt_codal_lookup(uint16_t uuid16, uint16_t *value_handle, uint16_t *cccd_handle)
{
    if (digiball_service == NULL) return -1;

    int idx = DigiballBleService::idxByCode(uuid16);
    if (idx < 0) return -1;

    *value_handle = digiball_service->charHandles(idx)->value;
    *cccd_handle = digiball_service->charHandles(idx)->cccd;   // 0 if the SD minted none
    return 0;
}

int mpbt_codal_gap_disconnect(uint16_t conn_handle)
{
    return sd_ble_gap_disconnect(conn_handle, BLE_HCI_REMOTE_USER_TERMINATED_CONNECTION) == 0 ? 0 : -1;
}

int mpbt_codal_get_preferred_mtu(void)
{
    return digiball_pref_mtu;
}

int mpbt_codal_set_preferred_mtu(uint16_t mtu)
{
    if (mtu < 23 || mtu > DIGIBALL_STORE) return -1;  // SD fixed it at init; stored answer only
    digiball_pref_mtu = mtu;
    return 0;
}

int mpbt_codal_gap_pair(uint16_t conn_handle)
{
    // peer_manager is the single keystore, initialized and registered by the
    // manager at boot — do not touch pm_* here (its event observer is
    // singular). NULL asks the SD to use the globally set security params,
    // which are the ones the manager installed via pm_sec_params_set: on this
    // target MICROBIT_BLE_SECURITY_MODE 2 (pairing required, no MITM). The
    // firmware never calls this — access to our characteristics is what pairs
    // the peer automatically.
    return sd_ble_gap_authenticate(conn_handle, NULL) == NRF_SUCCESS ? 0 : -1;
}

int mpbt_codal_gap_passkey(uint16_t conn_handle, uint8_t action, uint32_t passkey)
{
    (void)conn_handle; (void)action; (void)passkey;  // no prompts on this path (security mode 2)
    return 0;
}

} // extern "C"

#endif // CONFIG_ENABLED(DEVICE_BLE)
