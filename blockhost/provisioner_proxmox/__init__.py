"""Proxmox VE provisioner plugin for BlockHost."""

import re
from pathlib import Path

from blockhost.config import load_db_config


def get_terraform_dir() -> Path:
    """Get the Terraform working directory from db config."""
    return Path(load_db_config()["terraform_dir"])


def sanitize_resource_name(name: str) -> str:
    """Convert VM name to valid Terraform resource name."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)
