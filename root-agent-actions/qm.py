"""
Proxmox QM actions for the BlockHost root agent.

Shipped by blockhost-provisioner, loaded by the root agent daemon from
/usr/share/blockhost/root-agent-actions/.
"""

import os

from _common import (
    QM_CREATE_ALLOWED_ARGS,
    QM_SET_ALLOWED_KEYS,
    STORAGE_RE,
    log,
    run,
    validate_vmid,
)


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


ACTIONS = {
    'qm-start': lambda p: _handle_qm_simple(p, 'start'),
    'qm-stop': lambda p: _handle_qm_simple(p, 'stop'),
    'qm-shutdown': lambda p: _handle_qm_simple(p, 'shutdown', timeout=300),
    'qm-destroy': lambda p: _handle_qm_simple(p, 'destroy', extra_args=['--purge']),
    'qm-create': handle_qm_create,
    'qm-importdisk': handle_qm_importdisk,
    'qm-set': handle_qm_set,
    'qm-template': lambda p: _handle_qm_simple(p, 'template'),
}
