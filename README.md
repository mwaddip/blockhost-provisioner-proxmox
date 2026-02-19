# blockhost-provisioner-proxmox

Terraform-based Proxmox VM automation with NFT web3 authentication. Creates Debian 12 VMs from a cloud-init template that includes [libpam-web3](https://github.com/mwaddip/libpam-web3) for wallet-based SSH login.

## How it works

1. A Debian 12 cloud image is customized with `libpam-web3` and uploaded to Proxmox as a template (VMID 9001)
2. The engine reserves an NFT token ID, then calls `vm-generator.py` with `--owner-wallet` and `--nft-token-id`
3. `vm-generator.py` generates a Terraform `.tf.json` config with cloud-init (baking wallet + token ID into GECOS), applies it, and prints a JSON summary
4. Users authenticate to SSH by signing an OTP challenge with their wallet

VMs are tracked in a JSON database with IP/VMID allocation, expiry dates, and NFT token status. Expired VMs are cleaned up by `vm-gc.py`.

## Prerequisites

- **blockhost-common** package - Provides configuration and database modules
- [Terraform](https://www.terraform.io/) with the [bpg/proxmox](https://registry.terraform.io/providers/bpg/proxmox/latest) provider
- Proxmox VE host accessible via SSH (`root@ix`)
- `libguestfs-tools` for template image customization

## Installation

```bash
# Install dependencies
sudo dpkg -i blockhost-common_*.deb
sudo dpkg -i blockhost-provisioner-proxmox_*.deb

# Initialize server (generates keys and config)
sudo /path/to/blockhost-engine/scripts/init-server.sh

# Configure settings
sudo editor /etc/blockhost/db.yaml
sudo editor /etc/blockhost/web3-defaults.yaml

# Build Proxmox template
./scripts/build-template.sh
```

## Integration / Package Usage

For programmatic integration:

- **`PROJECT.yaml`** - Machine-readable API specification with all entry points, arguments, Python APIs, and configuration options
- **`CLAUDE.md`** - Instructions for AI assistants working with this codebase

Read `PROJECT.yaml` for the complete interface documentation.

## Project structure

```
.
├── PROJECT.yaml            # Machine-readable API spec
├── CLAUDE.md               # AI assistant instructions
├── provisioner.json          # Provisioner manifest for engine integration
├── scripts/
│   ├── build-template.sh   # Build Debian 12 template with libpam-web3
│   ├── vm-generator.py     # Generate + apply VM Terraform configs (JSON output)
│   ├── vm-destroy.sh       # Destroy a VM (terraform + cleanup)
│   ├── vm-start.sh         # Start a VM via root agent
│   ├── vm-stop.sh          # Gracefully shut down a VM
│   ├── vm-kill.sh          # Force-stop a VM
│   ├── vm-status.sh        # Print VM status
│   ├── vm-list.sh          # List all VMs
│   ├── vm-metrics.sh       # VM metrics (stub)
│   ├── vm-throttle.sh      # VM throttling (stub)
│   ├── vm-resume.py        # Resume a suspended VM
│   ├── vm-gc.py            # Garbage collect expired VMs
│   └── provisioner-detect.sh # Detect Proxmox VE host
├── accounting/
│   └── mock-db.json        # Mock database for testing
├── provider.tf.json        # Terraform provider config
└── variables.tf.json       # Terraform variable defaults
```

## Scripts

### `scripts/build-template.sh`

Builds the Proxmox VM template:

- Downloads the Debian 12 genericcloud qcow2 image
- Injects the `libpam-web3` `.deb` package using `virt-customize` (no VM boot required)
- Enables the `web3-auth-svc` systemd service
- Uploads to Proxmox and creates template VMID 9001

```bash
# Uses defaults (host=root@ix, template=9001)
./scripts/build-template.sh

# Override settings
PROXMOX_HOST=root@myhost TEMPLATE_VMID=9002 ./scripts/build-template.sh
```

### `scripts/vm-generator.py`

Creates VMs with NFT-based web3 authentication:

1. Allocates an IP address and VMID from the pool
2. Renders cloud-init from the `nft-auth` template (or uses `--cloud-init-content` for pre-rendered content)
3. Generates a `.tf.json` Terraform config
4. Optionally runs `terraform apply`
5. On success, prints a JSON summary as the last stdout line

The engine owns the NFT lifecycle (token ID reservation, encryption, minting). The provisioner receives `--nft-token-id` and `--owner-wallet` from the engine.

```bash
# Engine-driven: create VM
python3 scripts/vm-generator.py web-001 --owner-wallet 0x1234... \
    --nft-token-id 42 --apply

# With pre-rendered cloud-init
python3 scripts/vm-generator.py web-001 --owner-wallet 0x1234... \
    --nft-token-id 42 --apply --cloud-init-content /path/to/rendered.yaml

# Without web3 auth
python3 scripts/vm-generator.py web-001 --no-web3 --cloud-init webserver

# Test mode (mock DB)
python3 scripts/vm-generator.py web-001 --owner-wallet 0x1234... \
    --nft-token-id 0 --mock --apply
```

### `scripts/vm-gc.py`

Two-phase garbage collection for expired VMs:
- **Phase 1 (Suspend)**: Shuts down expired VMs, preserves disk data
- **Phase 2 (Destroy)**: Destroys suspended VMs past grace period, removes IPv6 host routes

Runs automatically via systemd timer (daily at 2 AM):

```bash
# Dry run - list expired VMs
python3 scripts/vm-gc.py

# Execute both phases
python3 scripts/vm-gc.py --execute

# Only suspend expired VMs
python3 scripts/vm-gc.py --execute --suspend-only

# Check timer status
systemctl status blockhost-gc.timer
```

### VM lifecycle commands

Standalone commands for managing individual VMs, used by the engine via the provisioner manifest:

```bash
blockhost-vm-start web-001      # Start a VM
blockhost-vm-stop web-001       # Gracefully shut down
blockhost-vm-kill web-001       # Force-stop
blockhost-vm-destroy web-001    # Destroy (terraform + cleanup)
blockhost-vm-status web-001     # Print status
blockhost-vm-list               # List all VMs (text)
blockhost-vm-list --format json # List all VMs (JSON)
```

## Configuration

Configuration files are provided by **blockhost-common** in `/etc/blockhost/`:

### `/etc/blockhost/web3-defaults.yaml`

Blockchain settings: chain ID, NFT contract address, RPC URL, deployer key path. Update these with your deployed contract details.

### `/etc/blockhost/db.yaml`

Database configuration: production DB file path, terraform_dir, IP pool range, VMID range, default expiry, and GC grace period.

## NFT auth flow

1. VM boots, `web3-auth-svc` starts serving signing page over HTTPS (port 8443, self-signed TLS)
2. User visits `https://VM_IP:8443`, signs challenge with their Ethereum wallet
3. PAM module (`pam_web3.so`) validates the signature against NFT ownership on-chain
4. Access granted if the wallet owns the VM's NFT token

## Setup

1. Install blockhost-common package
2. Run `init-server.sh` from blockhost-engine to generate keys and config
3. Edit `/etc/blockhost/web3-defaults.yaml` with your contract address and RPC URL
4. Edit `/etc/blockhost/db.yaml` with your terraform_dir path
5. Build the template: `./scripts/build-template.sh`
6. Create `terraform.tfvars` in terraform_dir with your Proxmox credentials
7. Create VMs: `python3 scripts/vm-generator.py <name> --owner-wallet <addr> --apply`
