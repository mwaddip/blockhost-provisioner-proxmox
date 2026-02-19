# Claude Instructions for blockhost-provisioner-proxmox

## SPECIAL.md (HIGHEST PRIORITY)

**Read and internalize `SPECIAL.md` at the start of every session.** It defines per-component priority weights — where to invest extra scrutiny beyond standard professional practice. All stats at 5 = normal competence. Stats above 5 = extra focus.

| Path pattern | Profile | Extra focus areas |
|---|---|---|
| `scripts/vm-gc.py` | S8 P6 E9 C4 I6 A5 L8 | Robustness (destroys resources), reliability (must be idempotent), edge cases (cleanup failures = data loss) |
| `scripts/build-template.sh` | S7 P6 E8 C4 I5 A5 L6 | Reliability (must be idempotent, runs once) |
| everything else | S9 P7 E9 C5 I7 A6 L7 | Robustness + reliability (VM lifecycle is unforgiving) |

See `SPECIAL.md` for full stat definitions and the priority allocation model.

## Interface Contracts (REFERENCE)

**Contract specs define how this package interfaces with the rest of the system.** Read and internalize the relevant contract before modifying CLI signatures, wizard exports, root agent actions, or the manifest. Do not rely on assumptions — read the contract.

| Contract | Covers | Read when... |
|----------|--------|-------------|
| `facts/PROVISIONER_INTERFACE.md` | The provisioner contract — manifest schema, CLI commands, wizard plugin, root agent actions, first-boot hook | Changing any interface the engine, wizard, root agent, or first-boot consumes |
| `facts/COMMON_INTERFACE.md` | blockhost-common's public API — config, vm_db, root_agent, cloud_init | Using any import from `blockhost.*` |

**This is the reference implementation.** The libvirt provisioner is being built against the same contract. If the contract needs to change, coordinate with the main session — both provisioners must stay in sync.

## Interface Integrity (PERSISTENT RULE)

**When interfaces don't match, fix the interface — never wrap the mismatch.** If something in the contract doesn't match reality, update the contract (and notify the main session), don't write a wrapper.

## Project Scope

**This Claude session only modifies blockhost-provisioner-proxmox.** Changes to dependency packages (blockhost-common, blockhost-broker, blockhost-engine) should be done in their respective Claude sessions with separate prompts.

## Project Overview

This is the Proxmox VM provisioning component of the Blockhost system, providing NFT-based web3 authentication. Read `PROJECT.yaml` for the complete machine-readable API specification.

**Dependencies:**
- `blockhost-common` - Provides `blockhost.config`, `blockhost.vm_db`, and `blockhost.root_agent` modules
- `blockhost-broker` - IPv6 tunnel broker (broker-client saves allocation to `/etc/blockhost/broker-allocation.json`)
- `blockhost-engine` - Provides `nft_tool` CLI (encrypt-symmetric for connection details)

## Environment Variables

Essential environment variables (contract addresses, deployer key, RPC URL) are stored in:
```
~/projects/sharedenv/blockhost.env
```

Source this file before running scripts that interact with the blockchain:
```bash
source ~/projects/sharedenv/blockhost.env
```

## Quick Reference

```bash
# Engine-driven: create VM, skip minting (engine mints separately)
python3 scripts/vm-generator.py <name> --owner-wallet <0x...> --apply --no-mint

# Engine-driven with pre-rendered cloud-init
python3 scripts/vm-generator.py <name> --owner-wallet <0x...> --apply --no-mint \
    --cloud-init-content /path/to/rendered.yaml

# Legacy: create VM and mint NFT inline
python3 scripts/vm-generator.py <name> --owner-wallet <0x...> [--apply]

# Legacy: with encrypted connection details (subscription system workflow)
python3 scripts/vm-generator.py <name> --owner-wallet <0x...> \
    --user-signature <0x...> --public-secret "libpam-web3:<address>:<nonce>" \
    [--apply]

# VM lifecycle commands
blockhost-vm-start <name>
blockhost-vm-stop <name>
blockhost-vm-kill <name>
blockhost-vm-destroy <name>
blockhost-vm-status <name>
blockhost-vm-list [--json]

# Update VM GECOS after ownership transfer
blockhost-vm-update-gecos <name> <wallet-address> --nft-id <id>

# Garbage collect expired VMs
python3 scripts/vm-gc.py [--execute] [--grace-days N]

# Build/rebuild Proxmox template
./scripts/build-template.sh
```

## Mandatory: Keep PROJECT.yaml Updated

**After ANY modification to the scripts, you MUST update `PROJECT.yaml`** to reflect:

1. **New/changed CLI arguments** - Update the `entry_points` section
2. **New/changed Python functions** - Update the `python_api` section
3. **New/changed config options** - Update the `config_files` section
4. **New cloud-init templates** - These belong in `blockhost-common`; update `external_modules` section if needed
5. **Changed workflow/behavior** - Update the `workflow` section

### Update Checklist

When modifying any script, ask yourself:
- [ ] Did I add/remove/change any command-line arguments?
- [ ] Did I add/remove/change any Python class methods?
- [ ] Did I add/remove/change any configuration options?
- [ ] Did I change the workflow or data flow?

If yes to any, update `PROJECT.yaml` accordingly.

## Key Files

| File | Purpose |
|------|---------|
| `PROJECT.yaml` | Machine-readable API spec (KEEP UPDATED) |
| `provisioner.json` | Provisioner manifest for engine integration |
| `scripts/vm-generator.py` | Main entry point for VM creation |
| `scripts/vm-destroy.sh` | Destroy a VM (terraform + cleanup) |
| `scripts/vm-start.sh` | Start a VM via root agent |
| `scripts/vm-stop.sh` | Gracefully shut down a VM |
| `scripts/vm-kill.sh` | Force-stop a VM |
| `scripts/vm-status.sh` | Print VM status |
| `scripts/vm-list.sh` | List all VMs |
| `scripts/vm-gc.py` | Garbage collection for expired VMs |
| `scripts/vm-resume.py` | Resume a suspended VM |
| `scripts/vm-update-gecos.sh` | Update VM GECOS after ownership transfer |
| `scripts/build-template.sh` | Proxmox template builder |
| `scripts/provisioner-detect.sh` | Detect Proxmox VE host |
| `blockhost/provisioner_proxmox/wizard.py` | Wizard plugin (Blueprint, finalization, summary) |
| `provisioner-hooks/first-boot.sh` | First-boot hook (installs Proxmox, Terraform) |
| `root-agent-actions/qm.py` | Root agent QM actions plugin (qm-start/stop/create/set/etc.) |

### From blockhost-common package

| Module/File | Purpose |
|-------------|---------|
| `blockhost.config` | Config loading (load_db_config, load_web3_config, load_broker_allocation) |
| `blockhost.vm_db` | Database abstraction (VMDatabase, MockVMDatabase, get_database) |
| `blockhost.root_agent` | Root agent client (qm_start/stop/shutdown/destroy, ip6_route_add/del) |
| `blockhost.cloud_init` | Cloud-init template rendering (render_cloud_init, find_template) |
| `/etc/blockhost/db.yaml` | Database and terraform_dir config |
| `/etc/blockhost/web3-defaults.yaml` | Blockchain/NFT settings |

## Configuration

### terraform_dir

The `terraform_dir` setting in `/etc/blockhost/db.yaml` specifies where:
- Generated `.tf.json` files are written
- Terraform commands are executed

This is typically a separate directory with Proxmox provider credentials and terraform state.

### Mock vs Production Database

- `--mock` flag uses `MockVMDatabase` backed by `accounting/mock-db.json`
- Production uses `VMDatabase` with file at path specified in `/etc/blockhost/db.yaml`

## Testing Changes

Always test with mock database first:
```bash
python3 scripts/vm-generator.py test-vm --owner-wallet 0x1234... --mock --skip-mint
```

## Package Integration

When installed as a package:
1. Install `blockhost-common` first (provides config and database modules)
2. Install `blockhost-provisioner-proxmox` (this package)
3. Install `blockhost-engine` (provides nft_tool CLI)
4. Configure `/etc/blockhost/db.yaml` with correct `terraform_dir`
5. Configure `/etc/blockhost/web3-defaults.yaml` with contract details
6. Run scripts via: `blockhost-vm-create`, `blockhost-vm-gc`, etc.

## NFT Token ID Management

NFT token IDs are sequential and tracked in the database:
- `reserve_nft_token_id()` - Reserves next ID before VM creation
- `mark_nft_minted()` - Called after successful mint
- `mark_nft_failed()` - Called if VM creation fails

**Never reuse failed token IDs** - they create gaps in the sequence but prevent on-chain conflicts.

## Pre-Push Documentation Check

**Before creating a commit or pushing to GitHub**, you MUST:

1. **Re-read `PROJECT.yaml`** and verify it reflects all changes made in this session
2. **Re-read `CLAUDE.md`** and verify the Quick Reference, Key Files table, and other sections are still accurate
3. **Fix any stale documentation** before committing — do not push code that contradicts the docs

This applies to every commit, not just large changes. Small changes (renamed flags, new imports, changed defaults) can silently make docs wrong.

## VM Authentication Flow

1. VM boots with libpam-web3 installed via cloud-init template
2. `web3-auth-svc` serves signing page over HTTPS (port 8443, self-signed TLS)
3. User visits `https://VM_IP:8443`, signs challenge with their wallet
4. PAM module validates signature against NFT ownership on-chain

The signing page is served by web3-auth-svc over HTTPS. With callback support (v0.6.0+), the page auto-fills OTP and machine name — users just sign and press Enter.

## Subscription System Workflow

When using the subscription system, ECIES-encrypted connection details are stored on-chain in the NFT's `userEncrypted` field:

1. **User signs message**: User signs `libpam-web3:<checksumAddress>:<nonce>` with their wallet
2. **Subscription system calls vm-generator.py** with:
   - `--owner-wallet`: User's wallet address
   - `--user-signature`: The decrypted signature (hex)
   - `--public-secret`: The original message that was signed
3. **vm-generator.py** creates the VM, then:
   - Encrypts connection details (hostname, port, username) using `nft_tool encrypt-symmetric`
   - Key derivation: `keccak256(signature_bytes)` → 32-byte AES key
   - Mints NFT with encrypted data in `userEncrypted` field
4. **User retrieves connection details**:
   - Re-signs the same `publicSecret` with their wallet
   - Derives decryption key from signature
   - Decrypts `userEncrypted` to get hostname/port/username

### NFT Contract Function

```solidity
mint(address to, bytes userEncrypted, string publicSecret,
     string description, string imageUri, string animationUrlBase64, uint256 expiresAt)
```

- `userEncrypted`: AES-256-GCM encrypted JSON (or `0x` if not using encryption)
- `publicSecret`: Format `libpam-web3:<checksumAddress>:<nonce>`
- `animationUrlBase64`: Empty string (signing page is served from local filesystem, not embedded in NFT)
