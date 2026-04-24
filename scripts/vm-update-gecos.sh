#!/bin/bash
# Update a VM user's GECOS field (wallet + NFT token) after ownership transfer.
# Delegates to blockhost-vm-guest-exec — this script handles validation and
# command composition; guest-exec handles VM lookup and execution.
set -e

VM_NAME="$1"
WALLET_ADDRESS="$2"
shift 2 2>/dev/null || { echo "Usage: blockhost-vm-update-gecos <vm-name> <wallet-address> --nft-id <id> [--username <user>]" >&2; exit 1; }

exec python3 - "$VM_NAME" "$WALLET_ADDRESS" "$@" << 'PYEOF'
import argparse
import re
import shlex
import subprocess
import sys

WALLET_RE = re.compile(r'^[a-zA-Z0-9]{1,128}$')
USERNAME_RE = re.compile(r'^[a-z_][a-z0-9_-]{0,31}$')

parser = argparse.ArgumentParser()
parser.add_argument("vm_name")
parser.add_argument("wallet_address")
parser.add_argument("--nft-id", required=True, type=int, help="NFT token ID")
parser.add_argument("--username", default="admin", help="Linux username (default: admin)")
args = parser.parse_args()

if not WALLET_RE.match(args.wallet_address):
    print(f"Error: Invalid wallet address format: {args.wallet_address}", file=sys.stderr)
    sys.exit(1)

if not USERNAME_RE.match(args.username):
    print(f"Error: Invalid username: {args.username}", file=sys.stderr)
    sys.exit(1)

gecos = f"wallet={args.wallet_address},nft={args.nft_id}"
command = f"usermod -c {shlex.quote(gecos)} {shlex.quote(args.username)}"

result = subprocess.run(
    ["blockhost-vm-guest-exec", args.vm_name, command],
    check=False,
)

if result.returncode == 0:
    print(f"Updated GECOS for {args.vm_name}: {gecos}")
else:
    print(f"Error: Failed to update GECOS for {args.vm_name} (exit {result.returncode})", file=sys.stderr)

sys.exit(result.returncode)
PYEOF
