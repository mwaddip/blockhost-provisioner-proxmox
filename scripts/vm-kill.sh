#!/bin/bash
# Force-stop a VM by name
set -e

VM_NAME="$1"
[ -z "$VM_NAME" ] && { echo "Usage: blockhost-vm-kill <name>" >&2; exit 1; }

exec python3 - "$VM_NAME" << 'PYEOF'
import sys

from blockhost.root_agent import qm_stop
from blockhost.vm_db import get_database

vm_name = sys.argv[1]
db = get_database()
vm = db.get_vm(vm_name)
if not vm:
    print(f"VM {vm_name} not found", file=sys.stderr)
    sys.exit(1)

qm_stop(vm["vmid"])
print(f"Killed {vm_name} (VMID {vm['vmid']})")
PYEOF
