"""
Proxmox VE wizard plugin for BlockHost installer.

Provides:
- Flask Blueprint with /wizard/proxmox route
- Finalization steps: token, terraform, db_config, bridge, template
- Summary data for the summary page
"""

import grp
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Optional

import yaml

from flask import Blueprint, redirect, render_template, request, session, url_for

blueprint = Blueprint(
    "provisioner_proxmox",
    __name__,
    template_folder="templates",
)


# --- Wizard Route ---


@blueprint.route("/wizard/proxmox", methods=["GET", "POST"])
def wizard_proxmox():
    """Proxmox configuration step."""
    detected = _detect_proxmox_resources()

    if request.method == "POST":
        session["proxmox"] = {
            "api_url": request.form.get("pve_api_url"),
            "node": request.form.get("pve_node"),
            "storage": request.form.get("pve_storage"),
            "bridge": request.form.get("pve_bridge"),
            "user": request.form.get("pve_user"),
            "template_vmid": int(request.form.get("template_vmid", 9001)),
            "vmid_start": int(request.form.get("vmid_start", 100)),
            "vmid_end": int(request.form.get("vmid_end", 999)),
            "ip_network": request.form.get("ip_network"),
            "ip_start": request.form.get("ip_start"),
            "ip_end": request.form.get("ip_end"),
            "gateway": request.form.get("gateway"),
            "gc_grace_days": int(request.form.get("gc_grace_days", 7)),
            "terraform_dir": "/var/lib/blockhost/terraform",
        }
        return redirect(url_for("wizard_ipv6"))

    return render_template("provisioner_proxmox/proxmox.html", detected=detected)


# --- Summary ---


def get_ui_params(session_data: dict) -> dict:
    """Return Proxmox-specific UI parameters for wizard templates.

    Templates consume these via prov_ui.<key> | default(...).
    """
    network = session_data.get("network", {})
    wan_ip = network.get("ip", "")
    return {
        "management_url": f"https://{wan_ip}:8006" if wan_ip else "",
        "management_label": "Open Proxmox",
        "knock_ports_default": "22, 8006",
        "knock_description": "Define a secret command name that opens SSH and Proxmox ports temporarily.",
        "storage_hint": "Review detected storage devices. Proxmox VE is already installed on your system disk.",
        "storage_extra_hint": "Additional disks can be configured for VM storage in Proxmox after setup completes.",
    }


def get_summary_data(session_data: dict) -> dict:
    """Return provisioner-specific summary data."""
    proxmox = session_data.get("proxmox", {})
    return {
        "node": proxmox.get("node"),
        "storage": proxmox.get("storage"),
        "bridge": proxmox.get("bridge"),
        "vmid_start": proxmox.get("vmid_start"),
        "vmid_end": proxmox.get("vmid_end"),
        "ip_start": proxmox.get("ip_start"),
        "ip_end": proxmox.get("ip_end"),
        "gc_grace_days": proxmox.get("gc_grace_days", 7),
    }


def get_summary_template() -> str:
    """Return the template name for the provisioner summary section."""
    return "provisioner_proxmox/summary_section.html"


# --- Finalization Steps ---


def get_finalization_steps() -> list[tuple]:
    """Return provisioner finalization steps.

    Each tuple: (step_id, display_name, callable[, hint])
    The callable signature: func(config: dict) -> tuple[bool, Optional[str]]
    """
    return [
        ("token", "Creating Proxmox API token", finalize_token),
        ("terraform", "Configuring Terraform provider", finalize_terraform),
        ("db_config", "Writing provisioner config", finalize_db_config),
        ("bridge", "Configuring network bridge", finalize_bridge),
        ("template", "Building VM template", finalize_template, "(this may take several minutes)"),
    ]


# --- Helper Functions ---


def _set_blockhost_ownership(path, mode=0o640):
    """Set file to root:blockhost with given mode."""
    os.chmod(str(path), mode)
    gid = grp.getgrnam("blockhost").gr_gid
    os.chown(str(path), 0, gid)


def _write_tfvars(path: Path, data: dict):
    """Write Terraform tfvars file."""
    lines = []
    for key, value in data.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f'{key} = "{value}"')

    path.write_text("\n".join(lines) + "\n")


# --- Detection ---


def _detect_proxmox_resources() -> dict:
    """Detect Proxmox VE resources (storage, bridges, node name)."""
    detected = {
        "api_url": "https://127.0.0.1:8006",
        "node_name": socket.gethostname(),
        "storages": [],
        "bridges": [],
        "token_exists": False,
    }

    # Get storage pools
    # pvesm status output format:
    # Name             Type     Status           Total            Used       Available        %
    # local             dir     active       102297016        8654608        88423628    8.46%
    # Values are in KB (kibibytes)
    try:
        result = subprocess.run(
            ["pvesm", "status", "-content", "images"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            if lines:
                # Default column indices (Name, Type, Status, Total, Used, Available, %)
                avail_col = 5  # 0-indexed, 'Available' is typically column 5

                for line in lines[1:]:  # Skip header
                    parts = line.split()
                    if len(parts) >= 6:
                        # Available is in KB, convert to bytes then to GB
                        try:
                            avail_kb = int(parts[avail_col])
                            avail_bytes = avail_kb * 1024  # KB to bytes
                            avail_gb = avail_bytes / (1024**3)
                        except (ValueError, IndexError):
                            avail_bytes = 0
                            avail_gb = 0.0

                        detected["storages"].append(
                            {
                                "name": parts[0],
                                "type": parts[1],
                                "status": parts[2],
                                "avail": avail_bytes,
                                "avail_human": f"{avail_gb:.1f} GB",
                            }
                        )
    except Exception:
        # Fallback
        detected["storages"] = [
            {"name": "local-lvm", "type": "lvmthin", "avail_human": "Unknown"}
        ]

    # Get network bridges
    try:
        result = subprocess.run(
            ["ip", "-j", "link", "show", "type", "bridge"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            bridges = json.loads(result.stdout)
            detected["bridges"] = [b["ifname"] for b in bridges]
    except Exception:
        detected["bridges"] = ["vmbr0"]

    # Check if API token exists
    token_file = Path("/etc/blockhost/pve-token")
    detected["token_exists"] = token_file.exists()

    return detected


# --- Finalization Functions ---


def finalize_token(config: dict) -> tuple[bool, Optional[str]]:
    """Create Proxmox API token."""
    try:
        proxmox = config.get("provisioner", {})
        user = proxmox.get("user", "root@pam")

        # Create API token using pveum
        token_name = "blockhost"
        result = subprocess.run(
            [
                "pveum",
                "user",
                "token",
                "add",
                user,
                token_name,
                "--privsep",
                "0",
                "--output-format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            # Parse token from JSON output
            token_data = json.loads(result.stdout)
            token_value = token_data.get("value", "")
            if token_value:
                token_id = f"{user}!{token_name}"

                # Write terraform.tfvars
                terraform_dir = Path("/var/lib/blockhost/terraform")
                terraform_dir.mkdir(parents=True, exist_ok=True)

                tfvars = {
                    "proxmox_api_token": f"{token_id}={token_value}",
                    "proxmox_node": proxmox.get("node"),
                    "proxmox_storage": proxmox.get("storage"),
                    "proxmox_bridge": proxmox.get("bridge"),
                }
                _write_tfvars(terraform_dir / "terraform.tfvars", tfvars)

                # Save token to /etc/blockhost/pve-token
                token_file = Path("/etc/blockhost/pve-token")
                token_file.write_text(f"{token_id}={token_value}")
                _set_blockhost_ownership(token_file, 0o640)

                return True, None

        return False, "Failed to create API token"
    except Exception as e:
        return False, str(e)


def finalize_terraform(config: dict) -> tuple[bool, Optional[str]]:
    """Configure Terraform with bpg/proxmox provider for VM provisioning."""
    try:
        terraform_dir = Path("/var/lib/blockhost/terraform")
        terraform_dir.mkdir(parents=True, exist_ok=True)

        config_dir = Path("/etc/blockhost")
        config_dir.mkdir(parents=True, exist_ok=True)

        proxmox = config.get("provisioner", {})
        node_name = proxmox.get("node", socket.gethostname())

        # Generate SSH keypair for Terraform to use
        ssh_key_file = config_dir / "terraform_ssh_key"
        ssh_pub_file = config_dir / "terraform_ssh_key.pub"

        if not ssh_key_file.exists():
            result = subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-f",
                    str(ssh_key_file),
                    "-N",
                    "",
                    "-C",
                    "terraform@blockhost",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return False, f"SSH keygen failed: {result.stderr}"

            # Set correct permissions
            _set_blockhost_ownership(ssh_key_file, 0o640)
            ssh_pub_file.chmod(0o644)

        # Add public key to root's authorized_keys
        authorized_keys = Path("/root/.ssh/authorized_keys")
        authorized_keys.parent.mkdir(parents=True, exist_ok=True)

        pub_key = ssh_pub_file.read_text().strip()

        # Check if key already exists
        existing_keys = ""
        if authorized_keys.exists():
            existing_keys = authorized_keys.read_text()

        if pub_key not in existing_keys:
            with open(authorized_keys, "a") as f:
                f.write(f"\n{pub_key}\n")
            authorized_keys.chmod(0o600)

        # Write provider.tf.json with bpg/proxmox provider
        provider_config = {
            "terraform": {
                "required_providers": {
                    "proxmox": {"source": "bpg/proxmox", "version": "~> 0.93.0"}
                }
            },
            "provider": {
                "proxmox": {
                    "endpoint": "https://127.0.0.1:8006",
                    "api_token": "${var.proxmox_api_token}",
                    "insecure": True,
                    "ssh": {
                        "agent": False,
                        "username": "root",
                        "private_key": '${file("/etc/blockhost/terraform_ssh_key")}',
                        "node": [{"name": node_name, "address": "127.0.0.1"}],
                    },
                }
            },
        }

        provider_file = terraform_dir / "provider.tf.json"
        provider_file.write_text(json.dumps(provider_config, indent=2))

        # Write variables.tf.json with wizard values
        variables_config = {
            "variable": {
                "proxmox_api_token": {
                    "type": "string",
                    "description": "Proxmox API token in format user@realm!tokenid=secret",
                    "sensitive": True,
                },
                "proxmox_node": {
                    "type": "string",
                    "description": "Proxmox node name",
                    "default": node_name,
                },
                "proxmox_storage": {
                    "type": "string",
                    "description": "Storage pool for VM disks",
                    "default": proxmox.get("storage", "local-lvm"),
                },
                "proxmox_bridge": {
                    "type": "string",
                    "description": "Network bridge for VMs",
                    "default": proxmox.get("bridge", "vmbr0"),
                },
                "template_vmid": {
                    "type": "number",
                    "description": "VMID of the base VM template",
                    "default": proxmox.get("template_vmid", 9001),
                },
                "vmid_start": {
                    "type": "number",
                    "description": "Start of VMID range for provisioned VMs",
                    "default": proxmox.get("vmid_start", 100),
                },
                "vmid_end": {
                    "type": "number",
                    "description": "End of VMID range for provisioned VMs",
                    "default": proxmox.get("vmid_end", 999),
                },
            }
        }

        variables_file = terraform_dir / "variables.tf.json"
        variables_file.write_text(json.dumps(variables_config, indent=2))

        # Run terraform init
        result = subprocess.run(
            ["terraform", "init"],
            cwd=terraform_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            return False, f"Terraform init failed: {result.stderr}"

        return True, None
    except subprocess.TimeoutExpired:
        return False, "Terraform init timed out"
    except Exception as e:
        return False, str(e)


def finalize_db_config(config: dict) -> tuple[bool, Optional[str]]:
    """Write /etc/blockhost/db.yaml with provisioner runtime config."""
    try:
        provisioner = config.get("provisioner", {})

        db_config = {
            "db_file": "/var/lib/blockhost/vm-db.json",
            "terraform_dir": provisioner.get("terraform_dir", "/var/lib/blockhost/terraform"),
            "bridge": provisioner.get("bridge", "vmbr0"),
            "gc_grace_days": int(provisioner.get("gc_grace_days", 7)),
            "vmid_range": {
                "start": int(provisioner.get("vmid_start", 100)),
                "end": int(provisioner.get("vmid_end", 999)),
            },
            "ip_pool": {
                "network": provisioner.get("ip_network", "192.168.122.0/24"),
                "start": provisioner.get("ip_start", "192.168.122.200"),
                "end": provisioner.get("ip_end", "192.168.122.250"),
                "gateway": provisioner.get("gateway", "192.168.122.1"),
            },
        }

        config_dir = Path("/etc/blockhost")
        config_dir.mkdir(parents=True, exist_ok=True)

        db_yaml_path = config_dir / "db.yaml"
        db_yaml_path.write_text(yaml.dump(db_config, default_flow_style=False))
        _set_blockhost_ownership(db_yaml_path, 0o640)

        return True, None
    except Exception as e:
        return False, str(e)


def finalize_bridge(config: dict) -> tuple[bool, Optional[str]]:
    """Ensure network bridge exists for VM networking.

    Uses the PVE API (pvesh) to create the bridge so that Proxmox manages
    /etc/network/interfaces itself. This ensures the bridge survives reboot.

    On bare metal Proxmox installs, vmbr0 is created by the installer.
    On nested/VM installs or custom setups, it may not exist.
    """
    try:
        proxmox = config.get("provisioner", {})
        bridge_name = proxmox.get("bridge", "vmbr0")
        node_name = proxmox.get("node", socket.gethostname())

        # Check if bridge already exists
        bridge_path = Path(f"/sys/class/net/{bridge_name}")
        if bridge_path.exists():
            return True, None

        # Find the primary network interface (the one with a default route)
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0 or not result.stdout.strip():
            return False, "Could not determine default network interface"

        # Parse: "default via 192.168.122.1 dev ens3 proto dhcp src 192.168.122.195 metric 100"
        parts = result.stdout.strip().split()
        try:
            dev_idx = parts.index("dev")
            primary_iface = parts[dev_idx + 1]
        except (ValueError, IndexError):
            return False, f"Could not parse default route: {result.stdout}"

        # Get current IP configuration from the primary interface
        result = subprocess.run(
            ["ip", "-j", "addr", "show", primary_iface],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            return False, f"Could not get IP config for {primary_iface}"

        iface_info = json.loads(result.stdout)

        # Find IPv4 address
        ipv4_addr = None
        ipv4_prefix = None

        for info in iface_info:
            for addr_info in info.get("addr_info", []):
                if addr_info.get("family") == "inet":
                    ipv4_addr = addr_info.get("local")
                    ipv4_prefix = addr_info.get("prefixlen")
                    break

        if not ipv4_addr or not ipv4_prefix:
            return False, f"Could not find IPv4 address on {primary_iface}"

        # Get gateway from default route
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        parts = result.stdout.strip().split()
        try:
            via_idx = parts.index("via")
            gateway = parts[via_idx + 1]
        except (ValueError, IndexError):
            return False, "Could not determine gateway"

        # Step 1: Create bridge via PVE API
        result = subprocess.run(
            [
                "pvesh",
                "create",
                f"/nodes/{node_name}/network",
                "--iface",
                bridge_name,
                "--type",
                "bridge",
                "--bridge_ports",
                primary_iface,
                "--autostart",
                "1",
                "--cidr",
                f"{ipv4_addr}/{ipv4_prefix}",
                "--gateway",
                gateway,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0 and "already exists" not in (result.stderr or ""):
            return False, f"PVE bridge creation failed: {result.stderr or result.stdout}"

        # Step 2: Set primary interface to manual (bridge port)
        subprocess.run(
            [
                "pvesh",
                "set",
                f"/nodes/{node_name}/network/{primary_iface}",
                "--type",
                "eth",
                "--autostart",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Non-fatal if this fails -- PVE may handle it automatically

        # Step 3: Apply the staged network config
        # This rewrites /etc/network/interfaces and reloads networking
        result = subprocess.run(
            ["pvesh", "set", f"/nodes/{node_name}/network"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            # Apply failed -- try ifreload as fallback
            subprocess.run(
                ["ifreload", "-a"],
                capture_output=True,
                timeout=30,
            )

        # Step 4: Ephemeral fallback -- bring bridge up with ip commands
        # if pvesh apply didn't fully activate it during this session
        if not Path(f"/sys/class/net/{bridge_name}").exists():
            subprocess.run(
                ["ip", "link", "add", "name", bridge_name, "type", "bridge"],
                capture_output=True,
                timeout=10,
            )

        # Ensure bridge is up and has IP
        subprocess.run(
            ["ip", "link", "set", bridge_name, "up"],
            capture_output=True,
            timeout=10,
        )

        if not Path(f"/sys/class/net/{bridge_name}").exists():
            return False, f"Failed to create bridge {bridge_name}"

        # Add primary interface to bridge if not already
        subprocess.run(
            ["ip", "link", "set", primary_iface, "master", bridge_name],
            capture_output=True,
            timeout=10,
        )

        # Ensure IP is on the bridge (move from primary if needed)
        ip_check = subprocess.run(
            ["ip", "addr", "show", bridge_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ipv4_addr not in (ip_check.stdout or ""):
            subprocess.run(
                [
                    "ip",
                    "addr",
                    "del",
                    f"{ipv4_addr}/{ipv4_prefix}",
                    "dev",
                    primary_iface,
                ],
                capture_output=True,
                timeout=10,
            )
            subprocess.run(
                [
                    "ip",
                    "addr",
                    "add",
                    f"{ipv4_addr}/{ipv4_prefix}",
                    "dev",
                    bridge_name,
                ],
                capture_output=True,
                timeout=10,
            )
            subprocess.run(
                ["ip", "route", "add", "default", "via", gateway, "dev", bridge_name],
                capture_output=True,
                timeout=10,
            )

        return True, None

    except subprocess.TimeoutExpired:
        return False, "Network configuration timed out"
    except Exception as e:
        return False, str(e)


def finalize_template(config: dict) -> tuple[bool, Optional[str]]:
    """Build VM template with libpam-web3."""
    try:
        proxmox = config.get("provisioner", {})
        template_vmid = proxmox.get("template_vmid", 9001)
        storage = proxmox.get("storage", "local-lvm")

        # Check if template already exists
        template_check = subprocess.run(
            ["qm", "status", str(template_vmid)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if template_check.returncode == 0:
            # Template VM already exists, skip building
            return True, None

        # Find libpam-web3 .deb in template-packages directory
        template_pkg_dir = Path("/var/lib/blockhost/template-packages")
        libpam_deb = None

        if template_pkg_dir.exists():
            debs = list(template_pkg_dir.glob("libpam-web3_*.deb"))
            if debs:
                # Use the most recent one if multiple exist
                libpam_deb = str(
                    sorted(debs, key=lambda p: p.stat().st_mtime, reverse=True)[0]
                )

        # Build template - use installed command from .deb package
        build_script = Path("/usr/bin/blockhost-build-template")
        if build_script.exists():
            # Set up environment for build script
            env = os.environ.copy()
            env["TEMPLATE_VMID"] = str(template_vmid)
            env["STORAGE"] = storage
            env["PROXMOX_HOST"] = "localhost"

            if libpam_deb:
                env["LIBPAM_WEB3_DEB"] = libpam_deb

            result = subprocess.run(
                [str(build_script)],
                capture_output=True,
                text=True,
                timeout=1800,  # 30 minutes
                env=env,
            )

            if result.returncode != 0:
                return False, result.stderr or "Template build failed"
        else:
            # Template build script not found - skip for now
            return True, None

        return True, None
    except subprocess.TimeoutExpired:
        return False, "Template build timed out"
    except Exception as e:
        return False, str(e)
