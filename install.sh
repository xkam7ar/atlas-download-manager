#!/usr/bin/env sh
set -eu

ATLAS_REPO="${ATLAS_REPO:-https://github.com/xkam7ar/atlas.git}"
ATLAS_TAP_FORMULA="${ATLAS_TAP_FORMULA:-xkam7ar/tap/atlas}"
MODE="full"
YES="0"
NO_INSTALL="0"
OPEN_MENU="1"

usage() {
  cat <<'EOF'
Atlas installer

Usage:
  curl -fsSL https://raw.githubusercontent.com/xkam7ar/atlas/main/install.sh | bash
  bash install.sh [--full|--minimal|--media-only|--mirrors] [--yes] [--no-install] [--no-menu]

Options:
  --full        Install/check ffmpeg, aria2, wget2, and wget. Default.
  --minimal     Install/check ffmpeg and ffprobe only.
  --media-only  Install/check ffmpeg and ffprobe only.
  --mirrors     Install/check wget2 and wget only.
  --yes, -y     Do not prompt before running Homebrew/uv commands.
  --no-install  Print the plan and verify only; do not install packages.
  --no-menu     Do not launch atlas after install.
  --help        Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --full) MODE="full" ;;
    --minimal) MODE="minimal" ;;
    --media-only) MODE="media-only" ;;
    --mirrors) MODE="mirrors" ;;
    --yes|-y) YES="1" ;;
    --no-install) NO_INSTALL="1" ;;
    --no-menu) OPEN_MENU="0" ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

need_confirm() {
  [ "$YES" = "0" ] && [ -t 0 ]
}

confirm() {
  prompt="$1"
  if need_confirm; then
    printf "%s [Y/n] " "$prompt"
    read -r answer || answer=""
    case "$answer" in
      n|N|no|NO|No) return 1 ;;
      *) return 0 ;;
    esac
  fi
  return 0
}

packages_for_mode() {
  case "$MODE" in
    full) echo "ffmpeg aria2 wget2 wget" ;;
    minimal|media-only) echo "ffmpeg" ;;
    mirrors) echo "wget2 wget" ;;
    *) echo "Unknown install mode: $MODE" >&2; exit 2 ;;
  esac
}

install_atlas() {
  if "$BREW" info "$ATLAS_TAP_FORMULA" >/dev/null 2>&1; then
    if confirm "Install atlas with Homebrew formula $ATLAS_TAP_FORMULA?"; then
      "$BREW" install "$ATLAS_TAP_FORMULA" || "$BREW" upgrade "$ATLAS_TAP_FORMULA"
      return 0
    fi
  else
    echo "Homebrew formula $ATLAS_TAP_FORMULA is not available from this machine."
  fi

  if ! command -v uv >/dev/null 2>&1; then
    if confirm "Install uv with Homebrew for Atlas fallback install?"; then
      "$BREW" install uv
    fi
  fi
  if command -v uv >/dev/null 2>&1; then
    if confirm "Install atlas with uv tool from $ATLAS_REPO?"; then
      uv tool install --force "git+$ATLAS_REPO"
      return 0
    fi
    echo "Atlas install skipped."
    return 0
  fi

  echo "uv is not installed, so atlas could not be installed automatically." >&2
  echo "Install uv or use: brew install $ATLAS_TAP_FORMULA" >&2
  return 1
}

echo "Atlas Installer"
echo
echo "Mode: $MODE"
echo "Repository: $ATLAS_REPO"
echo

if command -v brew >/dev/null 2>&1; then
  BREW="$(command -v brew)"
else
  BREW=""
fi

if [ -z "$BREW" ]; then
  cat >&2 <<'EOF'
Homebrew was not found.

Atlas uses Homebrew as the preferred macOS package layer for ffmpeg, aria2,
wget2, and wget. This installer will not install Homebrew silently.

Install Homebrew first:
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

Then rerun:
  curl -fsSL https://raw.githubusercontent.com/xkam7ar/atlas/main/install.sh | bash
EOF
  if [ "$NO_INSTALL" = "0" ]; then
    exit 1
  fi
fi

PACKAGES="$(packages_for_mode)"
echo "Will install/check runtime packages:"
echo "  brew install $PACKAGES"
echo

if [ "$NO_INSTALL" = "0" ]; then
  if confirm "Continue with Homebrew runtime install?"; then
    # shellcheck disable=SC2086
    "$BREW" install $PACKAGES
  else
    echo "Runtime install skipped."
  fi
fi

if command -v atlas >/dev/null 2>&1 && atlas setup --help >/dev/null 2>&1; then
  echo "atlas already exists at: $(command -v atlas)"
elif [ "$NO_INSTALL" = "0" ]; then
  if command -v atlas >/dev/null 2>&1; then
    echo "Existing atlas does not support setup; reinstalling/updating atlas."
  fi
  install_atlas
fi

if command -v atlas >/dev/null 2>&1; then
  echo
  atlas setup "--$MODE" --no-install
  echo
  atlas doctor
  if [ "$OPEN_MENU" = "1" ] && [ -t 0 ] && [ -t 1 ]; then
    echo
    if confirm "Open Atlas now?"; then
      atlas
    fi
  fi
else
  echo
  echo "atlas is not on PATH yet. Open a new shell or add the uv tool bin directory to PATH."
fi
