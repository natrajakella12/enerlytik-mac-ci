#!/usr/bin/env bash
# enerlytik — one-shot bootstrap for the self-hosted Mac runner host.
# Run this ONCE on the MacBook Air before registering the GitHub runner.
#
# Usage:
#   chmod +x setup_mac_runner.sh
#   ./setup_mac_runner.sh
#
# Idempotent: safe to re-run; skips anything already installed.

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}!!${NC} $*"; }
err()  { echo -e "${RED}xx${NC} $*" >&2; }

# ── 1. macOS sanity check ──────────────────────────────────────────
log "Checking macOS version + architecture"
MACOS_VER=$(sw_vers -productVersion)
ARCH=$(uname -m)
echo "    macOS:        $MACOS_VER"
echo "    Architecture: $ARCH"
if [[ "$ARCH" != "x86_64" ]]; then
    warn "Architecture is $ARCH (expected x86_64). Build will produce"
    warn "binaries for the host arch — tell Claude before triggering."
fi

# ── 2. Xcode Command Line Tools ────────────────────────────────────
log "Checking Xcode Command Line Tools"
if xcode-select -p >/dev/null 2>&1; then
    echo "    OK: $(xcode-select -p)"
else
    warn "Not installed. Triggering installer (a popup will appear)."
    warn "Click 'Install' in the popup and wait for it to finish, then"
    warn "re-run this script."
    xcode-select --install || true
    exit 1
fi

# ── 3. Homebrew ────────────────────────────────────────────────────
log "Checking Homebrew"
if ! command -v brew >/dev/null 2>&1; then
    log "Installing Homebrew (~5 min)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for this session (Apple Silicon vs Intel paths)
    if [[ -d /opt/homebrew/bin ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -d /usr/local/bin ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
else
    echo "    OK: $(brew --version | head -1)"
fi

# ── 4. Python 3.12 ─────────────────────────────────────────────────
log "Checking Python 3.12"
if command -v python3.12 >/dev/null 2>&1; then
    echo "    OK: $(python3.12 --version)"
else
    log "Installing Python 3.12 via Homebrew"
    brew install python@3.12
fi

# ── 5. Rust toolchain + x86_64 target ──────────────────────────────
log "Checking Rust toolchain"
if ! command -v rustc >/dev/null 2>&1; then
    log "Installing rustup (~3 min, prompts for default toolchain)"
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    # shellcheck disable=SC1090
    source "$HOME/.cargo/env"
else
    echo "    OK: $(rustc --version)"
fi
log "Ensuring x86_64-apple-darwin target is installed"
rustup target add x86_64-apple-darwin

# ── 6. Tauri CLI ───────────────────────────────────────────────────
log "Checking Tauri CLI"
if ! command -v cargo-tauri >/dev/null 2>&1; then
    log "Installing Tauri CLI v2 (~5-10 min from source)"
    cargo install tauri-cli --version '^2' --locked
else
    echo "    OK: $(cargo tauri --version)"
fi

# ── 7. Disk-space sanity check ─────────────────────────────────────
log "Checking free disk space"
FREE_GB=$(df -g / | awk 'NR==2 {print $4}')
echo "    Free on /: ${FREE_GB} GB"
if (( FREE_GB < 15 )); then
    warn "Less than 15 GB free. Builds need ~10 GB scratch + cache."
fi

# ── 8. Done ────────────────────────────────────────────────────────
echo
log "Bootstrap complete. Next steps:"
cat <<EOF

  1. Open the GitHub UI:
     https://github.com/natrajakella12/enerlytik-mac-ci/settings/actions/runners

  2. Click "New self-hosted runner" -> macOS -> x64

  3. Copy-paste GitHub's 4 commands into Terminal HERE.
     The token in those commands is one-time and expires in 1 hour.

  4. The last command is:
        ./run.sh
     Leave that Terminal window open; the runner is live while it runs.
     Press Ctrl+C to stop.

  5. To run the build:
     - GitHub: Actions tab -> tauri-mac-build -> Run workflow -> dev-v2.0
     - The job will pick up on this Mac within ~30 sec

  6. Add Groq keys as repo secrets (one-time):
     https://github.com/natrajakella12/enerlytik-mac-ci/settings/secrets/actions
       GROQ_KEY_1, GROQ_KEY_2, GROQ_KEY_3

  7. Optional: install runner as a launchd service (auto-starts on login):
        cd actions-runner
        sudo ./svc.sh install
        sudo ./svc.sh start

EOF
