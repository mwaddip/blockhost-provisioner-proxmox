#!/bin/bash
# Detect whether this host has Proxmox VE installed
# Exits 0 if Proxmox is detected, 1 otherwise

command -v pvesh >/dev/null 2>&1
