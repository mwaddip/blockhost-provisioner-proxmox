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
