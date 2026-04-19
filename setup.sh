#!/usr/bin/env bash
# setup.sh — Member 1
#
# One-time setup: pull a minimal base image and import it into the local store.
# Run this ONCE before any builds. After this, everything works offline.
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh
#
# What it does:
#   1. Pulls alpine:3.18 from Docker Hub (requires Docker just for this step)
#   2. Saves it as a tar
#   3. Imports it into ~/.docksmith/ via `docksmith import-image`
#   4. Removes the temporary tar
#
# After this script, you never need Docker or network again.

set -e

IMAGE="alpine:3.18"
TAR_FILE="/tmp/alpine-3.18.tar"
DOCKSMITH_NAME="alpine:3.18"

echo "=== Docksmith One-Time Setup ==="
echo ""

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is required." >&2
    exit 1
fi

# Check Docker is available for the one-time pull
if ! command -v docker &>/dev/null; then
    echo "Error: Docker is required for the one-time image pull." >&2
    echo "Install Docker, run this script once, then Docker is no longer needed." >&2
    exit 1
fi

echo "Step 1: Pulling $IMAGE from Docker Hub..."
docker pull "$IMAGE"

echo ""
echo "Step 2: Saving to $TAR_FILE ..."
docker save "$IMAGE" -o "$TAR_FILE"

echo ""
echo "Step 3: Importing into local Docksmith store..."
python3 main.py import-image "$TAR_FILE" "$DOCKSMITH_NAME"

echo ""
echo "Step 4: Cleaning up..."
rm -f "$TAR_FILE"

echo ""
echo "=== Setup complete! ==="
echo "Base image '$DOCKSMITH_NAME' is now available in ~/.docksmith/"
echo "You can now run builds fully offline."
echo ""
echo "Try:"
echo "  python3 main.py build -t myapp:latest ."
echo "  python3 main.py images"
