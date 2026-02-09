#!/bin/bash
# Print the status of a VM by name
set -e

VM_NAME="$1"
[ -z "$VM_NAME" ] && { echo "Usage: blockhost-vm-status <name>" >&2; exit 1; }

exec python3 - "$VM_NAME" << 'PYEOF'
import sys

from blockhost.vm_db import get_database

vm_name = sys.argv[1]
db = get_database()
vm = db.get_vm(vm_name)
if not vm:
    print("unknown")
    sys.exit(0)

print(vm.get("status", "unknown"))
PYEOF
