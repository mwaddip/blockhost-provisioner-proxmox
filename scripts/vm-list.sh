#!/bin/bash
# List all VMs (text or JSON output)
set -e

exec python3 - "$@" << 'PYEOF'
import json
import sys

from blockhost.vm_db import get_database

use_json = "--json" in sys.argv[1:]

db = get_database()
vms = db.list_vms()

if use_json:
    print(json.dumps(
        [{"name": v["vm_name"], "status": v.get("status", "unknown")} for v in vms],
        indent=2,
    ))
else:
    for v in vms:
        print(f"{v['vm_name']}\t{v.get('status', 'unknown')}")
PYEOF
