#!/bin/bash
# List all VMs (text or JSON output)
set -e

exec python3 - "$@" << 'PYEOF'
import json
import sys

from blockhost.vm_db import get_database

args = sys.argv[1:]
# Accept both --json (legacy) and --format json (contract)
use_json = "--json" in args
if "--format" in args:
    idx = args.index("--format")
    use_json = use_json or (idx + 1 < len(args) and args[idx + 1] == "json")

db = get_database()
vms = db.list_vms()

if use_json:
    print(json.dumps(
        [
            {
                "name": v["vm_name"],
                "status": v.get("status", "unknown"),
                "ip": v.get("ip_address", ""),
                "created": v.get("created_at", "")[:10],
            }
            for v in vms
        ],
        indent=2,
    ))
else:
    print("NAME\tSTATUS\tIP\tCREATED")
    for v in vms:
        name = v["vm_name"]
        status = v.get("status", "unknown")
        ip = v.get("ip_address", "")
        created = v.get("created_at", "")[:10]
        print(f"{name}\t{status}\t{ip}\t{created}")
PYEOF
