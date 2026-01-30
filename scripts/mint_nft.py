#!/usr/bin/env python3
"""
NFT Minting Script

Mints access credential NFTs after successful VM creation.
Uses Foundry's `cast` CLI for contract interaction.

Requires:
- Foundry (cast) installed: https://getfoundry.sh
- Deployer private key with funds on the target chain
- Signing page HTML file (from libpam-web3)
"""

import base64
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml

PROJECT_DIR = Path(__file__).parent.parent


def get_config_path(filename: str) -> Path:
    """Get config file path, checking /etc/blockhost/ first."""
    etc_path = Path("/etc/blockhost") / filename
    if etc_path.exists():
        return etc_path
    return PROJECT_DIR / "config" / filename


def load_web3_defaults() -> dict:
    """Load web3 default configuration."""
    config_path = get_config_path("web3-defaults.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f)


def read_deployer_key(config: dict) -> str:
    """Read the deployer private key from file."""
    key_file = Path(config["deployer"]["private_key_file"])

    if not key_file.exists():
        raise FileNotFoundError(
            f"Deployer key not found at {key_file}. "
            f"Create it with: cast wallet new | grep 'Private key' | awk '{{print $3}}' > {key_file}"
        )

    return key_file.read_text().strip()


def load_signing_page(config: dict) -> str:
    """
    Load and base64-encode the signing page HTML.

    The signing page is embedded in the NFT's animationUrlBase64 field
    and extracted by VMs to serve locally for wallet authentication.

    Returns just the base64-encoded content (not a data URI).
    """
    html_path = Path(config.get("signing_page", {}).get(
        "html_path",
        "/usr/share/libpam-web3/signing-page/index.html"
    ))

    # Also check local development path
    if not html_path.exists():
        dev_path = Path.home() / "projects/libpam-web3/signing-page/index.html"
        if dev_path.exists():
            html_path = dev_path

    if not html_path.exists():
        raise FileNotFoundError(
            f"Signing page HTML not found at {html_path}. "
            f"Install libpam-web3 or set signing_page.html_path in config."
        )

    html_content = html_path.read_text()
    return base64.b64encode(html_content.encode()).decode()


def mint_nft(
    owner_wallet: str,
    machine_id: str,
    user_encrypted: str = "0x",
    decrypt_message: str = "",
    config: Optional[dict] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Mint an access credential NFT to the specified wallet.

    Args:
        owner_wallet: Ethereum address to receive the NFT
        machine_id: VM name/machine ID (used in description)
        user_encrypted: Hex-encoded encrypted connection details (from subscription system)
        decrypt_message: Message the user signed during subscription
        config: Web3 config dict (loaded from web3-defaults.yaml if None)
        dry_run: If True, print the command but don't execute

    Returns:
        Transaction hash if successful, None if dry run
    """
    if config is None:
        config = load_web3_defaults()

    nft_contract = config["blockchain"]["nft_contract"]
    rpc_url = config["blockchain"]["rpc_url"]

    # Load signing page HTML as base64
    print("Loading signing page...")
    signing_page_base64 = load_signing_page(config)
    print(f"Signing page size: {len(signing_page_base64)} bytes (base64)")

    # Read deployer key
    deployer_key = read_deployer_key(config)

    # Build cast command with new contract signature
    # Parameters: to, userEncrypted, decryptMessage, description, imageUri, animationUrlBase64, expiresAt
    cmd = [
        "cast", "send",
        nft_contract,
        "mint(address,bytes,string,string,string,string,uint256)",
        owner_wallet,
        user_encrypted,                     # Encrypted connection details
        decrypt_message,                    # Message user signed during subscription
        f"Access - {machine_id}",           # description
        "",                                 # imageUri (use default)
        signing_page_base64,                # animationUrlBase64 (just base64, not data URI)
        "0",                                # expiresAt (0 = never)
        "--private-key", deployer_key,
        "--rpc-url", rpc_url,
    ]

    if dry_run:
        # Mask sensitive data in output
        display_cmd = cmd.copy()
        pk_idx = display_cmd.index("--private-key") + 1
        display_cmd[pk_idx] = "0x***REDACTED***"
        # Truncate signing page base64 for display
        for i, arg in enumerate(display_cmd):
            if len(arg) > 100 and not arg.startswith("--") and not arg.startswith("0x"):
                display_cmd[i] = f"{arg[:50]}...***TRUNCATED***"
        print(f"[DRY RUN] Would execute: {' '.join(display_cmd)}")
        return None

    print(f"Minting NFT to {owner_wallet}...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"Minting failed: {result.stderr}")

    # Extract transaction hash from output
    tx_hash = None
    for line in result.stdout.strip().split("\n"):
        if "transactionHash" in line or line.startswith("0x"):
            tx_hash = line.strip().split()[-1]
            break

    if tx_hash:
        print(f"NFT minted! TX: {tx_hash}")
    else:
        print(f"NFT minted! Output: {result.stdout.strip()}")

    return tx_hash


def main():
    """CLI for testing NFT minting."""
    import argparse

    parser = argparse.ArgumentParser(description="Mint access credential NFT")
    parser.add_argument("--owner-wallet", required=True, help="Wallet address to receive the NFT")
    parser.add_argument("--machine-id", required=True, help="Machine ID (used in NFT description)")
    parser.add_argument("--user-encrypted", default="0x",
                        help="Hex-encoded encrypted connection details (default: 0x)")
    parser.add_argument("--decrypt-message", default="",
                        help="Message the user signed during subscription")
    parser.add_argument("--dry-run", action="store_true", help="Print command without executing")

    args = parser.parse_args()

    try:
        tx_hash = mint_nft(
            owner_wallet=args.owner_wallet,
            machine_id=args.machine_id,
            user_encrypted=args.user_encrypted,
            decrypt_message=args.decrypt_message,
            dry_run=args.dry_run,
        )
        if tx_hash:
            print(f"\nTransaction: {tx_hash}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
