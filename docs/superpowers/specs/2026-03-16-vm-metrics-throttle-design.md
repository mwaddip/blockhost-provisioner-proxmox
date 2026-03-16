# VM Metrics & Throttle — Design Spec

**Date:** 2026-03-16
**Status:** Approved
**Contract:** `facts/PROVISIONER_INTERFACE.md` sections 2.12, 2.13
**SPECIAL profile:** S9 P7 E9 C5 I7 A6 L7 (provisioner default)

## Overview

Implement `blockhost-vm-metrics` and `blockhost-vm-throttle` commands, replacing the current stubs. These are consumed by the blockhost-monitor component for resource enforcement.

## Shared Infrastructure

### `blockhost/provisioner_proxmox/__init__.py`

Add two helpers alongside the existing `get_terraform_dir()` and `sanitize_resource_name()`:

**`load_pve_credentials() -> tuple[str, str, str]`**
- Returns `(api_url, api_token, node_name)`
- `api_token`: read from `/etc/blockhost/pve-token` (format `user@realm!tokenid=secret`)
- `api_url`: `https://127.0.0.1:8006`
- `node_name`: from `terraform.tfvars` → `proxmox_node`, fallback `socket.gethostname()`

**`pve_api_get(path: str, credentials: tuple = None, timeout: int = 5) -> dict`**
- GET a Proxmox API endpoint, return parsed JSON `data` key
- Uses `urllib.request` with unverified SSL context (localhost self-signed)
- Sets `Authorization: PVEAPIToken=<token>` header
- `credentials`: optional `(api_url, api_token, node_name)` tuple — if None, calls `load_pve_credentials()`
- `timeout`: HTTP timeout in seconds (default 5) — prevents blocking when PVE is overloaded
- Raises on HTTP errors

## Metrics: `scripts/vm-metrics.py`

### Data Sources (3 API calls max)

1. **`/api2/json/nodes/{node}/qemu/{vmid}/status/current`**
   CPU %, mem used/total, netin/netout (cumulative), uptime, status, cpus count.

2. **`/api2/json/nodes/{node}/qemu/{vmid}/rrddata?timeframe=hour`**
   Array of samples with `diskread`, `diskwrite`, `netin`, `netout` as rates (bytes/sec).
   Take the last non-NaN sample for current rates.

3. **`/api2/json/nodes/{node}/qemu/{vmid}/agent/get-fsinfo`**
   Guest agent filesystem info. 3-second timeout — returns -1 for agent-dependent
   fields if unresponsive. Sets `guest_agent_responsive: false`.

### Field Mapping

| Contract field | Source | Derivation |
|---|---|---|
| `cpu_percent` | status/current → `cpu` | `cpu * 100 * cpu_count` — see CPU note below |
| `cpu_count` | status/current → `cpus` | Direct |
| `memory_used_mb` | status/current → `mem` | `mem / 1048576` |
| `memory_total_mb` | status/current → `maxmem` | `maxmem / 1048576` |
| `disk_used_mb` | agent/get-fsinfo | Sum `used-bytes` across filesystems, `/1048576`. -1 if agent down |
| `disk_total_mb` | agent/get-fsinfo or status/current | Sum `total-bytes` from agent if responsive, else `maxdisk / 1048576` |
| `disk_read_iops` | N/A | 0 (Proxmox standard API doesn't expose per-VM IOPS) |
| `disk_write_iops` | N/A | 0 |
| `disk_read_bytes_sec` | rrddata → `diskread` | Last sample |
| `disk_write_bytes_sec` | rrddata → `diskwrite` | Last sample |
| `net_rx_bytes_sec` | rrddata → `netin` | Last sample |
| `net_tx_bytes_sec` | rrddata → `netout` | Last sample |
| `net_connections` | N/A | -1 (would need agent exec + ss parsing; not worth the latency) |
| `guest_agent_responsive` | agent/get-fsinfo success/fail | bool |
| `uptime_seconds` | status/current → `uptime` | Direct |
| `state` | status/current → `status` | Map: running/paused/stopped/unknown |

### CPU Note

PVE returns `cpu` as a fraction of total allocated CPU (0.0–1.0 where 1.0 = all vCPUs at 100%).
The contract wants top-style values (200 = 2 cores at 100%), so the formula is `cpu * 100 * cpu_count`.
If the raw `cpu` value exceeds 1.0 at runtime, log a warning — this would indicate PVE changed semantics.

### Disk Total Note

When guest agent is responsive, `disk_total_mb` is derived from `get-fsinfo` (sum of `total-bytes`)
for consistency with `disk_used_mb`. Falls back to `maxdisk` (block device size) when agent is down.
Consumers must treat `disk_used_mb == -1` as "unavailable" and skip percentage calculations.

### Known Gaps

- `disk_read_iops`, `disk_write_iops` → 0. Proxmox standard API doesn't expose per-VM IOPS.
- `net_connections` → -1. Would need guest agent exec + `ss` parsing, violates "must be cheap".

Both documented as best-effort in the contract.

### Flow

1. Look up VM in database → get vmid
2. Load PVE credentials once via `load_pve_credentials()`
3. Call status/current API (pass credentials to `pve_api_get`)
4. Call rrddata API (extract last valid sample)
5. Call agent/get-fsinfo (3s timeout, non-fatal)
6. Assemble JSON per contract schema
7. Print to stdout, exit 0

Exit 1 if VM not found or status call fails (stderr has reason).

## Throttle: `scripts/vm-throttle.py`

### CLI Options (per contract)

| Option | Root agent action | Param |
|---|---|---|
| `--cpu-shares N` | `pve-set-throttle` | `cpuunits=N` |
| `--cpu-quota P` | `pve-set-throttle` | `cpulimit=cpu_count * P/100` |
| `--iops-read N` | `pve-set-throttle` | If N>0: `iops_rd=N`. If N=0: `delete_keys=['iops_rd']` |
| `--iops-write N` | `pve-set-throttle` | If N>0: `iops_wr=N`. If N=0: `delete_keys=['iops_wr']` |
| `--bandwidth-in K` | `tc-rate-limit` | If K>0: `dev=tap{vmid}i0, direction=ingress, rate_kbps=K`. If K=0: `remove=True, direction=ingress` |
| `--bandwidth-out K` | `tc-rate-limit` | If K>0: `dev=tap{vmid}i0, direction=egress, rate_kbps=K`. If K=0: `remove=True, direction=egress` |
| `--reset` | Both | Reset params |

Options are additive — only specified limits are changed.

**Zero = unlimited:** The contract defines 0 as "remove limit" for bandwidth and IOPS options.
The script translates 0 into the appropriate removal action (tc qdisc delete, qm set --delete).

### Flow

1. Parse args, look up VM in db → get vmid + cpu_count
2. Validate VM is running (throttle on stopped VM is pointless)
3. If `--reset`: call both root agent actions with reset params, print confirmations
4. Otherwise: for each specified option, translate value (0 → removal) and call appropriate root agent action
5. Print one line per applied change to stdout
6. Exit 0 on success, 1 on failure

### Reset Behavior

- CPU/IOPS: `qm set {vmid} --cpuunits 1024 --cpulimit 0 --delete bps_rd,bps_wr,iops_rd,iops_wr`
- Bandwidth: remove tc qdiscs on tap interface

## Root Agent Actions

### `pve-set-throttle` (in `qm.py`)

Applies CPU and IOPS limits via `qm set` with a dedicated allowlist.

```python
THROTTLE_ALLOWED_KEYS = frozenset({
    'cpuunits', 'cpulimit', 'iops_rd', 'iops_wr',
})

# bps_rd/bps_wr included in deletable keys only — reset clears them in case
# they were set by an older version or manual intervention
THROTTLE_DELETABLE_KEYS = THROTTLE_ALLOWED_KEYS | frozenset({
    'bps_rd', 'bps_wr',
})
```

**Validation:**
- `cpuunits`: int, 1–10000
- `cpulimit`: float, 0–128 (0 = unlimited)
- `iops_rd/iops_wr`: int, >= 0
- `delete_keys`: optional list, subset of `THROTTLE_DELETABLE_KEYS`

Runs: `qm set {vmid} [--key value ...] [--delete key,key,...]`

### `tc-rate-limit` (in `qm.py`)

Applies bandwidth limits via `tc` on the VM's tap interface.
Note: `tc` is not a `qm` command, but lives in `qm.py` because the manifest points to a single
root agent actions file. A clear section comment separates it from the QM actions.

**Validation:**
- `dev`: must match `TAP_RE = re.compile(r'^tap\d+i\d+$')`
- `direction`: `ingress` or `egress`
- `rate_kbps`: int, > 0
- `remove`: bool — delete qdiscs instead of creating

**Commands:**
- Egress: `tc qdisc replace dev {dev} root tbf rate {rate}kbit burst 32kbit latency 400ms` (idempotent via `replace`)
- Ingress: delete existing ingress qdisc first (suppress errors), then `tc qdisc add dev {dev} handle ffff: ingress` + `tc filter add ... police rate {rate}kbit burst 32kbit drop`. This makes ingress idempotent (E9).
- Remove egress: `tc qdisc del dev {dev} root` (suppress "not found" errors)
- Remove ingress: `tc qdisc del dev {dev} ingress` (suppress "not found" errors)

### Security (S9 P7)

- `dev` validated against `TAP_RE` — prevents injection via device name
- Throttle keys use a separate allowlist from `QM_SET_ALLOWED_KEYS` — throttle action can't set arbitrary VM config
- `rate_kbps` validated as positive int — prevents `tc` argument injection
- `cpulimit` capped at 128

## File Changes

| File | Change |
|---|---|
| `blockhost/provisioner_proxmox/__init__.py` | Add `load_pve_credentials()`, `_pve_api_get()` |
| `scripts/vm-metrics.py` | New — replaces `vm-metrics.sh` stub |
| `scripts/vm-throttle.py` | New — replaces `vm-throttle.sh` stub |
| `root-agent-actions/qm.py` | Add `pve-set-throttle`, `tc-rate-limit` actions |
| `build-deb.sh` | Update lines to copy `.py` instead of `.sh` for metrics/throttle |
| `PROJECT.yaml` | Update `vm_metrics`, `vm_throttle` entries, document new root agent actions |
| `scripts/vm-metrics.sh` | Delete |
| `scripts/vm-throttle.sh` | Delete |

**Not changed:** `provisioner.json` (already declares both commands), `blockhost-common` (no changes needed).
