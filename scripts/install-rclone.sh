#!/usr/bin/env bash
# install-rclone.sh — pinned static rclone binary for the platform backup job.
#
# `ams.platform.backup` uploads snapshots to Cloudflare R2 with `rclone copyto`.
# The binary is one pinned static file in the harness store, beside the pinned
# Caddy that `deploy/install-host.sh` installs, for the same reasons Q3 gave for
# rejecting `apt install caddy`: a distro package brings a system user, a root
# unit and an upgrade cadence nobody here controls, and none of that is wanted
# for a program the harness invokes as itself, twice a day, with an argv it
# constructs.
#
# Idempotent: an existing binary matching RCLONE_BIN_SHA256 is left alone.
#
# This lives in scripts/ rather than in deploy/install-host.sh because that file
# is owned by another task this wave. T4.4 folds it in; until then, run it once
# on the host as root:
#
#     sudo scripts/install-rclone.sh
#
# Bumping the version means updating BOTH hashes together. RCLONE_ZIP_SHA256 is
# the value upstream publishes at https://downloads.rclone.org/v<ver>/SHA256SUMS;
# RCLONE_BIN_SHA256 is the hash of the `rclone` file extracted from that verified
# zip (print it with `sha256sum` after a manual extract).
set -euo pipefail

HARNESS_USER=${HARNESS_USER:-harness}
STORE_MNT=${STORE_MNT:-/home/$HARNESS_USER/store}

RCLONE_VERSION=${RCLONE_VERSION:-1.75.0}
RCLONE_ARCH=${RCLONE_ARCH:-linux-amd64}
RCLONE_ZIP_SHA256=aa2804e08f48250e71009c727124b6341cd0288465804a9a09d14663cabafbaa
RCLONE_BIN_SHA256=f3f9aff817f9766029e50adf9a7963c169e475b8f10c7927823568a0d9443db7
RCLONE_BIN=$STORE_MNT/bin/rclone

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root (it installs into $STORE_MNT/bin and chowns to $HARNESS_USER)." >&2
  exit 1
fi

if [ ! -d "$STORE_MNT" ]; then
  echo "ERROR: $STORE_MNT does not exist; run deploy/install-host.sh first." >&2
  exit 1
fi

install -d -o "$HARNESS_USER" -g "$HARNESS_USER" -m 0755 "$STORE_MNT/bin"

if [ -x "$RCLONE_BIN" ] && [ "$(sha256sum "$RCLONE_BIN" | cut -d' ' -f1)" = "$RCLONE_BIN_SHA256" ]; then
  echo "rclone $RCLONE_VERSION already installed at $RCLONE_BIN"
  "$RCLONE_BIN" version | head -1
  exit 0
fi

RELEASE=rclone-v${RCLONE_VERSION}-${RCLONE_ARCH}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

curl -fsSL -o "$TMP/rclone.zip" "https://downloads.rclone.org/v${RCLONE_VERSION}/${RELEASE}.zip"
echo "$RCLONE_ZIP_SHA256  $TMP/rclone.zip" | sha256sum -c -
unzip -q -o "$TMP/rclone.zip" "$RELEASE/rclone" -d "$TMP"
echo "$RCLONE_BIN_SHA256  $TMP/$RELEASE/rclone" | sha256sum -c -
install -o "$HARNESS_USER" -g "$HARNESS_USER" -m 0755 "$TMP/$RELEASE/rclone" "$RCLONE_BIN"

echo "installed rclone $RCLONE_VERSION to $RCLONE_BIN"
"$RCLONE_BIN" version | head -1
