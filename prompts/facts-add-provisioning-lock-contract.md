# Prompt for the main session: document `/run/blockhost/provisioning.lock` in PROVISIONER_INTERFACE.md

## Context

Engine reconcilers (currently `blockhost-engine-opnet/src/reconcile/index.ts`, presumably the EVM/Cardano/Ergo engines too) need a reliable way to detect that a provisioner `create` operation is in flight, so they can defer reconciliation rather than racing against `vms.json` writes.

The OPNet session sent identical prompts to **both** provisioner sessions specifying:
- Path: `/run/blockhost/provisioning.lock`
- Contents: PID of the running create process
- Lifecycle: written at create start, removed on exit (success/failure/SIGINT/SIGTERM)
- Stale-detection: provisioner-side (check PID alive; remove if dead)

`blockhost-provisioner-proxmox` has implemented this as of the most recent commit on `main`. (The libvirt session is presumably implementing in parallel against the same prompt.)

This is now a **cross-provisioner contract**, not a per-provisioner detail. The OPNet prompt explicitly asked: "Add a paragraph to `facts/PROVISIONER_INTERFACE.md` describing the lock file." That edit needs to land in `facts/` so all engines/provisioners share one truth.

## What to add

Suggested location: a new subsection in `facts/PROVISIONER_INTERFACE.md` — either as a new top-level section after §6 (Systemd Units) or as part of §2 (CLI Commands) under a "Cross-cutting concerns" header. Wherever fits the document's style.

Suggested content:

```markdown
## Provisioning Lock

A file at `/run/blockhost/provisioning.lock` signals that a `create` operation
is in flight. Engine reconcilers MUST check this file before reading `vms.json`
and defer reconciliation while it is present.

| Aspect | Spec |
|--------|------|
| **Path** | `/run/blockhost/provisioning.lock` |
| **Contents** | Decimal PID of the create process (no trailing newline required) |
| **Owner / mode** | Whatever user the provisioner runs as (typically `blockhost`); permissions are `0644` from `Path.write_text` defaults |
| **Lifecycle** | Created when `create` command starts; removed on exit (normal, error, SIGINT, SIGTERM) |
| **Scope** | ONLY the `create` command takes this lock. Other commands (`destroy`, `start`, `resume`, `update-gecos`, `guest-exec`) do not. |
| **Stale-detection** | Provisioner-side. On acquire, if the file already exists and the recorded PID is dead, remove and proceed. If alive, abort with non-zero exit. |
| **Mock mode** | Provisioners SHOULD skip the lock when run with `--mock` (no real DB, no engine to race against). |

**Engine responsibilities:**
- Check `existsSync("/run/blockhost/provisioning.lock")` (or equivalent) before reconciliation reads `vms.json`.
- If present, defer reconciliation to the next cycle. Do not try to read the lock's PID — provisioner owns that semantics.
- Do not attempt to take or remove the lock. It is provisioner-owned.

**Why `/run/blockhost/`:** The directory is created (root:blockhost, mode 2775)
by the engine's systemd unit's `ExecStartPre`. Provisioners run as `blockhost`
and can write here. The `/run` location ensures the lock is cleared on host
reboot — a stale lock from a crashed create won't survive.
```

## Why this needs to land in facts

The OPNet engine session is blocked from removing its `pgrep` heuristic until the contract is in writing. Without the contract:
- Other engines (EVM, Cardano, Ergo) won't know to honor the lock
- Future provisioners might pick a different lock path
- The `/run/blockhost/` directory ownership is currently an engine-side detail; the contract needs to surface it as shared infrastructure

## Coordination

- **blockhost-provisioner-proxmox** has the lock implemented as of the most recent `main` commit (`scripts/vm-generator.py` — `_acquire_provisioning_lock` / `_release_provisioning_lock`).
- **blockhost-provisioner-libvirt** session received an analogous prompt; check whether they've landed it.
- After facts/ updates land, the OPNet session will replace its `pgrep`-based `isProvisioningInProgress()` with a pure lock-file check.

## Files to read for verification

- `blockhost-provisioner-proxmox/scripts/vm-generator.py` — reference implementation (search for `PROVISIONING_LOCK`)
- `blockhost-engine-opnet/src/reconcile/index.ts` — current `pgrep`-based code (the thing being replaced)
- The OPNet session's outgoing prompt (received by both provisioner sessions): `blockhost-opnet/prompts/blockhost-provisioner-proxmox-lock-file.md` and the libvirt sibling

## What to do

1. Add the lock-file section to `facts/PROVISIONER_INTERFACE.md`.
2. Push facts.
3. The submodule sessions will pull at their next checkpoint.
