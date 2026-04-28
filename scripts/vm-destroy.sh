#!/bin/bash
# Destroy a VM by name — removes terraform config and runs terraform apply
set -e

VM_NAME="$1"
[ -z "$VM_NAME" ] && { echo "Usage: blockhost-vm-destroy <name>" >&2; exit 1; }

exec python3 - "$VM_NAME" << 'PYEOF'
import subprocess
import sys

from blockhost.config import load_db_config
from blockhost.provisioner_proxmox import get_terraform_dir, sanitize_resource_name
from blockhost.root_agent import RootAgentError, ip6_route_del
from blockhost.vm_db import get_database

vm_name = sys.argv[1]
db = get_database()
vm = db.get_vm(vm_name)

# Idempotent: missing or already-destroyed records are a no-op success.
if not vm:
    print(f"VM {vm_name} not in DB — already destroyed", file=sys.stderr)
    sys.exit(0)
if vm.get("status") == "destroyed":
    print(f"VM {vm_name} already destroyed", file=sys.stderr)
    sys.exit(0)

tf_dir = get_terraform_dir()
tf_file = tf_dir / f"{vm_name}.tf.json"
ci_file = tf_dir / f"{vm_name}-cloud-config.yaml"

# Destroy via terraform first (only after that succeeds is it safe to delete sources).
if tf_file.exists():
    resource_name = sanitize_resource_name(vm_name)
    target = f"proxmox_virtual_environment_vm.{resource_name}"
    result = subprocess.run(
        ["terraform", "destroy", "-target", target, "-auto-approve"],
        cwd=str(tf_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Terraform error: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    for f in [tf_file, ci_file]:
        if f.exists():
            f.unlink()
            print(f"Removed {f}")

# Remove IPv6 host route if VM had one
ipv6 = vm.get("ipv6_address")
if ipv6:
    bridge = load_db_config().get("bridge", "vmbr0")
    try:
        ip6_route_del(f"{ipv6}/128", bridge)
        print(f"Removed IPv6 host route: {ipv6}/128")
    except RootAgentError:
        pass  # Route may already be gone

db.mark_destroyed(vm_name)
print(f"VM {vm_name} destroyed")
PYEOF
