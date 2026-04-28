# Prompt for the main session: update PROVISIONER_INTERFACE.md action table

## Context

The action table in `facts/PROVISIONER_INTERFACE.md` §4 ("Action Sets per Provisioner") is drifted from reality on **both** provisioner columns. A simplify-code review of `blockhost-provisioner-proxmox` (commit `16a7831`'s parent → newer) deleted dead actions from `qm.py`; verifying against `blockhost-provisioner-libvirt/root-agent-actions/virsh.py` shows libvirt never shipped several rows the table claims.

This is a contract-only change. No code update is needed in either provisioner — they already match what the new table should describe.

## What to change

Edit `facts/PROVISIONER_INTERFACE.md` §4. The current table (around lines 558-573):

```
| Action | Proxmox (`qm.py`) | libvirt (`virsh.py`) |
|--------|:-----------------:|:--------------------:|
| start | `qm-start` | `virsh-start` |
| stop / shutdown | `qm-stop`, `qm-shutdown` | `virsh-shutdown` |
| destroy / undefine | `qm-destroy` | `virsh-destroy`, `virsh-undefine` |
| reboot | — | `virsh-reboot` |
| create / define | `qm-create` | `virsh-define` |
| import disk | `qm-importdisk` | — |
| set config | `qm-set` | — |
| template | `qm-template` | — |
| update GECOS | `qm-update-gecos` | `virsh-update-gecos` |
| bandwidth throttle | `pve-set-throttle`, `tc-rate-limit` | — (not yet ported) |
```

Reality (as of 2026-04-28) — verified by reading each provisioner's `ACTIONS` dict:

**Proxmox `root-agent-actions/qm.py` ACTIONS:**
`qm-start`, `qm-stop`, `qm-shutdown`, `qm-destroy`, `qm-guest-exec`, `pve-set-throttle`, `tc-rate-limit`

**libvirt `root-agent-actions/virsh.py` ACTIONS:**
`virsh-start`, `virsh-destroy`, `virsh-shutdown`, `virsh-define`, `virsh-undefine`, `virsh-guest-exec`

Replace the table with something like:

```
| Action | Proxmox (`qm.py`) | libvirt (`virsh.py`) |
|--------|:-----------------:|:--------------------:|
| start | `qm-start` | `virsh-start` |
| graceful shutdown | `qm-shutdown` | `virsh-shutdown` |
| force stop | `qm-stop` | `virsh-destroy` *(libvirt naming)* |
| destroy / undefine | `qm-destroy` *(`--purge`)* | `virsh-undefine` |
| define / create | — *(build-template runs `qm` directly)* | `virsh-define` |
| guest exec | `qm-guest-exec` | `virsh-guest-exec` |
| bandwidth/IO throttle | `pve-set-throttle`, `tc-rate-limit` | — *(uses cgroups via virsh schedinfo, no root-agent action needed)* |
```

Notable removals (these were in the table but neither provisioner ships them):
- **`reboot`** row — not in libvirt's ACTIONS; not needed by Proxmox either
- **`update GECOS`** row — both provisioners' `vm-update-gecos` CLIs delegate to `*-guest-exec`, which is already in the table
- **`import disk`**, **`set config`**, **`template`** rows — Proxmox column was used only by the template builder, which now runs `qm` directly via shell; libvirt never had analogues

## Why this change

`update GECOS` and `reboot` rows imply features that don't exist in either provisioner. Anyone implementing a third provisioner against this table would build things no one calls. The `qm-create`/`importdisk`/`set`/`template` rows survived from a refactor that moved template building from root-agent dispatch to direct shell (build-template runs as root from finalize_template, so it doesn't need the privilege boundary).

## Coordination

After updating `facts/PROVISIONER_INTERFACE.md`, push the facts repo, then in the proxmox and libvirt submodule sessions, pull the updated facts pointer. The submodule code already matches; no further changes needed there.

## Files to read for verification

- `blockhost-provisioner-proxmox/root-agent-actions/qm.py` — current `ACTIONS` dict (~line 152)
- `blockhost-provisioner-libvirt/root-agent-actions/virsh.py` — current `ACTIONS` dict (~line 173)
- `blockhost-provisioner-proxmox/scripts/vm-update-gecos.sh` — confirms it delegates to `blockhost-vm-guest-exec`
- `blockhost-provisioner-libvirt/scripts/vm-update-gecos.py` — same delegation pattern
