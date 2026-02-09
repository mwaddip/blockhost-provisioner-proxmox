#!/bin/bash
#
# Proxmox VE Provisioner — First Boot Hook
#
# Called by the main first-boot.sh to install provisioner-specific dependencies.
# This script installs:
# - Proxmox VE
# - Terraform
# - libguestfs-tools
#
# Uses step markers in STATE_DIR for idempotent execution.
#

set -e

STATE_DIR="${STATE_DIR:-/var/lib/blockhost}"
LOG_FILE="${LOG_FILE:-/var/log/blockhost-firstboot.log}"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [provisioner-proxmox] $1"
    echo "$msg" >> "$LOG_FILE"
}

#
# Step: Configure hostname for Proxmox
#
# CRITICAL: Proxmox requires the hostname to resolve to the real IP address,
# NOT to 127.0.1.1 (which Debian preseed creates by default).
#
STEP_HOSTNAME="${STATE_DIR}/.step-hostname"
if [ ! -f "$STEP_HOSTNAME" ]; then
    log "Configuring hostname for Proxmox..."

    CURRENT_IP=$(ip -4 addr show scope global 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
    if [ -z "$CURRENT_IP" ]; then
        log "ERROR: No network IP for hostname config"
        exit 1
    fi

    HOSTNAME=$(hostname)
    FQDN="${HOSTNAME}.local"

    sed -i '/^127\.0\.1\.1/d' /etc/hosts
    sed -i "/[[:space:]]${HOSTNAME}$/d" /etc/hosts
    sed -i "/[[:space:]]${HOSTNAME}[[:space:]]/d" /etc/hosts
    sed -i "/^127\.0\.0\.1/a ${CURRENT_IP}\t${FQDN}\t${HOSTNAME}" /etc/hosts

    touch "$STEP_HOSTNAME"
    log "Hostname configured."
else
    log "Hostname already configured, skipping."
fi

#
# Step: Install Proxmox VE
#
STEP_PROXMOX="${STATE_DIR}/.step-proxmox"
if [ ! -f "$STEP_PROXMOX" ]; then
    log "Installing Proxmox VE..."

    # Configure apt proxy if available
    APT_PROXY="http://192.168.122.1:3142"
    if curl -s --connect-timeout 2 "$APT_PROXY" >/dev/null 2>&1; then
        log "Using apt proxy: $APT_PROXY"
        echo "Acquire::http::Proxy \"$APT_PROXY\";" > /etc/apt/apt.conf.d/00proxy
    fi

    # Add Proxmox repository
    if [ ! -f /etc/apt/sources.list.d/pve-install-repo.list ]; then
        echo "deb [arch=amd64] http://download.proxmox.com/debian/pve bookworm pve-no-subscription" > /etc/apt/sources.list.d/pve-install-repo.list
    fi

    # Add Proxmox GPG key
    if [ ! -f /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg ]; then
        wget -q https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg -O /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg
    fi

    # Disable Proxmox enterprise repo (requires paid subscription)
    if [ -f /etc/apt/sources.list.d/pve-enterprise.list ]; then
        rm -f /etc/apt/sources.list.d/pve-enterprise.list
        log "Removed pve-enterprise.list (no subscription)."
    fi

    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y proxmox-ve postfix open-iscsi chrony python3-ecdsa jq
    update-grub

    touch "$STEP_PROXMOX"

    # Restart PVE services — they were started by dpkg triggers before
    # pve-cluster could initialize (hostname wasn't in /etc/hosts yet).
    # pvedaemon caches the "offline" state and needs a restart.
    log "Restarting PVE services..."
    systemctl reset-failed pve-cluster 2>/dev/null || true
    systemctl restart pve-cluster
    sleep 2
    systemctl restart pvedaemon pveproxy pvestatd 2>/dev/null || true

    log "Proxmox VE installed."
else
    log "Proxmox VE already installed, skipping."
fi

#
# Step: Install Terraform and libguestfs-tools
#
STEP_TERRAFORM="${STATE_DIR}/.step-terraform"
if [ ! -f "$STEP_TERRAFORM" ]; then
    log "Installing Terraform and libguestfs-tools..."

    # Disable Proxmox enterprise repo if it reappeared (e.g. after proxmox-ve install)
    if [ -f /etc/apt/sources.list.d/pve-enterprise.list ]; then
        rm -f /etc/apt/sources.list.d/pve-enterprise.list
        log "Removed pve-enterprise.list (no subscription)."
    fi

    if [ ! -f /usr/share/keyrings/hashicorp-archive-keyring.gpg ]; then
        wget -O- https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
        echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com bookworm main" > /etc/apt/sources.list.d/hashicorp.list
        apt-get update
    fi

    DEBIAN_FRONTEND=noninteractive apt-get install -y terraform libguestfs-tools

    touch "$STEP_TERRAFORM"
    log "Terraform and libguestfs-tools installed."
else
    log "Terraform already installed, skipping."
fi

log "Provisioner first-boot hook complete."
exit 0
