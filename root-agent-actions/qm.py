"""
Proxmox QM actions for the BlockHost root agent.

Shipped by blockhost-provisioner, loaded by the root agent daemon from
/usr/share/blockhost/root-agent-actions/.
"""

import json
import re

from _common import (
    log,
    run,
    validate_vmid,
)

# Throttle-specific allowlist for pve-set-throttle.
THROTTLE_ALLOWED_KEYS = frozenset({
    'cpuunits', 'cpulimit', 'iops_rd', 'iops_wr',
})

# bps_rd/bps_wr included in deletable keys only — reset clears them
# in case they were set by an older version or manual intervention
THROTTLE_DELETABLE_KEYS = THROTTLE_ALLOWED_KEYS | frozenset({
    'bps_rd', 'bps_wr',
})

TAP_RE = re.compile(r'^tap\d+i\d+$')


def _handle_qm_simple(params, subcommand, extra_args=(), timeout=120):
    """Run a simple qm subcommand that only takes a VMID."""
    vmid = validate_vmid(params['vmid'])
    rc, out, err = run(
        ['qm', subcommand, str(vmid)] + list(extra_args),
        timeout=timeout,
    )
    if rc != 0:
        return {'ok': False, 'error': err or out}
    return {'ok': True, 'output': out}


def handle_qm_guest_exec(params):
    """Execute a shell command inside a running VM via qm guest exec.

    Generic primitive used by the network hook (onion addresses, signing URL
    updates) and by update-gecos. The command runs through /bin/sh -c so
    shell features (pipes, redirects, quoting) work as expected.

    params:
        vmid (int): VM ID
        command (str): Shell command to execute inside the VM

    Returns on outer success (qm guest exec itself ran):
        {ok: True, exitcode: int, stdout: str, stderr: str}
    Returns on outer failure (agent unresponsive, VM not found, parse error):
        {ok: False, error: str}
    """
    vmid = validate_vmid(params['vmid'])
    command = params.get('command', '')

    if not isinstance(command, str) or not command:
        return {'ok': False, 'error': 'command must be a non-empty string'}

    rc, out, err = run(
        ['qm', 'guest', 'exec', str(vmid), '--', '/bin/sh', '-c', command],
        timeout=300,
    )

    if rc != 0:
        return {'ok': False, 'error': (err or out or f'qm guest exec failed with rc={rc}').strip()}

    try:
        data = json.loads(out)
    except (ValueError, json.JSONDecodeError) as e:
        return {'ok': False, 'error': f'Failed to parse qm guest exec JSON output: {e}'}

    return {
        'ok': True,
        'exitcode': int(data.get('exitcode', 0)),
        'stdout': data.get('out-data', '') or '',
        'stderr': data.get('err-data', '') or '',
    }


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


ACTIONS = {
    'qm-start': lambda p: _handle_qm_simple(p, 'start'),
    'qm-stop': lambda p: _handle_qm_simple(p, 'stop'),
    'qm-shutdown': lambda p: _handle_qm_simple(p, 'shutdown', timeout=300),
    'qm-destroy': lambda p: _handle_qm_simple(p, 'destroy', extra_args=['--purge']),
    'qm-guest-exec': handle_qm_guest_exec,
    'pve-set-throttle': handle_pve_set_throttle,
    'tc-rate-limit': handle_tc_rate_limit,
}
