"""
Proxmox QM actions for the BlockHost root agent.

Shipped by blockhost-provisioner, loaded by the root agent daemon from
/usr/share/blockhost/root-agent-actions/.
"""

import os
import re

from _common import (
    STORAGE_RE,
    log,
    run,
    validate_vmid,
)

# Input validation for qm-update-gecos
USERNAME_RE = re.compile(r'^[a-z_][a-z0-9_-]{0,31}$')
GECOS_RE = re.compile(r'^wallet=[a-zA-Z0-9]{1,128}(,nft=[0-9]{1,10})?$')

# Proxmox-specific allowlists — these belong in the provisioner, not in common.
QM_CREATE_ALLOWED_ARGS = frozenset({
    '--scsi0', '--boot', '--ide2', '--agent', '--serial0', '--vga',
    '--net0', '--memory', '--cores', '--name', '--ostype', '--scsihw',
    '--sockets', '--cpu', '--numa', '--machine', '--bios',
})

QM_SET_ALLOWED_KEYS = frozenset({
    'scsi0', 'boot', 'ide2', 'agent', 'serial0', 'vga',
    'net0', 'memory', 'cores', 'name', 'ostype', 'scsihw',
})

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


def handle_qm_create(params):
    """Create a new VM with validated arguments.

    params:
        vmid (int): VM ID
        args (list): List of [flag, value] pairs, e.g. [["--memory", "2048"]]
    """
    vmid = validate_vmid(params['vmid'])
    args = params.get('args', [])

    if not isinstance(args, list):
        return {'ok': False, 'error': 'args must be a list'}

    cmd = ['qm', 'create', str(vmid)]

    for item in args:
        if not isinstance(item, list) or len(item) != 2:
            return {'ok': False, 'error': f'Each arg must be [flag, value]: {item}'}
        flag, value = item[0], str(item[1])
        if flag not in QM_CREATE_ALLOWED_ARGS:
            return {'ok': False, 'error': f'Disallowed qm create arg: {flag}'}
        cmd.extend([flag, value])

    rc, out, err = run(cmd, timeout=120)
    if rc != 0:
        return {'ok': False, 'error': err or out}
    return {'ok': True, 'output': out}


def handle_qm_importdisk(params):
    """Import a disk image into a VM.

    params:
        vmid (int): VM ID
        image_path (str): Path to disk image (must be under /var/lib/blockhost/ or /tmp/)
        storage (str): Proxmox storage name
    """
    vmid = validate_vmid(params['vmid'])
    image_path = params.get('image_path', '')
    storage = params.get('storage', '')

    if not isinstance(image_path, str) or not image_path:
        return {'ok': False, 'error': 'image_path is required'}
    image_path = os.path.realpath(image_path)
    if not (image_path.startswith('/var/lib/blockhost/') or image_path.startswith('/tmp/')):
        return {'ok': False, 'error': 'image_path must be under /var/lib/blockhost/ or /tmp/'}
    if not os.path.isfile(image_path):
        return {'ok': False, 'error': f'Image not found: {image_path}'}

    if not isinstance(storage, str) or not STORAGE_RE.match(storage):
        return {'ok': False, 'error': f'Invalid storage name: {storage}'}

    rc, out, err = run(
        ['qm', 'importdisk', str(vmid), image_path, storage],
        timeout=600,
    )
    if rc != 0:
        return {'ok': False, 'error': err or out}
    return {'ok': True, 'output': out}


def handle_qm_set(params):
    """Set VM configuration with validated keys.

    params:
        vmid (int): VM ID
        config (dict): Key-value pairs, e.g. {"memory": "2048", "cores": "2"}
    """
    vmid = validate_vmid(params['vmid'])
    config = params.get('config', {})

    if not isinstance(config, dict) or not config:
        return {'ok': False, 'error': 'config must be a non-empty dict'}

    cmd = ['qm', 'set', str(vmid)]

    for key, value in config.items():
        if key not in QM_SET_ALLOWED_KEYS:
            return {'ok': False, 'error': f'Disallowed qm set key: {key}'}
        cmd.extend([f'--{key}', str(value)])

    rc, out, err = run(cmd, timeout=120)
    if rc != 0:
        return {'ok': False, 'error': err or out}
    return {'ok': True, 'output': out}


def handle_qm_update_gecos(params):
    """Update a VM user's GECOS field via qm guest exec.

    params:
        vmid (int): VM ID
        username (str): Linux username to update
        gecos (str): New GECOS value (format: wallet=<addr> or wallet=<addr>,nft=<id>)
    """
    vmid = validate_vmid(params['vmid'])

    username = params.get('username', '')
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        return {'ok': False, 'error': f'Invalid username: {username}'}

    gecos = params.get('gecos', '')
    if not isinstance(gecos, str) or not GECOS_RE.match(gecos):
        return {'ok': False, 'error': f'Invalid gecos: {gecos}'}

    rc, out, err = run(
        ['qm', 'guest', 'exec', str(vmid), '--', 'usermod', '-c', gecos, username],
        timeout=30,
    )
    if rc != 0:
        return {'ok': False, 'error': err or out}
    return {'ok': True, 'output': out}


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
    'qm-create': handle_qm_create,
    'qm-importdisk': handle_qm_importdisk,
    'qm-set': handle_qm_set,
    'qm-template': lambda p: _handle_qm_simple(p, 'template'),
    'qm-update-gecos': handle_qm_update_gecos,
    'pve-set-throttle': handle_pve_set_throttle,
    'tc-rate-limit': handle_tc_rate_limit,
}
