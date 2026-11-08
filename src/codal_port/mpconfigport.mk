# Build flags for the bluetooth module (added for DigiBall).
#
# The backend switches stay UNSET at the make level on purpose: setting
# MICROPY_PY_BLUETOOTH here would arm extmod.mk's gate block, which insists
# on MICROPY_BLUETOOTH_NIMBLE/BTSTACK and includes the matching .mk - and
# those add their own port-implementation .c files (modbluetooth_nimble.c
# etc.) to the build, duplicating every symbol modbluetooth_glue.c provides.
# Left unset, the gate block and both .mk includes are skipped entirely and
# modbluetooth_glue.c is the only backend.
#
# The defines go on the global CFLAGS so the compile pass and the qstr/module
# preprocessing pass (QSTR_GEN_CFLAGS := $(CFLAGS)) see exactly the same
# defines - registration and qstr extraction of MP_REGISTER_EXTENSIBLE_MODULE
# in extmod/modbluetooth.c depend on it.

CFLAGS_EXTRA += -DMICROPY_PY_BLUETOOTH=1 -DMICROPY_PY_BLUETOOTH_ENABLE_PAIRING_BONDING=1 -DMICROPY_PY_BLUETOOTH_USE_SYNC_EVENTS=1
