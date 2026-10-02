#!/usr/bin/env bash
# One-time host preparation for the ams harness on Ubuntu 24.04 (run as root).
# Idempotent: re-running is safe. Mirrors what was done by hand on racknerd on
# 2026-09-02; see docs/design/DECISIONS.md D2-D4, D8 for the why.
#
# Knobs (environment):
#   AMS_STORE_FS=xfs    (default) a reflink=1 XFS loop file mounted at ~harness/store:
#                       per-service venvs / node_modules share extents with the caches.
#   AMS_STORE_FS=plain  just a directory on whatever filesystem /home is on. Everything
#                       works; provisioning copies instead of reflinking (more disk).
#                       CI and test hosts use this.
#   AMS_WITH_TOOLS=0    skip the uv/pnpm/bun/caddy downloads (the isolation tests need none).
#   AMS_TEST_DEPS=1     also put pytest into the harness venv (scripts/linux-test.sh).
#   AMS_INSTALL_UNIT=0  do not install ams-harness.service (test hosts).
set -euo pipefail

HARNESS_USER=${HARNESS_USER:-harness}
HARNESS_HOME=/home/$HARNESS_USER
STORE_IMG=${STORE_IMG:-/var/lib/ams/store.img}
STORE_SIZE=${STORE_SIZE:-6G}
STORE_MNT=$HARNESS_HOME/store
STORE_FS=${AMS_STORE_FS:-xfs}
WITH_TOOLS=${AMS_WITH_TOOLS:-1}
TEST_DEPS=${AMS_TEST_DEPS:-0}
INSTALL_UNIT=${AMS_INSTALL_UNIT:-1}
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)

case "$STORE_FS" in
  xfs | plain) ;;
  *) echo "AMS_STORE_FS must be xfs or plain, got $STORE_FS" >&2; exit 2 ;;
esac

log() { echo "install-host: $*"; }

# Run a command as the harness user in a login shell. pam_env copies
# /etc/environment into that shell, and some images (GitHub's runners) set
# XDG_* there to the *image user's* home: installers then write to a directory
# harness does not own. Drop them so every tool falls back to $HOME.
as_harness() {
  su -l "$HARNESS_USER" -c "unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_STATE_HOME; $1"
}

export DEBIAN_FRONTEND=noninteractive
# libatomic1: the standalone pnpm binary dlopens libatomic.so.1 and a minimal
# 24.04 cloud image does not ship it (found on the DO mock host, 2026-09-03).
PKGS=(uidmap python3.12-venv rsync unzip curl libatomic1 apparmor)
[ "$STORE_FS" = xfs ] && PKGS+=(xfsprogs)
apt-get update -q >/dev/null
apt-get install -y -q "${PKGS[@]}" >/dev/null
log "packages: ${PKGS[*]}"

# 1. Unprivileged harness user with an /etc/subuid + /etc/subgid range.
if ! id "$HARNESS_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$HARNESS_USER"
  log "created user $HARNESS_USER"
fi
# useradd normally assigns the range itself. If it did not (SUB_UID_COUNT=0, or a
# user created before /etc/subuid existed), take the first 65536-wide range past
# every existing entry -- never a fixed 100000, which the host's first login user
# usually owns already (ams would then ask newuidmap for ids it does not hold).
next_free_subid() {
  awk -F: 'BEGIN { end = 100000 } NF == 3 && $2 + $3 > end { end = $2 + $3 } END { print end }' "$1"
}
for f in /etc/subuid /etc/subgid; do
  touch "$f"
  if ! grep -q "^$HARNESS_USER:" "$f"; then
    echo "$HARNESS_USER:$(next_free_subid "$f"):65536" >> "$f"
  fi
  log "$f: $(grep "^$HARNESS_USER:" "$f")"
done
loginctl enable-linger "$HARNESS_USER"
# Services run as mapped uids and must traverse into $HARNESS_HOME (store,
# state). 711 = traverse only; harness's own files keep their own modes.
chmod 711 "$HARNESS_HOME"

# 2. Private interpreter copy so the AppArmor userns grant applies only to the harness.
if [ ! -x "$HARNESS_HOME/venv/bin/python3" ]; then
  as_harness "python3 -m venv --copies $HARNESS_HOME/venv"
fi
if [ "$TEST_DEPS" = 1 ]; then
  as_harness "$HARNESS_HOME/venv/bin/python3 -m pip install -q 'pytest>=8' 'pytest-timeout>=2'"
fi
if [ "$WITH_TOOLS" = 1 ]; then
  as_harness 'command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null'
fi

# 3. AppArmor: kernel.apparmor_restrict_unprivileged_userns=1 on 24.04.
install -m 0644 "$REPO_DIR/deploy/apparmor/ams-harness" /etc/apparmor.d/ams-harness
apparmor_parser -r /etc/apparmor.d/ams-harness

# 4. The store: caches, toolchains and (AMS_STATE_DIR) every service root.
# xfs: a reflink loop volume, so per-service environments cost ~0 extra disk.
# plain: a directory; provisioning falls back to full copies (DECISIONS D8/D13).
mkdir -p "$STORE_MNT"
if [ "$STORE_FS" = xfs ]; then
  mkdir -p "$(dirname "$STORE_IMG")"
  if [ ! -f "$STORE_IMG" ]; then
    fallocate -l "$STORE_SIZE" "$STORE_IMG"
    mkfs.xfs -q -m reflink=1 -L ams-store "$STORE_IMG"
  fi
  grep -q " $STORE_MNT " /etc/fstab || echo "$STORE_IMG $STORE_MNT xfs loop,defaults,nofail 0 0" >> /etc/fstab
  systemctl daemon-reload
  mountpoint -q "$STORE_MNT" || mount "$STORE_MNT"
fi
log "store: $STORE_MNT ($STORE_FS)"
chown "$HARNESS_USER:$HARNESS_USER" "$STORE_MNT"; chmod 755 "$STORE_MNT"
as_harness "mkdir -p $STORE_MNT/uv-cache $STORE_MNT/python $STORE_MNT/venvs $STORE_MNT/pnpm-store $STORE_MNT/pnpm-home $STORE_MNT/bun-cache"

if [ "$WITH_TOOLS" = 1 ]; then
  # 4b. Toolchains for unpinned pnpm/bun runtimes (DECISIONS D10). Core mode does not
  # need them: it downloads its pinned node/pnpm itself (runtime.node).
  as_harness "test -x $STORE_MNT/pnpm-home/bin/pnpm || (curl -fsSL https://get.pnpm.io/install.sh | env PNPM_HOME=$STORE_MNT/pnpm-home SHELL=/bin/bash sh -) >/dev/null"
  as_harness 'test -x ~/.bun/bin/bun || (curl -fsSL https://bun.sh/install | BUN_INSTALL=$HOME/.bun bash) >/dev/null'
  command -v node >/dev/null || log "WARNING: no system node; unpinned pnpm runtimes need one (or pin runtime.node)"

  # 4c. Pinned static Caddy binary for the gateway.
  # NOT `apt install caddy`: the package brings a root systemd unit, a `caddy`
  # system user and 80/443 binding. This is one static binary in the store, run
  # as an ordinary ams service on a high port. Version and hashes are pinned;
  # bumping means updating all three constants together (upstream publishes
  # sha512 in caddy_<ver>_checksums.txt -- CADDY_TGZ_SHA256 is derived from that
  # verified tarball, CADDY_BIN_SHA256 from the file it extracts).
  CADDY_VERSION=${CADDY_VERSION:-2.11.4}
  CADDY_TGZ_SHA256=527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9
  CADDY_BIN_SHA256=b7105518e3ed1c0761f232e44fc09345535533c9cb0abf0e12809416c7ac64d9
  CADDY_BIN=$STORE_MNT/bin/caddy
  install -d -o "$HARNESS_USER" -g "$HARNESS_USER" -m 0755 "$STORE_MNT/bin"
  if [ -x "$CADDY_BIN" ] && [ "$(sha256sum "$CADDY_BIN" | cut -d' ' -f1)" = "$CADDY_BIN_SHA256" ]; then
    log "caddy $CADDY_VERSION already installed at $CADDY_BIN"
  else
    CADDY_TMP=$(mktemp -d)
    curl -fsSL -o "$CADDY_TMP/caddy.tar.gz" \
      "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz"
    echo "$CADDY_TGZ_SHA256  $CADDY_TMP/caddy.tar.gz" | sha256sum -c -
    tar -xzf "$CADDY_TMP/caddy.tar.gz" -C "$CADDY_TMP" caddy
    echo "$CADDY_BIN_SHA256  $CADDY_TMP/caddy" | sha256sum -c -
    install -o "$HARNESS_USER" -g "$HARNESS_USER" -m 0755 "$CADDY_TMP/caddy" "$CADDY_BIN"
    rm -rf "$CADDY_TMP"
    log "installed caddy $CADDY_VERSION to $CADDY_BIN"
  fi
else
  log "AMS_WITH_TOOLS=0: skipping uv/pnpm/bun/caddy"
fi

# 5. State dir + systemd unit.
install -d -o "$HARNESS_USER" -g "$HARNESS_USER" -m 0755 "$STORE_MNT/state"
if [ "$INSTALL_UNIT" = 1 ]; then
  install -m 0644 "$REPO_DIR/deploy/ams-harness.service" /etc/systemd/system/ams-harness.service
  systemctl daemon-reload
  log "host ready. Deploy code to $HARNESS_HOME/ams, then: systemctl enable --now ams-harness"
else
  log "host ready (no unit installed)."
fi
