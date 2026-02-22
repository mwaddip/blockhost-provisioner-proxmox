# Codebase Health Review — Findings

> Date: 2026-02-22
> Scope: blockhost-provisioner-proxmox (feature/opnet branch, commit 9bb851e)
> Constraint: No public interface changes. All refactors must be behavior-preserving.

---

## 1. Dead Code & Orphans

### F1.1 — Stale build artifacts in `build/` (informational)

`build/` is `.gitignore`'d and not tracked by git, but if it exists locally from a previous `build-deb.sh` run, it contains two outdated Python files:

- `build/pkg/.../mint_nft.py` — from before NFT minting moved to blockhost-engine. Not copied by current `build-deb.sh`, just leftover.
- `build/pkg/.../vm_generator.py` — old version that imports removed symbols (`from mint_nft import mint_nft`, `from blockhost.config import get_terraform_dir`).

No code change needed — just a `rm -rf build/` to clean the workspace.

### F1.2 — `variables.tf.json` and `provider.tf.json` in repo root

These are local development Terraform files. They define variables (`proxmox_endpoint`, `default_storage`, etc.) that production code never references — `vm-generator.py` reads settings from `terraform.tfvars` and `db.yaml`, not from Terraform variables. The wizard writes its own `provider.tf.json` and `variables.tf.json` to `terraform_dir` during finalization.

If still used for local `terraform plan` testing they're fine. If not, they're clutter.

### F1.3 — `vm-generator.py:load_terraform_vars()` is only used for defaults (informational)

The function (lines 62–84) parses `terraform.tfvars` to extract `proxmox_node`, `proxmox_storage`, etc. as fallback defaults. This is correct and used, but worth noting: the wizard's `finalize_terraform` writes these keys, and the production flow will always have them. The parser only matters for manual/dev use where someone creates VMs before running the wizard.

Not dead code — just context for later.

---

## 2. Repetition & Abstraction Opportunities

### F2.1 — `get_terraform_dir()` duplicated across two scripts

Identical logic in two files:

- `scripts/vm-generator.py:56–59`
- `scripts/vm-gc.py:52–54`

Both do `Path(load_db_config()["terraform_dir"])`. Also inlined in `scripts/vm-destroy.sh:24`.

### F2.2 — `sanitize_resource_name()` duplicated across two scripts

Identical `re.sub(r"[^a-zA-Z0-9_]", "_", name)` in:

- `scripts/vm-generator.py:87–89`
- `scripts/vm-gc.py:57–59`

### F2.3 — Shell wrapper scripts follow identical pattern

`vm-start.sh`, `vm-stop.sh`, and `vm-kill.sh` are structurally identical:

```
1. Validate $1 not empty
2. exec python3 with inline script that:
   a. Looks up VM in database
   b. Calls one root_agent function
   c. Prints confirmation
```

The only differences are the imported function (`qm_start` / `qm_shutdown` / `qm_stop`), the verb in the success message, and the error message. `vm-status.sh` follows the same pattern with minor variation (no root agent call). These are small files and the duplication is tolerable — flagging it, but a shared helper might over-abstract for 4 shell wrappers.

---

## 3. Input Validation & Sanitization

### F3.1 — `--owner-wallet` not validated in `vm-generator.py` (MEDIUM)

`vm-generator.py` accepts `--owner-wallet` and passes it directly into cloud-init template variables (`WALLET_ADDRESS`) and the database. The root agent's `GECOS_RE` (`^wallet=[a-zA-Z0-9]{1,128}(,nft=[0-9]{1,10})?$`) validates the composed GECOS string at the `update-gecos` boundary — but at creation time, the wallet address is baked into cloud-init content without any validation.

An address containing shell metacharacters or YAML-special chars could corrupt the cloud-init YAML (e.g., a `:` or `\n` in the address). The engine is the primary caller and sends real wallet addresses, but the CLI is a system boundary.

**Suggestion:** Validate `--owner-wallet` against `^[a-zA-Z0-9]{1,128}$` (matching what GECOS_RE expects) early in `main()`.

### F3.2 — `--ip` and `--ipv6` not validated in `vm-generator.py` (LOW)

Manual override IPs are passed directly to Terraform config (e.g., `f"{ip_address}/24"`). Invalid input would cause Terraform to fail, so there's no silent corruption, but an early check would give better error messages.

**Suggestion:** Validate with `ipaddress.IPv4Address()` / `ipaddress.IPv6Address()`.

### F3.3 — `vm_name` not validated in `vm-resume.py` (LOW)

`vm-generator.py` validates the VM name (`^[a-zA-Z0-9][a-zA-Z0-9._-]*$`) to prevent path traversal (the name is used in file paths). `vm-resume.py` skips this validation. Since `vm-resume.py` only does a database lookup (not file creation), the impact is minimal, but it would be good hygiene to validate consistently.

### F3.4 — `wallet_address` not validated in `vm-update-gecos.sh` (LOW)

The wallet address from the positional CLI argument is composed into a GECOS string (`wallet={addr},nft={id}`) and sent to the root agent. The root agent's `GECOS_RE` catches any invalid format, so this is defense-in-depth, not a gap. But early validation would give a better error message ("invalid wallet address" vs "invalid gecos").

### F3.5 — Wizard form integer parsing without error handling (LOW)

`wizard.py:44–51` does bare `int()` casts on form values:

```python
"template_vmid": int(request.form.get("template_vmid", 9001)),
```

A non-numeric submission crashes with `ValueError`. This is a web form submitted by an admin during installation (not a public endpoint), so exploitability is negligible, but it would be cleaner to validate.

---

## 4. General Code Quality

### F4.1 — `shutdown_vm()` in `vm-gc.py` has an unused `graceful_timeout` parameter

`vm-gc.py:83`:

```python
def shutdown_vm(vmid: int, graceful_timeout: int = 60) -> tuple[bool, str]:
```

`graceful_timeout` is declared but never used. The function calls `qm_shutdown()` (which sends an ACPI shutdown signal and returns immediately) then immediately falls back to `qm_stop()` if `qm_shutdown()` fails. There's no actual waiting for the graceful shutdown to complete before force-stopping. This means a VM that would shut down cleanly in 10 seconds is force-stopped anyway if `qm_shutdown` returns a non-error but the VM hasn't fully stopped yet.

In practice, `qm_shutdown` returns success if the ACPI signal was sent (even though the VM hasn't stopped yet), so the fallback to `qm_stop` shouldn't trigger. But the parameter is misleading — it suggests a timeout that doesn't exist.

**Suggestion:** Either remove the parameter, or implement actual wait-and-retry logic.

### F4.2 — Shell wrapper scripts don't catch `RootAgentError`

`vm-start.sh`, `vm-stop.sh`, `vm-kill.sh` call root agent functions without try/except:

```python
qm_start(vm["vmid"])  # RootAgentError → ugly stack trace
```

`vm-destroy.sh` and `vm-update-gecos.sh` *do* catch `RootAgentError`. This inconsistency means some commands show clean errors and others dump Python tracebacks.

### F4.3 — `vm-generator.py:run_terraform()` doesn't capture stderr for error reporting

`vm-generator.py:228`:

```python
result = subprocess.run(cmd, cwd=tf_dir)
```

Terraform output goes directly to the terminal (no `capture_output`), which is fine for interactive use but means the JSON summary on stdout (for engine consumption) is mixed with Terraform's own stdout. The engine parses the *last line* of stdout as JSON, which works as long as Terraform doesn't write to stdout after the `print(json.dumps(summary))` call. This is currently safe because `run_terraform` finishes before the summary is printed, but it's fragile.

By contrast, `vm-gc.py:121` captures output: `subprocess.run(cmd, cwd=tf_dir, capture_output=True, text=True)`.

### F4.4 — `build-deb.sh` version string duplicated

The version `0.2.0` appears in two places within `build-deb.sh`:

- Line 14: `VERSION="0.2.0"`
- Line 35 in the DEBIAN/control heredoc: `Version: 0.2.0`

The control file should use `${VERSION}` but it's inside a `'EOF'` heredoc (single-quoted = no expansion). This means version bumps require updating two places within the same file, plus `provisioner.json`.

### F4.5 — `wizard.py:finalize_bridge` runs `ip route show default` twice

Lines 478–494 parse the default route to find the primary interface. Then lines 524–535 run the exact same command again to extract the gateway. The first invocation already has the gateway in `parts` but doesn't extract it.

---

## Summary Table

| ID | Category | Severity | File(s) |
|----|----------|----------|---------|
| F1.2 | Dead code | Low | `variables.tf.json`, `provider.tf.json` (root) |
| F2.1 | Repetition | Low | `vm-generator.py`, `vm-gc.py` |
| F2.2 | Repetition | Low | `vm-generator.py`, `vm-gc.py` |
| F2.3 | Repetition | Low | `vm-start.sh`, `vm-stop.sh`, `vm-kill.sh` |
| F3.1 | Validation | Medium | `vm-generator.py` |
| F3.2 | Validation | Low | `vm-generator.py` |
| F3.3 | Validation | Low | `vm-resume.py` |
| F3.4 | Validation | Low | `vm-update-gecos.sh` |
| F3.5 | Validation | Low | `wizard.py` |
| F4.1 | Quality | Low | `vm-gc.py` |
| F4.2 | Quality | Low | `vm-start.sh`, `vm-stop.sh`, `vm-kill.sh` |
| F4.3 | Quality | Low | `vm-generator.py` |
| F4.4 | Quality | Low | `build-deb.sh` |
| F4.5 | Quality | Low | `wizard.py` |
