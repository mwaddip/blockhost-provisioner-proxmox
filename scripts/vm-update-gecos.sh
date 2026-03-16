#!/bin/bash
# Update a VM user's GECOS field (wallet + NFT token) after ownership transfer
set -e

VM_NAME="$1"
WALLET_ADDRESS="$2"
shift 2 2>/dev/null || { echo "Usage: blockhost-vm-update-gecos <vm-name> <wallet-address> --nft-id <id> [--username <user>]" >&2; exit 1; }

exec python3 - "$VM_NAME" "$WALLET_ADDRESS" "$@" << 'PYEOF'
import argparse
import re
import sys

from blockhost.root_agent import call, RootAgentError
from blockhost.vm_db import get_database

parser = argparse.ArgumentParser()
parser.add_argument("vm_name")
parser.add_argument("wallet_address")
parser.add_argument("--nft-id", required=True, type=int, help="NFT token ID")
parser.add_argument("--username", default="admin", help="Linux username (default: admin)")
args = parser.parse_args()

if not re.match(r'^[a-zA-Z0-9]{1,128}$', args.wallet_address):
    print(f"Error: Invalid wallet address format: {args.wallet_address}", file=sys.stderr)
    sys.exit(1)

db = get_database()
vm = db.get_vm(args.vm_name)
if not vm:
    print(f"Error: VM '{args.vm_name}' not found", file=sys.stderr)
    sys.exit(1)

if vm.get("status") != "active":
    print(f"Error: VM '{args.vm_name}' is not active (status: {vm.get('status')})", file=sys.stderr)
    sys.exit(1)

gecos = f"wallet={args.wallet_address},nft={args.nft_id}"

try:
    call("qm-update-gecos", vmid=vm["vmid"], username=args.username, gecos=gecos)
    print(f"Updated GECOS for {args.vm_name}: {gecos}")
except RootAgentError as e:
    print(f"Error: Failed to update GECOS for {args.vm_name}: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
