# VM Metrics & Throttle Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `blockhost-vm-metrics` and `blockhost-vm-throttle` commands, replacing stubs, per the contract in `facts/PROVISIONER_INTERFACE.md` sections 2.12 and 2.13.

**Architecture:** Metrics uses the Proxmox HTTP API (urllib, no external deps) for cheap per-VM polling. Throttle delegates to two new root agent actions (`pve-set-throttle` for CPU/IOPS via `qm set`, `tc-rate-limit` for bandwidth via `tc`). Shared PVE API helpers live in `blockhost/provisioner_proxmox/__init__.py`.

**Tech Stack:** Python 3.10+, urllib.request (stdlib), Proxmox REST API, `tc` for traffic shaping, `qm set` for CPU/IOPS limits.

**Spec:** `docs/superpowers/specs/2026-03-16-vm-metrics-throttle-design.md`

**Note on testing:** This project has no test infrastructure (no `tests/` dir, no pytest). Scripts talk to Proxmox APIs and root agent daemons that don't exist locally. Verification is syntax checks (`ast.parse`, `bash -n`) and `.deb` builds. This is consistent with how every other script in this repo is verified.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `blockhost/provisioner_proxmox/__init__.py` | Modify | Add `load_pve_credentials()`, `pve_api_get()` |
| `scripts/vm-metrics.py` | Create | Metrics command — 3 PVE API calls, JSON output |
| `scripts/vm-throttle.py` | Create | Throttle command — delegates to root agent |
| `root-agent-actions/qm.py` | Modify | Add `handle_pve_set_throttle()`, `handle_tc_rate_limit()`, register in ACTIONS |
| `build-deb.sh` | Modify | Change `.sh` → `.py` for metrics/throttle install lines |
| `PROJECT.yaml` | Modify | Update `vm_metrics`, `vm_throttle`, document new root agent actions |
| `scripts/vm-metrics.sh` | Delete | Replaced by `.py` |
| `scripts/vm-throttle.sh` | Delete | Replaced by `.py` |

---

## Task 1: PVE API helpers in `__init__.py`

**Files:**
- Modify: `blockhost/provisioner_proxmox/__init__.py`

Both metrics and throttle need PVE API access. Add it first so the scripts can import it.

- [ ] **Step 1: Add imports**

At the top of `blockhost/provisioner_proxmox/__init__.py`, add after the existing imports:

```python
import json
import socket
import ssl
import urllib.request
```

- [ ] **Step 2: Add `load_pve_credentials()`**

After `sanitize_resource_name()`, add:

```python
def load_pve_credentials() -> tuple[str, str, str]:
    """Load Proxmox API credentials.

    Returns:
        (api_url, api_token, node_name)

    api_token is read from /etc/blockhost/pve-token.
    node_name is read from terraform.tfvars (proxmox_node key),
    falling back to the system hostname.
    """
    from pathlib import Path

    token_file = Path("/etc/blockhost/pve-token")
    if not token_file.exists():
        raise FileNotFoundError(f"PVE token not found: {token_file}")
    api_token = token_file.read_text().strip()

    # Read node name from terraform.tfvars
    node_name = socket.gethostname()
    tf_dir = get_terraform_dir()
    tfvars_file = tf_dir / "terraform.tfvars"
    if tfvars_file.exists():
        for line in tfvars_file.read_text().split("\n"):
            line = line.strip()
            if line.startswith("proxmox_node"):
                _, _, value = line.partition("=")
                node_name = value.strip().strip('"')
                break

    return ("https://127.0.0.1:8006", api_token, node_name)
```

- [ ] **Step 3: Add `pve_api_get()`**

After `load_pve_credentials()`, add:

```python
def pve_api_get(path: str, credentials: tuple = None, timeout: int = 5) -> dict:
    """GET a Proxmox API endpoint and return the JSON 'data' key.

    Args:
        path: API path (e.g. '/api2/json/nodes/pve/qemu/100/status/current')
        credentials: (api_url, api_token, node_name) tuple, or None to auto-load
        timeout: HTTP timeout in seconds (default 5)

    Returns:
        The 'data' value from the JSON response.

    Raises:
        urllib.error.URLError: On HTTP errors or timeouts.
        KeyError: If response has no 'data' key.
    """
    if credentials is None:
        credentials = load_pve_credentials()
    api_url, api_token, _ = credentials

    url = f"{api_url}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"PVEAPIToken={api_token}")

    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = json.loads(resp.read())
    return body["data"]
```

- [ ] **Step 4: Syntax check**

Run: `python3 -c "import ast; ast.parse(open('blockhost/provisioner_proxmox/__init__.py').read()); print('OK')"`

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add blockhost/provisioner_proxmox/__init__.py
git commit -m "Add PVE API helpers: load_pve_credentials, pve_api_get"
```

---

## Task 2: Metrics script (`scripts/vm-metrics.py`)

**Files:**
- Create: `scripts/vm-metrics.py`

Implements contract section 2.12. Outputs JSON with all 16 fields.

- [ ] **Step 1: Create `scripts/vm-metrics.py`**

```python
#!/usr/bin/env python3
"""
VM Metrics — collect resource usage for a VM.

Called by blockhost-monitor at regular intervals. Must be cheap —
execution cost multiplies by VM count x poll frequency.

Uses the Proxmox API (3 calls max):
1. status/current — CPU, memory, uptime, state
2. rrddata — disk I/O and network rates (last sample)
3. agent/get-fsinfo — disk usage (guest agent, non-fatal)

Usage:
    blockhost-vm-metrics <vm-name>
"""

import json
import math
import sys
import urllib.error

from blockhost.provisioner_proxmox import load_pve_credentials, pve_api_get
from blockhost.vm_db import get_database


def get_last_rrd_sample(rrd_data: list) -> dict:
    """Extract the last non-NaN sample from RRD data.

    Proxmox RRD samples may contain NaN for fields that haven't been
    updated yet. Walk backwards to find the most recent valid sample.
    """
    for sample in reversed(rrd_data):
        # A valid sample has at least one non-NaN numeric rate field
        if any(
            isinstance(sample.get(k), (int, float)) and not math.isnan(sample.get(k, float('nan')))
            for k in ('diskread', 'diskwrite', 'netin', 'netout')
        ):
            return sample
    return {}


def get_disk_info_from_agent(node: str, vmid: int, credentials: tuple) -> dict:
    """Query guest agent for filesystem info.

    Returns dict with 'disk_used_mb', 'disk_total_mb', 'responsive'.
    Non-fatal — returns defaults if agent is unresponsive.
    """
    try:
        fs_info = pve_api_get(
            f"/api2/json/nodes/{node}/qemu/{vmid}/agent/get-fsinfo",
            credentials=credentials,
            timeout=3,
        )
        used_bytes = 0
        total_bytes = 0
        for fs in fs_info:
            used_bytes += fs.get("used-bytes", 0)
            total_bytes += fs.get("total-bytes", 0)
        return {
            "disk_used_mb": int(used_bytes / 1048576),
            "disk_total_mb": int(total_bytes / 1048576),
            "responsive": True,
        }
    except Exception:
        return {
            "disk_used_mb": -1,
            "disk_total_mb": None,  # caller falls back to maxdisk
            "responsive": False,
        }


def main():
    if len(sys.argv) != 2:
        print("Usage: blockhost-vm-metrics <vm-name>", file=sys.stderr)
        sys.exit(1)

    vm_name = sys.argv[1]

    # Look up VM in database
    db = get_database()
    vm = db.get_vm(vm_name)
    if not vm:
        print(f"VM '{vm_name}' not found", file=sys.stderr)
        sys.exit(1)

    vmid = vm["vmid"]

    # Load credentials once
    try:
        creds = load_pve_credentials()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    _, _, node = creds

    # 1. Status/current — CPU, memory, uptime, state
    try:
        status = pve_api_get(
            f"/api2/json/nodes/{node}/qemu/{vmid}/status/current",
            credentials=creds,
        )
    except urllib.error.URLError as e:
        print(f"Error querying VM {vmid}: {e}", file=sys.stderr)
        sys.exit(1)

    cpu_raw = status.get("cpu", 0)
    cpu_count = status.get("cpus", 1)
    if cpu_raw > 1.0:
        print(
            f"Warning: PVE cpu={cpu_raw} > 1.0 — formula may need adjustment",
            file=sys.stderr,
        )

    # Map PVE status to contract state
    pve_status = status.get("status", "unknown")
    state_map = {"running": "running", "paused": "paused", "stopped": "stopped"}
    state = state_map.get(pve_status, "unknown")

    # 2. RRD data — disk I/O and network rates
    rrd_sample = {}
    try:
        rrd_data = pve_api_get(
            f"/api2/json/nodes/{node}/qemu/{vmid}/rrddata?timeframe=hour",
            credentials=creds,
        )
        rrd_sample = get_last_rrd_sample(rrd_data)
    except Exception:
        pass  # Non-fatal — rates will be 0

    def safe_rate(key):
        """Extract a rate value from RRD, defaulting to 0."""
        val = rrd_sample.get(key, 0)
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return 0
        return int(val)

    # 3. Guest agent — disk usage
    agent_info = get_disk_info_from_agent(node, vmid, creds)

    # disk_total_mb: prefer agent data for consistency with disk_used_mb
    disk_total_mb = agent_info["disk_total_mb"]
    if disk_total_mb is None:
        disk_total_mb = int(status.get("maxdisk", 0) / 1048576)

    # Assemble output per contract schema
    metrics = {
        "cpu_percent": round(cpu_raw * 100 * cpu_count, 1),
        "cpu_count": cpu_count,
        "memory_used_mb": int(status.get("mem", 0) / 1048576),
        "memory_total_mb": int(status.get("maxmem", 0) / 1048576),
        "disk_used_mb": agent_info["disk_used_mb"],
        "disk_total_mb": disk_total_mb,
        "disk_read_iops": 0,
        "disk_write_iops": 0,
        "disk_read_bytes_sec": safe_rate("diskread"),
        "disk_write_bytes_sec": safe_rate("diskwrite"),
        "net_rx_bytes_sec": safe_rate("netin"),
        "net_tx_bytes_sec": safe_rate("netout"),
        "net_connections": -1,
        "guest_agent_responsive": agent_info["responsive"],
        "uptime_seconds": status.get("uptime", 0),
        "state": state,
    }

    print(json.dumps(metrics))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Syntax check**

Run: `python3 -c "import ast; ast.parse(open('scripts/vm-metrics.py').read()); print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/vm-metrics.py
git commit -m "Add vm-metrics: collect VM resource usage via Proxmox API"
```

---

## Task 3: Root agent actions (`pve-set-throttle`, `tc-rate-limit`)

**Files:**
- Modify: `root-agent-actions/qm.py`

Add two new actions. These run as root and must validate everything strictly.

- [ ] **Step 1: Add constants after existing allowlists (after line 32)**

After `QM_SET_ALLOWED_KEYS`, add:

```python
# Throttle-specific allowlists — separate from QM_SET_ALLOWED_KEYS
# so the throttle action can't set arbitrary VM config.
THROTTLE_ALLOWED_KEYS = frozenset({
    'cpuunits', 'cpulimit', 'iops_rd', 'iops_wr',
})

# bps_rd/bps_wr included in deletable keys only — reset clears them
# in case they were set by an older version or manual intervention
THROTTLE_DELETABLE_KEYS = THROTTLE_ALLOWED_KEYS | frozenset({
    'bps_rd', 'bps_wr',
})

TAP_RE = re.compile(r'^tap\d+i\d+$')
```

- [ ] **Step 2: Add `handle_pve_set_throttle()` after `handle_qm_update_gecos()`**

```python
def handle_pve_set_throttle(params):
    """Apply CPU and IOPS throttle limits via qm set.

    params:
        vmid (int): VM ID
        config (dict): Key-value pairs from THROTTLE_ALLOWED_KEYS
        delete_keys (list): Optional list of keys to delete (from THROTTLE_DELETABLE_KEYS)
    """
    vmid = validate_vmid(params['vmid'])
    config = params.get('config', {})
    delete_keys = params.get('delete_keys', [])

    if not isinstance(config, dict):
        return {'ok': False, 'error': 'config must be a dict'}
    if not isinstance(delete_keys, list):
        return {'ok': False, 'error': 'delete_keys must be a list'}

    if not config and not delete_keys:
        return {'ok': False, 'error': 'config or delete_keys required'}

    cmd = ['qm', 'set', str(vmid)]

    # Validate and add config keys
    for key, value in config.items():
        if key not in THROTTLE_ALLOWED_KEYS:
            return {'ok': False, 'error': f'Disallowed throttle key: {key}'}

        if key == 'cpuunits':
            try:
                v = int(value)
            except (ValueError, TypeError):
                return {'ok': False, 'error': f'cpuunits must be int: {value}'}
            if v < 1 or v > 10000:
                return {'ok': False, 'error': f'cpuunits out of range (1-10000): {v}'}
        elif key == 'cpulimit':
            try:
                v = float(value)
            except (ValueError, TypeError):
                return {'ok': False, 'error': f'cpulimit must be float: {value}'}
            if v < 0 or v > 128:
                return {'ok': False, 'error': f'cpulimit out of range (0-128): {v}'}
        elif key in ('iops_rd', 'iops_wr'):
            try:
                v = int(value)
            except (ValueError, TypeError):
                return {'ok': False, 'error': f'{key} must be int: {value}'}
            if v < 0:
                return {'ok': False, 'error': f'{key} must be >= 0: {v}'}

        cmd.extend([f'--{key}', str(value)])

    # Validate and add delete keys
    if delete_keys:
        for key in delete_keys:
            if key not in THROTTLE_DELETABLE_KEYS:
                return {'ok': False, 'error': f'Disallowed delete key: {key}'}
        cmd.extend(['--delete', ','.join(delete_keys)])

    rc, out, err = run(cmd, timeout=30)
    if rc != 0:
        return {'ok': False, 'error': err or out}
    return {'ok': True, 'output': out}
```

- [ ] **Step 3: Add `handle_tc_rate_limit()` with section comment**

```python
# --- Traffic Control Actions ---
# tc commands for bandwidth shaping on VM tap interfaces.
# Lives in qm.py because the manifest points to a single root agent actions file.


def handle_tc_rate_limit(params):
    """Apply or remove bandwidth limits via tc on a VM tap interface.

    params:
        dev (str): Tap device name (must match tap{N}i{N})
        direction (str): 'ingress' or 'egress'
        rate_kbps (int): Rate limit in kbps (required unless remove=True)
        remove (bool): If true, remove the qdisc instead of creating
    """
    dev = params.get('dev', '')
    if not isinstance(dev, str) or not TAP_RE.match(dev):
        return {'ok': False, 'error': f'Invalid tap device: {dev}'}

    direction = params.get('direction', '')
    if direction not in ('ingress', 'egress'):
        return {'ok': False, 'error': f'direction must be ingress or egress: {direction}'}

    remove = bool(params.get('remove', False))

    if remove:
        # Remove qdisc — suppress "not found" errors
        if direction == 'egress':
            run(['tc', 'qdisc', 'del', 'dev', dev, 'root'], timeout=10)
        else:
            run(['tc', 'qdisc', 'del', 'dev', dev, 'ingress'], timeout=10)
        return {'ok': True, 'output': f'Removed {direction} limit on {dev}'}

    # Validate rate
    try:
        rate_kbps = int(params.get('rate_kbps', 0))
    except (ValueError, TypeError):
        return {'ok': False, 'error': 'rate_kbps must be a positive integer'}
    if rate_kbps < 1:
        return {'ok': False, 'error': f'rate_kbps must be > 0: {rate_kbps}'}

    rate = f'{rate_kbps}kbit'

    if direction == 'egress':
        rc, out, err = run(
            ['tc', 'qdisc', 'replace', 'dev', dev, 'root',
             'tbf', 'rate', rate, 'burst', '32kbit', 'latency', '400ms'],
            timeout=10,
        )
        if rc != 0:
            return {'ok': False, 'error': err or out}
    else:
        # Ingress: delete existing first for idempotency (E9), then add fresh
        run(['tc', 'qdisc', 'del', 'dev', dev, 'ingress'], timeout=10)

        rc, out, err = run(
            ['tc', 'qdisc', 'add', 'dev', dev, 'handle', 'ffff:', 'ingress'],
            timeout=10,
        )
        if rc != 0:
            return {'ok': False, 'error': f'Failed to add ingress qdisc: {err or out}'}

        rc, out, err = run(
            ['tc', 'filter', 'add', 'dev', dev, 'parent', 'ffff:',
             'protocol', 'all', 'u32', 'match', 'u32', '0', '0',
             'police', 'rate', rate, 'burst', '32kbit', 'drop', 'flowid', ':1'],
            timeout=10,
        )
        if rc != 0:
            return {'ok': False, 'error': f'Failed to add ingress filter: {err or out}'}

    return {'ok': True, 'output': f'Set {direction} rate limit {rate} on {dev}'}
```

- [ ] **Step 4: Register both actions in the ACTIONS dict**

In the `ACTIONS` dict at the bottom of the file, add:

```python
    'pve-set-throttle': handle_pve_set_throttle,
    'tc-rate-limit': handle_tc_rate_limit,
```

- [ ] **Step 5: Syntax check**

Run: `python3 -c "import ast; ast.parse(open('root-agent-actions/qm.py').read()); print('OK')"`

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add root-agent-actions/qm.py
git commit -m "Add pve-set-throttle and tc-rate-limit root agent actions"
```

---

## Task 4: Throttle script (`scripts/vm-throttle.py`)

**Files:**
- Create: `scripts/vm-throttle.py`

Implements contract section 2.13. Parses CLI args, translates to root agent calls.

- [ ] **Step 1: Create `scripts/vm-throttle.py`**

```python
#!/usr/bin/env python3
"""
VM Throttle — apply or remove resource limits on a running VM.

Called by blockhost-monitor when enforcement action is needed.
Delegates to root agent actions for privilege separation.

Usage:
    blockhost-vm-throttle <vm-name> [options]

Options:
    --cpu-shares N       CPU weight (1-10000, default 1024)
    --cpu-quota P        Hard CPU cap as % of allocated vCPUs (1-100)
    --bandwidth-in K     Inbound bandwidth limit in kbps (0 = unlimited)
    --bandwidth-out K    Outbound bandwidth limit in kbps (0 = unlimited)
    --iops-read N        Read IOPS limit (0 = unlimited)
    --iops-write N       Write IOPS limit (0 = unlimited)
    --reset              Remove all throttling, restore defaults
"""

import argparse
import sys

from blockhost.provisioner_proxmox import pve_api_get, load_pve_credentials
from blockhost.root_agent import RootAgentError, call
from blockhost.vm_db import get_database


def apply_cpu_throttle(vmid: int, config: dict, delete_keys: list = None):
    """Apply CPU/IOPS limits via pve-set-throttle root agent action."""
    params = {"vmid": vmid}
    if config:
        params["config"] = config
    if delete_keys:
        params["delete_keys"] = delete_keys
    call("pve-set-throttle", **params)


def apply_bandwidth(vmid: int, direction: str, rate_kbps: int):
    """Apply or remove bandwidth limit via tc-rate-limit root agent action."""
    tap_dev = f"tap{vmid}i0"
    if rate_kbps == 0:
        call("tc-rate-limit", dev=tap_dev, direction=direction, remove=True)
    else:
        call("tc-rate-limit", dev=tap_dev, direction=direction, rate_kbps=rate_kbps)


def main():
    parser = argparse.ArgumentParser(
        description="Apply or remove resource limits on a running VM",
    )
    parser.add_argument("vm_name", help="Name of the VM")
    parser.add_argument("--cpu-shares", type=int, help="CPU weight (1-10000)")
    parser.add_argument("--cpu-quota", type=int, help="CPU cap as %% of allocated vCPUs (1-100)")
    parser.add_argument("--bandwidth-in", type=int, help="Inbound bandwidth in kbps (0=unlimited)")
    parser.add_argument("--bandwidth-out", type=int, help="Outbound bandwidth in kbps (0=unlimited)")
    parser.add_argument("--iops-read", type=int, help="Read IOPS limit (0=unlimited)")
    parser.add_argument("--iops-write", type=int, help="Write IOPS limit (0=unlimited)")
    parser.add_argument("--reset", action="store_true", help="Remove all throttling")

    args = parser.parse_args()

    # Look up VM
    db = get_database()
    vm = db.get_vm(args.vm_name)
    if not vm:
        print(f"Error: VM '{args.vm_name}' not found", file=sys.stderr)
        return 1

    vmid = vm["vmid"]

    # Check VM is running
    try:
        creds = load_pve_credentials()
        _, _, node = creds
        status = pve_api_get(
            f"/api2/json/nodes/{node}/qemu/{vmid}/status/current",
            credentials=creds,
        )
        if status.get("status") != "running":
            print(f"Error: VM '{args.vm_name}' is not running (status: {status.get('status')})", file=sys.stderr)
            return 1
        cpu_count = status.get("cpus", 1)
    except Exception as e:
        print(f"Error querying VM status: {e}", file=sys.stderr)
        return 1

    if args.reset:
        # Reset all throttling
        try:
            apply_cpu_throttle(
                vmid,
                config={"cpuunits": 1024, "cpulimit": 0},
                delete_keys=["bps_rd", "bps_wr", "iops_rd", "iops_wr"],
            )
            print("Reset CPU/IOPS limits to defaults")
        except RootAgentError as e:
            print(f"Error resetting CPU/IOPS: {e}", file=sys.stderr)
            return 1

        tap_dev = f"tap{vmid}i0"
        try:
            call("tc-rate-limit", dev=tap_dev, direction="egress", remove=True)
            call("tc-rate-limit", dev=tap_dev, direction="ingress", remove=True)
            print("Reset bandwidth limits")
        except RootAgentError as e:
            print(f"Error resetting bandwidth: {e}", file=sys.stderr)
            return 1

        return 0

    # Check that at least one option was specified
    has_option = any([
        args.cpu_shares is not None,
        args.cpu_quota is not None,
        args.bandwidth_in is not None,
        args.bandwidth_out is not None,
        args.iops_read is not None,
        args.iops_write is not None,
    ])
    if not has_option:
        print("Error: specify at least one throttle option or --reset", file=sys.stderr)
        return 1

    # Build CPU/IOPS config
    pve_config = {}
    pve_delete = []

    if args.cpu_shares is not None:
        pve_config["cpuunits"] = args.cpu_shares

    if args.cpu_quota is not None:
        # Convert percentage of allocated vCPUs to PVE cpulimit (float, 0-N)
        cpulimit = cpu_count * (args.cpu_quota / 100)
        pve_config["cpulimit"] = round(cpulimit, 2)

    if args.iops_read is not None:
        if args.iops_read == 0:
            pve_delete.append("iops_rd")
        else:
            pve_config["iops_rd"] = args.iops_read

    if args.iops_write is not None:
        if args.iops_write == 0:
            pve_delete.append("iops_wr")
        else:
            pve_config["iops_wr"] = args.iops_write

    # Apply CPU/IOPS changes
    if pve_config or pve_delete:
        try:
            apply_cpu_throttle(vmid, config=pve_config, delete_keys=pve_delete or None)
            for key, value in pve_config.items():
                print(f"Set {key}={value}")
            for key in pve_delete:
                print(f"Removed {key} limit")
        except RootAgentError as e:
            print(f"Error applying CPU/IOPS limits: {e}", file=sys.stderr)
            return 1

    # Apply bandwidth changes
    if args.bandwidth_in is not None:
        try:
            apply_bandwidth(vmid, "ingress", args.bandwidth_in)
            if args.bandwidth_in == 0:
                print("Removed inbound bandwidth limit")
            else:
                print(f"Set inbound bandwidth limit: {args.bandwidth_in} kbps")
        except RootAgentError as e:
            print(f"Error applying inbound bandwidth: {e}", file=sys.stderr)
            return 1

    if args.bandwidth_out is not None:
        try:
            apply_bandwidth(vmid, "egress", args.bandwidth_out)
            if args.bandwidth_out == 0:
                print("Removed outbound bandwidth limit")
            else:
                print(f"Set outbound bandwidth limit: {args.bandwidth_out} kbps")
        except RootAgentError as e:
            print(f"Error applying outbound bandwidth: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Syntax check**

Run: `python3 -c "import ast; ast.parse(open('scripts/vm-throttle.py').read()); print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/vm-throttle.py
git commit -m "Add vm-throttle: apply resource limits via root agent"
```

---

## Task 5: Packaging and cleanup

**Files:**
- Delete: `scripts/vm-metrics.sh`, `scripts/vm-throttle.sh`
- Modify: `build-deb.sh` (lines 176-177, 191-192)

- [ ] **Step 1: Delete the old stubs**

```bash
git rm scripts/vm-metrics.sh scripts/vm-throttle.sh
```

- [ ] **Step 2: Update `build-deb.sh`**

Change line 176 from:
```bash
cp "${SCRIPT_DIR}/scripts/vm-metrics.sh" "${PKG}/usr/bin/blockhost-vm-metrics"
```
to:
```bash
cp "${SCRIPT_DIR}/scripts/vm-metrics.py" "${PKG}/usr/bin/blockhost-vm-metrics"
```

Change line 177 from:
```bash
cp "${SCRIPT_DIR}/scripts/vm-throttle.sh" "${PKG}/usr/bin/blockhost-vm-throttle"
```
to:
```bash
cp "${SCRIPT_DIR}/scripts/vm-throttle.py" "${PKG}/usr/bin/blockhost-vm-throttle"
```

No chmod changes needed — lines 191-192 already set 755 on the installed names.

- [ ] **Step 3: Build the .deb to verify**

Run: `./build-deb.sh`

Expected: `Package built successfully!`

- [ ] **Step 4: Commit**

```bash
git add scripts/vm-metrics.sh scripts/vm-throttle.sh build-deb.sh
git commit -m "Replace metrics/throttle stubs with Python implementations in .deb"
```

---

## Task 6: Update PROJECT.yaml and CLAUDE.md

**Files:**
- Modify: `PROJECT.yaml`
- Modify: `CLAUDE.md` (Key Files table)

- [ ] **Step 1: Update PROJECT.yaml**

Replace the `vm_metrics` stub entry with the real implementation details. Replace the `vm_throttle` stub entry similarly. Add `pve_set_throttle` and `tc_rate_limit` to `root_agent_operations`. Update the `Last updated` comment. Update the root_agent_actions section to include the new actions.

Key changes:
- `vm_metrics`: description, required_args, json_output schema reference
- `vm_throttle`: description, required_args, optional_args for all 7 options
- `root_agent_actions.actions`: add `pve-set-throttle` and `tc-rate-limit`
- `privilege_separation.root_agent_operations`: add both new actions

- [ ] **Step 2: Update CLAUDE.md Key Files table**

Change the vm-metrics and vm-throttle entries from `.sh` stubs to `.py` implementations:
- `scripts/vm-metrics.py` — Collect VM resource usage via Proxmox API
- `scripts/vm-throttle.py` — Apply/remove VM resource limits

- [ ] **Step 3: Syntax check PROJECT.yaml**

Run: `python3 -c "import yaml; yaml.safe_load(open('PROJECT.yaml')); print('OK')"`

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add PROJECT.yaml CLAUDE.md
git commit -m "Update docs: metrics/throttle implementations, new root agent actions"
```

---

## Task 7: Update facts submodule and final verification

**Files:**
- Submodule: `facts` (already on `feature/monitor`)

- [ ] **Step 1: Stage the facts submodule change**

The facts submodule was checked out to `origin/feature/monitor` at the start. Stage it:

```bash
git add facts
```

- [ ] **Step 2: Full syntax check on all modified Python files**

```bash
python3 -c "import ast; ast.parse(open('blockhost/provisioner_proxmox/__init__.py').read()); print('__init__.py OK')"
python3 -c "import ast; ast.parse(open('scripts/vm-metrics.py').read()); print('vm-metrics.py OK')"
python3 -c "import ast; ast.parse(open('scripts/vm-throttle.py').read()); print('vm-throttle.py OK')"
python3 -c "import ast; ast.parse(open('root-agent-actions/qm.py').read()); print('qm.py OK')"
```

Expected: All print `OK`.

- [ ] **Step 3: Build .deb**

Run: `./build-deb.sh`

Expected: `Package built successfully!`

- [ ] **Step 4: Commit facts submodule**

```bash
git commit -m "Update facts submodule to feature/monitor (metrics/throttle contract)"
```

- [ ] **Step 5: Push**

```bash
git push
```
