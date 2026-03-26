#!/bin/bash
set -euo pipefail

# Proxmox Template Builder
# Creates a minimal Debian 12 cloud-init ready template with libpam-web3
#
# Supports two modes:
# - Local: PROXMOX_HOST=localhost (default) - runs directly on Proxmox host
# - Remote: PROXMOX_HOST=root@hostname - runs via SSH (for development)

# Configuration with defaults
PROXMOX_HOST="${PROXMOX_HOST:-localhost}"
TEMPLATE_VMID="${TEMPLATE_VMID:-9001}"
STORAGE="${STORAGE:-local-lvm}"
IMAGE_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
IMAGE_NAME="debian-12-genericcloud-amd64.qcow2"
TEMPLATE_NAME="debian-12-web3-template"

# Locate ALL template packages from standard directory
TEMPLATE_DEBS=()
for deb in /var/lib/blockhost/template-packages/*.deb; do
    [ -f "$deb" ] || continue
    TEMPLATE_DEBS+=("$deb")
done

if [ ${#TEMPLATE_DEBS[@]} -eq 0 ]; then
    echo "Error: No template packages found in /var/lib/blockhost/template-packages/"
    exit 1
fi

echo "=== Proxmox Template Builder ==="
echo "Host: ${PROXMOX_HOST}"
echo "Template VMID: ${TEMPLATE_VMID}"
echo "Storage: ${STORAGE}"
echo "Template packages:"
for deb in "${TEMPLATE_DEBS[@]}"; do
    echo "  $(basename "$deb")"
done
echo ""

# Helper functions for local/remote execution
run_on_host() {
    if [[ "$PROXMOX_HOST" == "localhost" ]]; then
        "$@"
    else
        ssh "root@$PROXMOX_HOST" "$@"
    fi
}

copy_to_host() {
    local src="$1"
    local dest="$2"
    if [[ "$PROXMOX_HOST" == "localhost" ]]; then
        cp "$src" "$dest"
    else
        scp "$src" "root@$PROXMOX_HOST:$dest"
    fi
}

run_script_on_host() {
    # Run a script block on the host (handles heredoc-style commands)
    if [[ "$PROXMOX_HOST" == "localhost" ]]; then
        bash -c "$1"
    else
        ssh "root@$PROXMOX_HOST" "$1"
    fi
}

# Check prerequisites
if ! command -v virt-customize &> /dev/null; then
    echo "Error: virt-customize not found. Install with: sudo apt install libguestfs-tools"
    exit 1
fi

# Download image locally
echo "Downloading ${IMAGE_NAME}..."
wget -N -P /tmp "${IMAGE_URL}"

# Make a working copy of the image for customization
WORK_IMAGE="/tmp/${IMAGE_NAME%.qcow2}-customized.qcow2"
echo "Creating working copy for customization..."
cp "/tmp/${IMAGE_NAME}" "${WORK_IMAGE}"

# Build virt-customize arguments dynamically
# Note: sudo required on Ubuntu because /boot/vmlinuz-* is not world-readable
echo "Installing packages into image..."
CUSTOMIZE_ARGS=(
    -a "${WORK_IMAGE}"
    --install qemu-guest-agent
    --run-command 'curl -fsSL https://deb.nodesource.com/setup_22.x | bash -'
    --install nodejs
)

# Copy all template debs into the image
for deb in "${TEMPLATE_DEBS[@]}"; do
    CUSTOMIZE_ARGS+=(--copy-in "$deb":/tmp)
done

# Install all at once, then fix dependencies
DEB_NAMES=""
for deb in "${TEMPLATE_DEBS[@]}"; do
    DEB_NAMES+="/tmp/$(basename "$deb") "
done
CUSTOMIZE_ARGS+=(--run-command "dpkg -i ${DEB_NAMES}|| apt-get install -f -y")

# Fix sshd_config: ensure Include directive exists and disable conflicting KbdInteractiveAuthentication
# Newer Debian 12 images have KbdInteractiveAuthentication no in the main config, which overrides
# sshd_config.d/ snippets (first-match-wins). Comment it out so the drop-in can control it.
CUSTOMIZE_ARGS+=(
    --run-command 'grep -q "^Include /etc/ssh/sshd_config.d" /etc/ssh/sshd_config || sed -i "1i Include /etc/ssh/sshd_config.d/*.conf" /etc/ssh/sshd_config'
    --run-command 'sed -i "s/^KbdInteractiveAuthentication no/#KbdInteractiveAuthentication no  # overridden by sshd_config.d/" /etc/ssh/sshd_config'
)

# Enable services
CUSTOMIZE_ARGS+=(
    --run-command 'systemctl enable web3-auth-svc'
    --run-command 'systemctl enable qemu-guest-agent'
)

# Clean up copied debs
for deb in "${TEMPLATE_DEBS[@]}"; do
    CUSTOMIZE_ARGS+=(--delete "/tmp/$(basename "$deb")")
done

sudo virt-customize "${CUSTOMIZE_ARGS[@]}"

IMAGE_SIZE=$(du -h "${WORK_IMAGE}" | cut -f1)
echo "Customized image size: ${IMAGE_SIZE}"

# Upload/copy to Proxmox template directory
echo "Uploading customized image to ${PROXMOX_HOST}..."
run_on_host mkdir -p /var/lib/vz/template/qcow2
copy_to_host "${WORK_IMAGE}" "/var/lib/vz/template/qcow2/${IMAGE_NAME}"

# Clean up working copy
rm -f "${WORK_IMAGE}"

# Create template on Proxmox
echo "Creating template VM ${TEMPLATE_VMID}..."

TEMPLATE_SCRIPT=$(cat << EOF
set -euo pipefail

# Remove existing template if present
if qm status ${TEMPLATE_VMID} &>/dev/null; then
    echo "Removing existing VM ${TEMPLATE_VMID}..."
    qm destroy ${TEMPLATE_VMID} --purge
fi

# Create VM
qm create ${TEMPLATE_VMID} --name "${TEMPLATE_NAME}" \
    --memory 512 --cores 1 \
    --net0 virtio,bridge=vmbr0 \
    --ostype l26 --scsihw virtio-scsi-pci

# Import disk and capture output to get the actual volume ID
# Output format varies:
#   "Successfully imported disk as 'unused0:local-lvm:vm-9001-disk-0'"
#   "Successfully imported disk as 'unused0:local:9001/vm-9001-disk-0.qcow2'"
echo "Importing disk..."
IMPORT_OUTPUT=\$(qm importdisk ${TEMPLATE_VMID} /var/lib/vz/template/qcow2/${IMAGE_NAME} ${STORAGE} 2>&1)
echo "Import output:"
echo "\$IMPORT_OUTPUT"

# Extract the volume ID from the import output
# Look for the pattern after "as 'unused0:" and before the closing quote
# Note: || true prevents pipefail from exiting when grep finds no match
VOLUME_ID=\$(echo "\$IMPORT_OUTPUT" | grep -oP "as 'unused0:\K[^']+" | head -1 || true)

# Fallback: try to match STORAGE:something pattern directly
if [[ -z "\$VOLUME_ID" ]]; then
    echo "Primary regex failed, trying fallback..."
    VOLUME_ID=\$(echo "\$IMPORT_OUTPUT" | grep -oP "${STORAGE}:[^'\"[:space:]]+" | head -1 || true)
fi

# Second fallback: look for any volume pattern in the last line
if [[ -z "\$VOLUME_ID" ]]; then
    echo "Fallback regex failed, trying line-based extraction..."
    # Get the line containing "imported" and extract volume after unused0:
    VOLUME_ID=\$(echo "\$IMPORT_OUTPUT" | grep -i "imported" | sed -n "s/.*unused0:\([^']*\).*/\1/p" | head -1 || true)
fi

if [[ -z "\$VOLUME_ID" ]]; then
    echo "Error: Failed to extract volume ID from import output"
    echo "Please check the output format above and report this issue"
    exit 1
fi
echo "Extracted volume ID: \$VOLUME_ID"

# Attach disk using the actual volume ID (works for both LVM and directory storage)
qm set ${TEMPLATE_VMID} --scsi0 "\$VOLUME_ID"
qm set ${TEMPLATE_VMID} --boot order=scsi0

# Add cloud-init drive
qm set ${TEMPLATE_VMID} --ide2 ${STORAGE}:cloudinit

# Enable QEMU guest agent
qm set ${TEMPLATE_VMID} --agent enabled=1

# Serial console for cloud images
qm set ${TEMPLATE_VMID} --serial0 socket --vga serial0

# Convert to template
echo "Converting to template..."
qm template ${TEMPLATE_VMID}

echo "Template ${TEMPLATE_VMID} created successfully!"
qm config ${TEMPLATE_VMID}
EOF
)

run_script_on_host "$TEMPLATE_SCRIPT"

echo ""
echo "=== Template creation complete ==="
echo "Template VMID: ${TEMPLATE_VMID}"
echo "You can now clone this template using Terraform"
