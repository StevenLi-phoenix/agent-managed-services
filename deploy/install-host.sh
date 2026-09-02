#!/usr/bin/env bash
# One-time host preparation for the ams harness on Ubuntu 24.04 (run as root).
# Idempotent: re-running is safe. Mirrors what was done by hand on racknerd on
# 2026-09-02; see .claude/state/DECISIONS.md D2-D4, D8 for the why.
set -euo pipefail

HARNESS_USER=${HARNESS_USER:-harness}
HARNESS_HOME=/home/$HARNESS_USER
STORE_IMG=${STORE_IMG:-/var/lib/ams/store.img}
STORE_SIZE=${STORE_SIZE:-6G}
STORE_MNT=$HARNESS_HOME/store
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q uidmap python3.12-venv rsync xfsprogs unzip curl >/dev/null

# 1. Unprivileged harness user with an /etc/subuid + /etc/subgid range (useradd assigns one).
if ! id "$HARNESS_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$HARNESS_USER"
fi
grep -q "^$HARNESS_USER:" /etc/subuid || echo "$HARNESS_USER:100000:65536" >> /etc/subuid
grep -q "^$HARNESS_USER:" /etc/subgid || echo "$HARNESS_USER:100000:65536" >> /etc/subgid
loginctl enable-linger "$HARNESS_USER"
# Services run as mapped uids (100000+) and must traverse into $HARNESS_HOME
# (store, state). 711 = traverse only; harness's own files keep their own modes.
chmod 711 "$HARNESS_HOME"

# 2. Private interpreter copy so the AppArmor userns grant applies only to the harness.
if [ ! -x "$HARNESS_HOME/venv/bin/python3" ]; then
  su -l "$HARNESS_USER" -c "python3 -m venv --copies $HARNESS_HOME/venv"
fi
su -l "$HARNESS_USER" -c 'command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null'

# 3. AppArmor: kernel.apparmor_restrict_unprivileged_userns=1 on 24.04.
install -m 0644 "$REPO_DIR/deploy/apparmor/ams-harness" /etc/apparmor.d/ams-harness
apparmor_parser -r /etc/apparmor.d/ams-harness

# 4. XFS reflink store for venvs + shared uv cache (ext4 root cannot reflink).
mkdir -p "$(dirname "$STORE_IMG")" "$STORE_MNT"
if [ ! -f "$STORE_IMG" ]; then
  fallocate -l "$STORE_SIZE" "$STORE_IMG"
  mkfs.xfs -q -m reflink=1 -L ams-store "$STORE_IMG"
fi
grep -q " $STORE_MNT " /etc/fstab || echo "$STORE_IMG $STORE_MNT xfs loop,defaults,nofail 0 0" >> /etc/fstab
systemctl daemon-reload
mountpoint -q "$STORE_MNT" || mount "$STORE_MNT"
chown "$HARNESS_USER:$HARNESS_USER" "$STORE_MNT"; chmod 755 "$STORE_MNT"
su -l "$HARNESS_USER" -c "mkdir -p $STORE_MNT/uv-cache $STORE_MNT/python $STORE_MNT/venvs $STORE_MNT/pnpm-store $STORE_MNT/pnpm-home $STORE_MNT/bun-cache"

# 4b. Node toolchains for pnpm/bun runtimes (stores live on the reflink volume, see DECISIONS.md D10).
su -l "$HARNESS_USER" -c "test -x $STORE_MNT/pnpm-home/bin/pnpm || (curl -fsSL https://get.pnpm.io/install.sh | env PNPM_HOME=$STORE_MNT/pnpm-home SHELL=/bin/bash sh -) >/dev/null"
su -l "$HARNESS_USER" -c 'test -x ~/.bun/bin/bun || (curl -fsSL https://bun.sh/install | BUN_INSTALL=$HOME/.bun bash) >/dev/null'
command -v node >/dev/null || echo "WARNING: no system node; pnpm runtimes need one (pnpm env use --global 22 as $HARNESS_USER)"

# 4c. Pinned static Caddy binary for the gateway (PLAN-allin Q3 / T2.2).
# NOT `apt install caddy`: the package brings a root systemd unit, a `caddy`
# system user and 80/443 binding, none of which Phase A wants. This is one
# static binary in the store, run as an ordinary ams service on an
# ams-allocated high port. Version and hashes are pinned; bumping means
# updating all three constants together (upstream publishes sha512 in
# caddy_<ver>_checksums.txt — CADDY_TGZ_SHA256 is derived from that verified
# tarball, CADDY_BIN_SHA256 from the file it extracts).
CADDY_VERSION=${CADDY_VERSION:-2.11.4}
CADDY_TGZ_SHA256=527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9
CADDY_BIN_SHA256=b7105518e3ed1c0761f232e44fc09345535533c9cb0abf0e12809416c7ac64d9
CADDY_BIN=$STORE_MNT/bin/caddy
install -d -o "$HARNESS_USER" -g "$HARNESS_USER" -m 0755 "$STORE_MNT/bin"
if [ -x "$CADDY_BIN" ] && [ "$(sha256sum "$CADDY_BIN" | cut -d' ' -f1)" = "$CADDY_BIN_SHA256" ]; then
  echo "caddy $CADDY_VERSION already installed at $CADDY_BIN"
else
  CADDY_TMP=$(mktemp -d)
  curl -fsSL -o "$CADDY_TMP/caddy.tar.gz" \
    "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz"
  echo "$CADDY_TGZ_SHA256  $CADDY_TMP/caddy.tar.gz" | sha256sum -c -
  tar -xzf "$CADDY_TMP/caddy.tar.gz" -C "$CADDY_TMP" caddy
  echo "$CADDY_BIN_SHA256  $CADDY_TMP/caddy" | sha256sum -c -
  install -o "$HARNESS_USER" -g "$HARNESS_USER" -m 0755 "$CADDY_TMP/caddy" "$CADDY_BIN"
  rm -rf "$CADDY_TMP"
  echo "installed caddy $CADDY_VERSION to $CADDY_BIN"
fi

# 5. State dir + systemd unit.
install -d -o "$HARNESS_USER" -g "$HARNESS_USER" -m 0755 "$STORE_MNT/state"
install -m 0644 "$REPO_DIR/deploy/ams-harness.service" /etc/systemd/system/ams-harness.service
systemctl daemon-reload
echo "host ready. Deploy code to $HARNESS_HOME/ams, then: systemctl enable --now ams-harness"
