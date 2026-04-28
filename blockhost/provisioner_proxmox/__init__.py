"""Proxmox VE provisioner plugin for BlockHost."""

import json
import re
import socket
import ssl
import urllib.request
from pathlib import Path

from blockhost.config import load_db_config


def get_terraform_dir() -> Path:
    """Get the Terraform working directory from db config."""
    return Path(load_db_config()["terraform_dir"])


def sanitize_resource_name(name: str) -> str:
    """Convert VM name to valid Terraform resource name."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def load_tfvars(path: Path) -> dict:
    """Parse a terraform.tfvars file as `key = "value"` pairs.

    Returns an empty dict if the file does not exist. Strips matched surrounding
    double quotes from values; does not handle escapes or HCL expressions.
    """
    if not path.exists():
        return {}

    variables = {}
    for line in path.read_text().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            variables[key.strip()] = value.strip().strip('"')
    return variables


def load_pve_credentials() -> tuple[str, str, str]:
    """Load Proxmox API credentials.

    Returns:
        (api_url, api_token, node_name)

    api_token is read from /etc/blockhost/pve-token.
    node_name is read from terraform.tfvars (proxmox_node key),
    falling back to the system hostname.
    """
    token_file = Path("/etc/blockhost/pve-token")
    if not token_file.exists():
        raise FileNotFoundError(f"PVE token not found: {token_file}")
    api_token = token_file.read_text().strip()

    tfvars = load_tfvars(get_terraform_dir() / "terraform.tfvars")
    node_name = tfvars.get("proxmox_node") or socket.gethostname()

    return ("https://127.0.0.1:8006", api_token, node_name)


def pve_api_get(path: str, credentials: tuple = None, timeout: int = 5) -> dict:
    """GET a Proxmox API endpoint and return the JSON 'data' key.

    Args:
        path: API path (e.g. '/api2/json/nodes/pve/qemu/100/status/current')
        credentials: (api_url, api_token, node_name) tuple, or None to auto-load
        timeout: HTTP timeout in seconds (default 5)

    Returns:
        The 'data' value from the JSON response.

    Raises:
        urllib.error.URLError: On HTTP errors or timeouts.
        KeyError: If response has no 'data' key.
    """
    if credentials is None:
        credentials = load_pve_credentials()
    api_url, api_token, _ = credentials

    url = f"{api_url}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"PVEAPIToken={api_token}")

    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = json.loads(resp.read())
    return body["data"]
