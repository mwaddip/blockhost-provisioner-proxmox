# Provisioner Interface Specification

> Authoritative reference for implementing a provisioner backend.
> Derived from the working Proxmox implementation — these are the actual
> signatures, output formats, and integration points that consumers depend on.
>
> See also: `blockhost-common/provisioner-contract.md` (lighter overview).

---

## 1. Manifest

**Installed to:** `/usr/share/blockhost/provisioner.json`

The manifest is the single discovery mechanism. Every consumer finds the provisioner through this file. If it exists, the provisioner is active. If not, first-boot fails hard (intentionally — no hypervisor means no point continuing).

### Schema

```json
{
  "name":         "<machine-id>",
  "version":      "<semver>",
  "display_name": "<human-readable>",

  "commands": {
    "create":         "<executable-name>",
    "destroy":        "<executable-name>",
    "start":          "<executable-name>",
    "stop":           "<executable-name>",
    "kill":           "<executable-name>",
    "status":         "<executable-name>",
    "list":           "<executable-name>",
    "metrics":        "<executable-name>",
    "throttle":       "<executable-name>",
    "build-template": "<executable-name>",
    "gc":             "<executable-name>",
    "resume":         "<executable-name>"
  },

  "setup": {
    "first_boot_hook":    "<absolute-path>",
    "detect":             "<executable-name>",
    "wizard_module":      "<python.module.path>",
    "finalization_steps": ["<step_id>", "..."]
  },

  "root_agent_actions": "<absolute-path-to-py>",

  "config_keys": {
    "session_key":        "<flask-session-key>",
    "provisioner_config": ["<db.yaml key>", "..."]
  }
}
```

### Field Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Machine-readable ID. Used in logs and config. No spaces. |
| `version` | string | yes | Package version (semver). |
| `display_name` | string | yes | Shown in wizard UI. |
| `commands` | object | yes | Maps verb → CLI executable in `$PATH`. |
| `setup.first_boot_hook` | string | yes | Absolute path to first-boot script. |
| `setup.detect` | string | yes | CLI command; exit 0 = platform present. |
| `setup.wizard_module` | string | yes | Python module path for dynamic import. |
| `setup.finalization_steps` | list | yes | Ordered step IDs run during wizard finalization. |
| `root_agent_actions` | string | yes | Absolute path to root agent plugin `.py` file. |
| `config_keys.session_key` | string | yes | Flask session key where wizard stores provisioner config. |
| `config_keys.provisioner_config` | list | yes | Keys this provisioner owns in `db.yaml`. |

### Consumers

| Consumer | File | What it reads |
|----------|------|---------------|
| First-boot | `scripts/first-boot.sh` | `setup.first_boot_hook` — runs it. Hard failure if manifest missing. |
| Installer/wizard | `installer/web/app.py` | `setup.wizard_module` (Blueprint import), `setup.finalization_steps`, `config_keys.session_key`, `display_name` |
| Engine | `blockhost-engine/src/provisioner.ts` | `commands.*` — resolves verb to executable via `getCommand()` |
| Common dispatcher | `blockhost-common/.../provisioner.py` | `commands.*` — `ProvisionerDispatcher` caches manifest, provides `get_command()` |
| Root agent daemon | `blockhost-common/.../root_agent_daemon.py` | `root_agent_actions` — loads module, merges `ACTIONS` dict |

---

## 2. CLI Commands

All commands are resolved through the manifest. The engine calls `getCommand("create")` → gets executable name → spawns process. Consumers never hardcode command names.

### Common Conventions

- Exit 0 = success, non-zero = failure
- stdout = structured output when applicable (JSON for `create`, `list`)
- stderr = human-readable progress/error text
- All commands receive **VM name** as the primary identifier (not VMID — the name is hypervisor-agnostic)

---

### `create`

The most complex command. Creates a VM, optionally mints an NFT.

**Actual signature:**
```
blockhost-vm-create <name>
    --owner-wallet <0x...>
    [--cpu N]
    [--memory N]
    [--disk N]
    [--apply]
    [--cloud-init-content <path>]
    [--skip-mint]
    [--no-mint]
    [--user-signature <hex>]
    [--public-secret <str>]
    [--mock]
```

| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `name` | yes | — | VM name (positional). Used as primary key everywhere. |
| `--owner-wallet` | yes | — | Ethereum address (0x...). Owner of the access NFT. |
| `--cpu` | no | 1 | vCPU count. |
| `--memory` | no | 2048 | Memory in MB. |
| `--disk` | no | 20 | Disk in GB. |
| `--apply` | no | false | Actually create. Without this, dry-run (plan only). |
| `--cloud-init-content` | no | — | Path to pre-rendered cloud-init YAML. If absent, provisioner renders its own using `blockhost.cloud_init.render_cloud_init()`. |
| `--skip-mint` | no | false | Don't mint NFT (legacy flag). |
| `--no-mint` | no | false | Don't mint NFT (engine handles minting separately). **This is the flag the engine uses.** |
| `--user-signature` | no | — | User's wallet signature (hex). Enables encrypted connection details in NFT. |
| `--public-secret` | no | — | Signed message (`libpam-web3:<address>:<nonce>`). Required with `--user-signature`. |
| `--mock` | no | false | Use mock database. |

**stdout on success (JSON):**
```json
{
  "status": "ok",
  "vm_name": "myvm",
  "ip": "192.168.1.100",
  "ipv6": "2001:db8::100",
  "vmid": 100,
  "nft_token_id": 42,
  "username": "user"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | `"ok"` | Always `"ok"` on success. |
| `vm_name` | string | The name passed in. Echo back for confirmation. |
| `ip` | string | Assigned IPv4 address. |
| `ipv6` | string | Assigned IPv6 address (may be empty if broker unavailable). |
| `vmid` | int or string | Hypervisor-specific VM ID. Integer for Proxmox, domain name for libvirt. |
| `nft_token_id` | int | Reserved NFT token ID (even if minting was skipped). |
| `username` | string | SSH username created in the VM. |

**The engine depends on:** `status`, `vm_name`, `ip`, `ipv6`, `vmid`, `nft_token_id`, `username`. All fields must be present.

**Exit:** 0 on success, 1 on failure (stderr has error details).

---

### `destroy`

```
blockhost-vm-destroy <name>
```

Must be **idempotent** — destroying an already-destroyed VM is not an error. Cleans up all resources: VM definition, storage, cloud-init artifacts, IP allocation, database record.

**stdout:** Progress text.
**Exit:** 0/1.

---

### `start`

```
blockhost-vm-start <name>
```

Start a stopped/suspended VM. Typically delegates to root agent for the privileged operation.

**stdout:** Progress text.
**Exit:** 0/1.

---

### `stop`

```
blockhost-vm-stop <name>
```

**Graceful** shutdown. Sends ACPI signal, waits for OS to shut down.

**stdout:** Progress text.
**Exit:** 0/1.

---

### `kill`

```
blockhost-vm-kill <name>
```

**Immediate** power off. No graceful shutdown.

**stdout:** Progress text.
**Exit:** 0/1.

---

### `status`

```
blockhost-vm-status <name>
```

**stdout:** Exactly one of four strings:
- `active` — VM is running
- `suspended` — VM exists but is stopped/paused
- `destroyed` — VM has been removed
- `unknown` — Cannot determine state

**Exit:** Always 0. Status is communicated via stdout, not exit code.

---

### `list`

```
blockhost-vm-list [--format json]
```

**stdout (default):** Tab-separated columns: `NAME\tSTATUS\tIP\tCREATED`

**stdout (`--format json`):**
```json
[
  {"name": "vm1", "status": "active", "ip": "192.168.1.100", "created": "2026-02-09"},
  {"name": "vm2", "status": "suspended", "ip": "192.168.1.101", "created": "2026-02-08"}
]
```

**Exit:** Always 0.

---

### `gc`

```
blockhost-vm-gc [--execute] [--suspend-only] [--destroy-only] [--grace-days N] [--mock]
```

Two-phase garbage collection:
1. **Suspend phase:** VMs past NFT expiry → graceful shutdown
2. **Destroy phase:** VMs past expiry + grace period → full cleanup

**Dry-run by default.** Must pass `--execute` to actually perform actions.

**stdout:** Phase counts (e.g., `Suspend: 3 candidates`, `Destroy: 1 candidate`).
**Exit:** 0/1.

---

### `resume`

```
blockhost-vm-resume <name> [--extend-days N] [--mock] [--dry-run]
```

Resume a suspended VM. Optionally extend its subscription.

**stdout:** Progress text.
**Exit:** 0/1.

---

### `build-template`

```
blockhost-build-template
```

Build or update the base VM template/image. What this means is hypervisor-specific:
- Proxmox: Creates a Proxmox VM template from a cloud image + libpam-web3
- libvirt: Creates a qcow2 base image customized with libguestfs

**stdout:** Progress text.
**Exit:** 0/1.

---

### `metrics` (stub)

```
blockhost-vm-metrics <name>
```

Collect VM resource usage. Currently unimplemented in all provisioners.

**Exit:** 0 (even as stub).

---

### `throttle` (stub)

```
blockhost-vm-throttle <name>
```

Apply resource limits. Currently unimplemented in all provisioners.

**Exit:** 0 (even as stub).

---

### `detect`

```
blockhost-provisioner-detect
```

No arguments, no stdout. **Exit code only:**
- 0 = this provisioner's platform is available
- 1 = not available

Used by the installer to auto-detect which provisioner to use (future: when multiple provisioner packages are installed).

---

## 3. Wizard Plugin

The provisioner contributes a configuration page and finalization steps to the installer wizard.

### Module Path

Declared in `setup.wizard_module`. The installer imports this module dynamically:

```python
module = importlib.import_module(manifest["setup"]["wizard_module"])
```

### Required Exports

| Export | Type | Signature |
|--------|------|-----------|
| `blueprint` | `flask.Blueprint` | Registers provisioner wizard route(s) |
| `get_finalization_steps()` | function | `-> list[tuple[str, str, callable]]` |
| `get_summary_data(session)` | function | `-> dict` |
| `get_summary_template()` | function | `-> str` |

**Note:** The export is `blueprint`, not `wizard_bp`. The installer accesses `module.blueprint`.

### Blueprint

Must register at least one route for the provisioner config page. Convention: `/wizard/<provisioner-name>`.

```python
blueprint = Blueprint(
    "provisioner_<name>",
    __name__,
    template_folder="templates",
)

@blueprint.route("/wizard/<name>", methods=["GET", "POST"])
def wizard_page():
    # On GET: render config form with detected defaults
    # On POST: save to session[config_keys.session_key], redirect to next step
```

The POST handler must:
1. Save all provisioner config to `session[<session_key>]` as a dict
2. Redirect to `url_for("wizard_ipv6")` (the next step after provisioner config)

Templates must extend `base.html` and use the `step_bar` macro.

### Finalization Steps

`get_finalization_steps()` returns an ordered list of tuples:

```python
[
    ("step_id", "Human-readable description", finalize_function),
    ("storage", "Configuring storage pool", finalize_storage),
    ...
]
```

The step IDs must match the `setup.finalization_steps` array in the manifest.

**Finalization function signature:**
```python
def finalize_<step_id>(config: dict) -> tuple[bool, Optional[str]]:
    """
    config: the full Flask session dict (all wizard steps, not just provisioner)

    Returns:
        (True, None)              — success
        (False, "error message")  — failure
    """
```

The installer calls these in order during the summary page finalization pipeline. Each step is shown to the user with a progress indicator. On failure, the pipeline stops and shows the error.

### Summary

`get_summary_data(session)` receives the Flask session dict and returns a dict of key-value pairs to display on the review page.

`get_summary_template()` returns the path to an HTML template (relative to the Blueprint's template folder) that renders the provisioner section of the summary page. The template receives the dict from `get_summary_data()` as `provisioner`.

---

## 4. Root Agent Actions

The provisioner extends the root agent daemon with hypervisor-specific privileged commands.

### Module Path

Declared in `root_agent_actions`. Absolute path to a `.py` file. The root agent daemon loads it at startup:

```python
spec = importlib.util.spec_from_file_location("provisioner_actions", manifest["root_agent_actions"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
actions.update(module.ACTIONS)
```

### Required Export

```python
ACTIONS = {
    "<action-name>": handler_function,
    ...
}
```

**Note:** The export is `ACTIONS`, not `COMMANDS` (despite what the contract doc says).

### Handler Signature

```python
def handler(params: dict) -> dict:
    """
    params: action-specific parameters from the client request.

    Returns:
        {"ok": True, "output": "..."}           — success
        {"ok": False, "error": "description"}    — failure
    """
```

### Available Imports from `_common`

The root agent daemon's `_common` module (from blockhost-common) provides:

| Import | Type | Purpose |
|--------|------|---------|
| `log(msg)` | function | Log to root agent log file |
| `run(cmd, timeout=120)` | function | `-> (returncode, stdout, stderr)`. Runs subprocess with timeout. |
| `validate_vmid(value)` | function | Validates and returns integer VMID. Raises on invalid. |
| `QM_CREATE_ALLOWED_ARGS` | set | Proxmox-specific. Whitelist for qm create flags. |
| `QM_SET_ALLOWED_KEYS` | set | Proxmox-specific. Whitelist for qm set keys. |
| `STORAGE_RE` | regex | Validates storage pool names. |

Non-Proxmox provisioners will primarily use `log`, `run`, and potentially `STORAGE_RE`. The QM-specific constants are only relevant to the Proxmox provisioner.

### Security Model

- The root agent daemon runs as root
- Client requests come over a Unix socket from the `blockhost` user
- **All parameters must be validated** before constructing shell commands
- File paths must be restricted to allowed directories (`/var/lib/blockhost/`, `/tmp/`)
- Command arguments must be whitelisted — never pass user input directly to shell

---

## 5. First-Boot Hook

Installs the provisioner's platform dependencies during first system setup.

### Contract

| Aspect | Requirement |
|--------|-------------|
| **Path** | Declared in `setup.first_boot_hook`. Absolute path. |
| **Caller** | `scripts/first-boot.sh` in the main repo, after package installation. |
| **Runs as** | root |
| **Env vars** | `STATE_DIR` (e.g., `/var/lib/blockhost`), `LOG_FILE` (e.g., `/var/log/blockhost-firstboot.log`) |
| **Idempotent** | Must be safe to run multiple times. Use step markers in `$STATE_DIR`. |
| **Exit** | 0 on success. Non-zero stops the entire first-boot sequence. |

### Step Marker Pattern

```bash
STEP_MARKER="${STATE_DIR}/.step-<name>"
if [ ! -f "$STEP_MARKER" ]; then
    # ... do the work ...
    touch "$STEP_MARKER"
fi
```

**Critical:** Step marker names must not collide with markers used by the main `first-boot.sh`. The main script uses: `.step-network-wait`, `.step-packages`, `.step-foundry`. Provisioner hooks should use descriptive names like `.step-libvirt`, `.step-proxmox`.

---

## 6. Systemd Units

Provisioners typically ship a GC timer for daily cleanup of expired VMs.

| Unit | Type | Purpose |
|------|------|---------|
| `blockhost-gc.timer` | Timer | Triggers daily at 02:00 ± 30min |
| `blockhost-gc.service` | Oneshot | Runs `blockhost-vm-gc --execute` as `blockhost:blockhost` |

Installed to `/usr/lib/systemd/system/`. Enabled and started by the `.deb` postinst script.

---

## 7. Config Files

### Owned by the provisioner

Written during wizard finalization, consumed by provisioner scripts at runtime:

| File | Written by | Contains |
|------|-----------|----------|
| `/etc/blockhost/db.yaml` | Installer finalization | Provisioner-specific keys (declared in `config_keys.provisioner_config`) plus shared keys (`ip_pool`, `grace_days`, `db_path`) |

The provisioner **declares** which keys it owns in the manifest. The installer writes them. The provisioner reads them via `blockhost.config.load_db_config()` (from blockhost-common).

### Shared (read-only for provisioner)

| File | Managed by | Provisioner reads |
|------|-----------|-------------------|
| `/etc/blockhost/web3-defaults.yaml` | Installer | `rpc_url`, `nft_contract`, `deployer_key_path` — for NFT minting |
| `/etc/blockhost/broker-allocation.json` | Broker client | `ipv6_prefix`, `gateway` — for IPv6 VM access |

### Config loading

Always use the blockhost-common API:
```python
from blockhost.config import load_db_config, load_web3_config, load_broker_allocation
```

---

## 8. .deb Package

### Naming

`blockhost-provisioner-<name>_<version>_all.deb`

### Conflicts

Provisioner packages must declare `Conflicts:` with all other provisioner packages. Only one provisioner can be active per host (the manifest path is a singleton).

```
Conflicts: blockhost-provisioner-proxmox
```

### Install Locations

| Content | Destination |
|---------|-------------|
| CLI commands | `/usr/bin/blockhost-vm-*`, `/usr/bin/blockhost-build-template`, `/usr/bin/blockhost-provisioner-detect`, `/usr/bin/blockhost-mint-nft` |
| Manifest | `/usr/share/blockhost/provisioner.json` |
| First-boot hook | `/usr/share/blockhost/provisioner-hooks/first-boot.sh` |
| Root agent actions | `/usr/share/blockhost/root-agent-actions/<name>.py` |
| Wizard plugin | `/usr/lib/python3/dist-packages/blockhost/provisioner_<name>/` |
| Systemd units | `/usr/lib/systemd/system/blockhost-gc.{service,timer}` |
| Docs | `/usr/share/doc/blockhost-provisioner-<name>/` |

### Dependencies

```
Depends: python3 (>= 3.10), blockhost-common (>= 0.1.0), libpam-web3-tools (>= 0.5.0)
Recommends: <hypervisor-specific packages>
```

---

## 9. Known Issues & Inconsistencies

Documented for awareness. These exist in the current implementation.

### `blockhost-mint-nft` not in manifest dispatch

The engine hardcodes `blockhost-mint-nft` at `handlers/index.ts:324` instead of resolving through `getCommand("mint-nft")`. The manifest has no `"mint-nft"` verb. Decision: minting belongs in the engine, not in provisioners. This is not a contract violation — it's correctly engine-owned.

### ~~Hardcoded `/opt/` paths in app.py~~ (FIXED)

~~Lines 1648 and 3127 reference `/opt/blockhost-provisioner-proxmox/scripts/build-template.sh` as fallbacks.~~ Fixed: both now resolve `build-template` from the provisioner manifest. One remaining instance in `provisioner_proxmox/wizard.py:649` (submodule — prompt sent).

### Transitional summary fallback

`app.py` line 637 has a hardcoded Proxmox dict as fallback when no provisioner module is loaded. This should be removed — no-provisioner is not a valid state, and the hardcoded dict actively poisons custom provisioner development.

### Stub commands in manifest

`vm-metrics` and `vm-throttle` are declared in the manifest but return "not yet implemented" on stderr. Consumers get exit 0 with no useful output. Currently harmless — no consumer calls these yet.

### Contract doc drift

`blockhost-common/provisioner-contract.md` has several inaccuracies vs. the actual implementation:
- CLI signatures show `--vmid VMID` and `--name NAME` as named args; actual uses positional `<name>` for most commands
- Wizard export listed as `wizard_bp`; actual is `blueprint`
- Root agent export listed as `COMMANDS`; actual is `ACTIONS`
- Finalization function return type shown as `dict`; actual is `tuple[bool, Optional[str]]`
- `status` output described as JSON; actual is plain text (one of four strings)

This document (`PROVISIONER_INTERFACE.md`) reflects the actual implementation.
