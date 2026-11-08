# Build flags for the bluetooth module (added for DigiBall).
#
# MICROPY_BLUETOOTH_NIMBLE=1 satisfies the extmod.mk gate
# ("Must enable one of MICROPY_BLUETOOTH_NIMBLE or MICROPY_BLUETOOTH_BTSTACK").
# Its source lists are inert for this port (py.mk does not fold SRC_EXTMOD_C
# into OBJ; the port links an explicit SRC_C list) - the actual backend is
# modbluetooth_glue.c in this directory, talking to the CODAL BLE stack through
# the codal_app C-ABI bridge.
#
# The defines go on the global CFLAGS so the compile pass and the qstr/module
# preprocessing pass (QSTR_GEN_CFLAGS := $(CFLAGS)) see exactly the same
# defines - registration and qstr extraction of MP_REGISTER_EXTENSIBLE_MODULE
# in extmod/modbluetooth.c depend on it.

MICROPY_PY_BLUETOOTH = 1
MICROPY_BLUETOOTH_NIMBLE = 1

CFLAGS_EXTRA += -DMICROPY_PY_BLUETOOTH=1 -DMICROPY_PY_BLUETOOTH_ENABLE_PAIRING_BONDING=1 -DMICROPY_PY_BLUETOOTH_USE_SYNC_EVENTS=1
