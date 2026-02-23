#!/bin/bash
# Start a VM by name
set -e

VM_NAME="$1"
[ -z "$VM_NAME" ] && { echo "Usage: blockhost-vm-start <name>" >&2; exit 1; }

exec python3 - "$VM_NAME" << 'PYEOF'
import sys

from blockhost.root_agent import RootAgentError, call, qm_start
from blockhost.vm_db import get_database

vm_name = sys.argv[1]
db = get_database()
vm = db.get_vm(vm_name)
if not vm:
    print(f"VM {vm_name} not found", file=sys.stderr)
    sys.exit(1)

try:
    qm_start(vm["vmid"])
except RootAgentError as e:
    print(f"Error starting {vm_name}: {e}", file=sys.stderr)
    sys.exit(1)
print(f"Started {vm_name} (VMID {vm['vmid']})")

# Enable bridge port isolation so VMs cannot see each other's L2 traffic
tap_dev = f"tap{vm['vmid']}i0"
try:
    call("bridge-port-isolate", dev=tap_dev)
    print(f"Bridge port isolation enabled on {tap_dev}")
except RootAgentError as e:
    print(f"Warning: bridge port isolation failed on {tap_dev}: {e}", file=sys.stderr)
PYEOF
