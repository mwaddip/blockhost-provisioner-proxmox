# Prompt for the blockhost-common session: add `delete_vm(name)` to VMDatabaseBase

## Context

A simplify-code review of `blockhost-provisioner-proxmox` (commit on `main`, 2026-04-28) tightened the existence check in `vm-generator.py` so that creating a VM with the name of an existing record (in any status — active, suspended, **or destroyed**) now returns a clean error instead of either silently overwriting or exploding inside `register_vm`.

The "destroyed" case is the one that needs your help. The intent was to allow **destroyed-name reuse** — i.e. after a VM is destroyed, the operator should be able to create a new VM with the same name. Today this is impossible because `register_vm` rejects any existing key:

```python
# blockhost/vm_db.py, line ~200
def mutator(db):
    if name in db["vms"]:
        raise ValueError(f"VM '{name}' already exists")
```

So a destroyed record acts as a permanent name lock. To unblock destroyed-name reuse, the provisioner needs a primitive to actually remove a record.

## What to add

Add a `delete_vm(name)` method to `VMDatabaseBase` in `blockhost/vm_db.py` (and to the matching docs in `blockhost-common/facts/COMMON_INTERFACE.md` if that section enumerates DB methods). Suggested implementation:

```python
def delete_vm(self, name: str) -> None:
    """Permanently remove a VM record from the database.

    Releases any IPv4/IPv6 allocations still attached to the record
    (in case the caller is using delete_vm without a prior mark_destroyed).

    Raises:
        ValueError: If no record with that name exists.
    """
    def mutator(db):
        if name not in db["vms"]:
            raise ValueError(f"VM '{name}' not found")

        vm = db["vms"][name]

        ip = vm.get("ip_address")
        if ip and ip in db["allocated_ips"]:
            db["allocated_ips"].remove(ip)

        ipv6 = vm.get("ipv6_address")
        if ipv6 and ipv6 in db.get("allocated_ipv6", []):
            db["allocated_ipv6"].remove(ipv6)

        del db["vms"][name]

    self._atomic_update(mutator)
```

No new fields, no schema migration — just a record removal that releases IPs (so calling `delete_vm` on an `active` record cleans up too, not only on `destroyed`).

## Caller intent (proxmox provisioner)

After this lands, `blockhost-provisioner-proxmox`'s `vm-generator.py` will be updated like:

```python
existing = db.get_vm(args.name)
if existing:
    if existing.get("status") in ("active", "suspended"):
        print(f"Error: VM '{args.name}' already exists (status={existing.get('status')})")
        sys.exit(1)
    # status == "destroyed" — clean it out so register_vm succeeds
    db.delete_vm(args.name)
```

The libvirt provisioner has the same shape and will adopt the same pattern.

## Why not just relax `register_vm`?

Two reasons to keep `register_vm` strict and add `delete_vm` as the explicit primitive:
1. `register_vm`'s rejection catches genuine bugs (concurrent create races, accidental name reuse). Loosening it would lose that signal.
2. The provisioner WANTS to make a deliberate choice about whether to clobber a destroyed record. An explicit `delete_vm` makes that choice visible at the call site.

## Verification

The proxmox provisioner currently has this comment in `scripts/vm-generator.py` (added in the simplify-code commit):

```python
# (Note: destroyed-name reuse hits a hard reject in register_vm because
# blockhost-common's VM database doesn't expose a delete_vm primitive yet.
# Treat destroyed records the same as active/suspended for now.)
```

Once `delete_vm` ships in common, that comment + the `"destroyed"` branch in the existence check can be removed in the proxmox session.

## Files to read

- `blockhost/vm_db.py` — `VMDatabaseBase` (~line 127), `register_vm` (~line 175), `mark_destroyed` (~line 274)
- `facts/COMMON_INTERFACE.md` — if it documents the DB API surface, add a row for `delete_vm`
