#!/usr/bin/env python3
"""
VM Guest Exec — run a shell command inside a running VM.

General-purpose primitive used by the network hook (pushing .onion addresses,
updating /etc/hosts, updating signing URLs), the engine handler (GECOS updates),
and the admin panel.

The VM must be running with a responsive QEMU guest agent. The command is
executed via `qm guest exec <vmid> -- /bin/sh -c <command>` on the Proxmox
host (privileged; dispatched through the root agent).

Usage:
    blockhost-vm-guest-exec <name> <command...>

Behavior:
    - Resolves VM name → VMID via the VM database.
    - All remaining argv elements are joined with spaces to form the shell command.
    - Prints the inner command's stdout to stdout and stderr to stderr.
    - Exits with the inner command's exit code.
    - Exits non-zero (1) if the VM is not found, the agent is unresponsive,
      or the root agent call fails.
"""

import sys

from blockhost.root_agent import RootAgentError, call
from blockhost.vm_db import get_database


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: blockhost-vm-guest-exec <name> <command...>", file=sys.stderr)
        return 1

    vm_name = sys.argv[1]
    command = " ".join(sys.argv[2:])

    db = get_database()
    vm = db.get_vm(vm_name)
    if not vm:
        print(f"Error: VM '{vm_name}' not found", file=sys.stderr)
        return 1

    try:
        response = call("qm-guest-exec", vmid=vm["vmid"], command=command)
    except RootAgentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    stdout = response.get("stdout", "") or ""
    stderr = response.get("stderr", "") or ""

    if stdout:
        sys.stdout.write(stdout)
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()

    return int(response.get("exitcode", 0))


if __name__ == "__main__":
    sys.exit(main())
