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
