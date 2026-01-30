# Claude Instructions for proxmox-terraform

## Project Overview

This is a Proxmox VM automation system with NFT-based web3 authentication. Read `PROJECT.yaml` for the complete machine-readable API specification.

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
# Create VM with web3 auth (basic - no encrypted connection details)
python3 scripts/vm-generator.py <name> --owner-wallet <0x...> [--apply]

# Create VM with encrypted connection details (subscription system workflow)
python3 scripts/vm-generator.py <name> --owner-wallet <0x...> \
    --user-signature <0x...> --decrypt-message "libpam-web3:<address>:<nonce>" \
    [--apply]

# Mint NFT manually (with encrypted connection details)
python3 scripts/mint_nft.py --owner-wallet <0x...> --machine-id <name> \
    --user-encrypted <0x...> --decrypt-message "libpam-web3:<address>:<nonce>"

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
4. **New cloud-init templates** - Update the `cloud_init_templates` section
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
| `scripts/vm-generator.py` | Main entry point for VM creation |
| `scripts/vm_db.py` | Database abstraction (VMDatabase, MockVMDatabase) |
| `scripts/vm-gc.py` | Garbage collection for expired VMs |
| `scripts/mint_nft.py` | NFT minting via Foundry cast |
| `scripts/build-template.sh` | Proxmox template builder |
| `config/db.yaml` | Database and terraform_dir config |
| `config/web3-defaults.yaml` | Blockchain/NFT settings |

## Configuration

### terraform_dir

The `terraform_dir` setting in `config/db.yaml` specifies where:
- Generated `.tf.json` files are written
- Terraform commands are executed

This is typically a separate directory with Proxmox provider credentials and terraform state.

### Mock vs Production Database

- `--mock` flag uses `MockVMDatabase` backed by `accounting/mock-db.json`
- Production uses `VMDatabase` with file at path specified in `config/db.yaml`

## Testing Changes

Always test with mock database first:
```bash
python3 scripts/vm-generator.py test-vm --owner-wallet 0x1234... --mock --skip-mint
```

## Submodule Integration

When this repo is used as a submodule:
1. Parent project should configure `config/db.yaml` with correct `terraform_dir`
2. Parent project should configure `config/web3-defaults.yaml` with contract details
3. Import scripts via: `python3 path/to/submodule/scripts/vm-generator.py ...`
4. Read `PROJECT.yaml` for complete API documentation

## NFT Token ID Management

NFT token IDs are sequential and tracked in the database:
- `reserve_nft_token_id()` - Reserves next ID before VM creation
- `mark_nft_minted()` - Called after successful mint
- `mark_nft_failed()` - Called if VM creation fails

**Never reuse failed token IDs** - they create gaps in the sequence but prevent on-chain conflicts.

## Subscription System Workflow

When using the subscription system, connection details are encrypted into the NFT:

1. **User signs message**: User signs `libpam-web3:<checksumAddress>:<nonce>` with their wallet
2. **Subscription system calls vm-generator.py** with:
   - `--owner-wallet`: User's wallet address
   - `--user-signature`: The decrypted signature (hex)
   - `--decrypt-message`: The original message that was signed
3. **vm-generator.py** creates the VM, then:
   - Encrypts connection details (hostname, port, username) using `pam_web3_tool encrypt-symmetric`
   - Key derivation: `keccak256(signature_bytes)` → 32-byte AES key
   - Mints NFT with encrypted data in `userEncrypted` field
4. **User retrieves connection details**:
   - Re-signs the same `decryptMessage` with their wallet
   - Derives decryption key from signature
   - Decrypts `userEncrypted` to get hostname/port/username

### NFT Contract Function

The new contract uses this mint signature:
```solidity
mint(address to, bytes userEncrypted, string decryptMessage,
     string description, string imageUri, string animationUrlBase64, uint256 expiresAt)
```

- `userEncrypted`: AES-256-GCM encrypted JSON (or `0x` if not using encryption)
- `decryptMessage`: Format `libpam-web3:<checksumAddress>:<nonce>`
- `animationUrlBase64`: Signing page HTML as base64 (not data URI)
