#!/usr/bin/env bash
# scripts/install_cli.sh — install the `vyuha` launcher globally.
#
# Copies bin/vyuha to ~/.local/bin/vyuha, sets VYUHA_HOME in the
# user's shell rc file if not already present, and prints the next
# step (open a new shell or source the rc file).
#
# Usage:
#   bash scripts/install_cli.sh           # install
#   bash scripts/install_cli.sh --uninstall   # remove

set -e

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${HOME}/.local/bin"
LAUNCHER_SRC="${REPO}/bin/vyuha"

cyan() { printf '\033[1;36m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
red() { printf '\033[1;31m%s\033[0m\n' "$*"; }

if [[ "${1:-}" == "--uninstall" ]]; then
    if [[ -f "${TARGET}/vyuha" ]]; then
        rm -f "${TARGET}/vyuha"
        green "✓ Removed ${TARGET}/vyuha"
    else
        yellow "No launcher found at ${TARGET}/vyuha"
    fi
    yellow "  To also clean PATH entries, remove the 'vyuha' block from your ~/.bashrc"
    exit 0
fi

cyan "╔══════════════════════════════════════════════════════════════╗"
cyan "║  VYUHA CLI installer                                          ║"
cyan "╚══════════════════════════════════════════════════════════════╝"
echo

if [[ ! -f "${LAUNCHER_SRC}" ]]; then
    red "✗ Launcher not found at ${LAUNCHER_SRC}"
    exit 1
fi

mkdir -p "${TARGET}"
cp "${LAUNCHER_SRC}" "${TARGET}/vyuha"
chmod +x "${TARGET}/vyuha"
green "✓ Installed launcher to ${TARGET}/vyuha"

# Make sure ~/.local/bin is on PATH for future shells. Idempotent —
# skips if the export line is already present in any common rc file.
RC_FILES=("${HOME}/.bashrc" "${HOME}/.zshrc" "${HOME}/.profile")
UPDATED_RC=""
for RC in "${RC_FILES[@]}"; do
    if [[ -f "${RC}" ]] && grep -q 'HOME/.local/bin' "${RC}"; then
        continue
    fi
done

# Pick the first existing rc file (prefer .bashrc, then .zshrc).
TARGET_RC=""
for RC in "${HOME}/.bashrc" "${HOME}/.zshrc" "${HOME}/.profile"; do
    if [[ -f "${RC}" ]]; then
        TARGET_RC="${RC}"
        break
    fi
done

# If no rc file exists, create .bashrc as a sensible default.
if [[ -z "${TARGET_RC}" ]]; then
    TARGET_RC="${HOME}/.bashrc"
    touch "${TARGET_RC}"
fi

if ! grep -q 'VYUHA_HOME' "${TARGET_RC}"; then
    cat >> "${TARGET_RC}" << EOF

# vyuha
export PATH="\$HOME/.local/bin:\$PATH"
export VYUHA_HOME="${REPO}"
EOF
    UPDATED_RC="${TARGET_RC}"
    green "✓ Added PATH + VYUHA_HOME to ${TARGET_RC}"
else
    yellow "  VYUHA_HOME already configured in shell rc"
fi

echo
green "Installation complete."
echo
cyan "Next step:"
if [[ -n "${UPDATED_RC}" ]]; then
    yellow "  Open a new shell, or run:  source ${UPDATED_RC}"
else
    yellow "  Open a new shell so the PATH change takes effect."
fi
echo
cyan "Try it:"
yellow "  vyuha --help"
yellow "  vyuha setup         # pick provider + paste API key"
yellow "  vyuha single RELIANCE  # run for one ticker"
yellow "  vyuha portfolio     # cross-check watchlist"
yellow "  vyuha resolve       # backfill realised outcomes"
echo
cyan "Uninstall:"
yellow "  bash scripts/install_cli.sh --uninstall"