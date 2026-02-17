#!/bin/bash
# Update a VM user's GECOS field (wallet + NFT token) after ownership transfer
set -e

VM_NAME="$1"
WALLET_ADDRESS="$2"
[ -z "$VM_NAME" ] || [ -z "$WALLET_ADDRESS" ] && {
    echo "Usage: blockhost-vm-update-gecos <vm-name> <wallet-address>" >&2
    exit 1
}

exec python3 - "$VM_NAME" "$WALLET_ADDRESS" << 'PYEOF'
import sys

from blockhost.root_agent import call, RootAgentError
from blockhost.vm_db import get_database

vm_name = sys.argv[1]
wallet_address = sys.argv[2]

db = get_database()
vm = db.get_vm(vm_name)
if not vm:
    print(f"Error: VM '{vm_name}' not found", file=sys.stderr)
    sys.exit(1)

if vm.get("status") != "active":
    print(f"Error: VM '{vm_name}' is not active (status: {vm.get('status')})", file=sys.stderr)
    sys.exit(1)

nft_token_id = vm.get("nft_token_id")
if nft_token_id is None:
    print(f"Error: VM '{vm_name}' has no NFT token ID", file=sys.stderr)
    sys.exit(1)

gecos = f"wallet={wallet_address},nft={nft_token_id}"

try:
    result = call("qm-update-gecos", vmid=vm["vmid"], username="admin", gecos=gecos)
    print(f"Updated GECOS for {vm_name}: {gecos}")
except RootAgentError as e:
    print(f"Error: Failed to update GECOS for {vm_name}: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
