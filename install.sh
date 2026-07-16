#!/usr/bin/env sh
set -eu

ATLAS_REPO="${ATLAS_REPO:-https://github.com/xkam7ar/atlas.git}"
ATLAS_TAP_FORMULA="${ATLAS_TAP_FORMULA:-xkam7ar/tap/atlas}"
HOMEBREW_INSTALL_URL="https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"
MODE="full"
YES="0"
NO_INSTALL="0"
OPEN_MENU="1"
NATIVE_PACKAGES=""
BREW_PACKAGES=""
BREW=""
UV=""
ATLAS_BIN=""
ATLAS_INSTALL_METHOD=""
SUDO=""

usage() {
  cat <<'EOF'
Atlas installer

Usage:
  curl -fsSL https://raw.githubusercontent.com/xkam7ar/atlas/main/install.sh | bash
  bash install.sh [--full|--minimal|--media-only|--mirrors] [--yes] [--no-install] [--no-menu]

Options:
  --full        Install/check ffmpeg, ffprobe, aria2, wget2, and wget. Default.
  --minimal     Install/check ffmpeg and ffprobe only.
  --media-only  Install/check ffmpeg and ffprobe only.
  --mirrors     Install/check wget2 and wget only.
  --yes, -y     Approve the displayed plan without an Atlas confirmation prompt.
  --no-install  Print the plan only; do not install packages or create Atlas paths.
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

confirm_plan() {
  [ "$YES" = "1" ] && return 0
  if [ -t 1 ] && [ -r /dev/tty ] && [ -w /dev/tty ]; then
    printf "%s [Y/n] " "$1" >/dev/tty
    read -r answer </dev/tty || answer=""
  elif [ -t 0 ]; then
    printf "%s [Y/n] " "$1"
    read -r answer || answer=""
  else
    echo "Non-interactive installation requires --yes." >&2
    return 1
  fi
  case "$answer" in
    n|N|no|NO|No) return 1 ;;
    *) return 0 ;;
  esac
}

append_native() {
  case " $NATIVE_PACKAGES " in
    *" $1 "*) ;;
    *) NATIVE_PACKAGES="${NATIVE_PACKAGES:+$NATIVE_PACKAGES }$1" ;;
  esac
}

append_brew() {
  case " $BREW_PACKAGES " in
    *" $1 "*) ;;
    *) BREW_PACKAGES="${BREW_PACKAGES:+$BREW_PACKAGES }$1" ;;
  esac
}

queue_runtime_package() {
  tool="$1"
  case "$PACKAGE_MANAGER:$tool" in
    homebrew:*) append_brew "$tool" ;;
    apt:ffmpeg|apt:aria2|apt:wget2|apt:wget) append_native "$tool" ;;
    dnf:ffmpeg) append_native "ffmpeg-free" ;;
    dnf:aria2|dnf:wget2) append_native "$tool" ;;
    dnf:wget) append_native "wget1-wget" ;;
    pacman:wget2) append_brew "wget2" ;;
    pacman:ffmpeg|pacman:aria2|pacman:wget) append_native "$tool" ;;
    none:*) append_brew "$tool" ;;
  esac
}

detect_manager() {
  OS_NAME="${ATLAS_OS:-$(uname -s)}"
  PACKAGE_MANAGER="none"
  PACKAGE_MANAGER_PATH=""
  if command -v brew >/dev/null 2>&1; then
    BREW="$(command -v brew)"
  fi
  case "$OS_NAME" in
    Darwin)
      if [ -n "$BREW" ]; then
        PACKAGE_MANAGER="homebrew"
        PACKAGE_MANAGER_PATH="$BREW"
      fi
      ;;
    Linux)
      if command -v apt-get >/dev/null 2>&1; then
        PACKAGE_MANAGER="apt"
        PACKAGE_MANAGER_PATH="$(command -v apt-get)"
      elif command -v dnf >/dev/null 2>&1; then
        PACKAGE_MANAGER="dnf"
        PACKAGE_MANAGER_PATH="$(command -v dnf)"
      elif command -v pacman >/dev/null 2>&1; then
        PACKAGE_MANAGER="pacman"
        PACKAGE_MANAGER_PATH="$(command -v pacman)"
      elif [ -n "$BREW" ]; then
        PACKAGE_MANAGER="homebrew"
        PACKAGE_MANAGER_PATH="$BREW"
      fi
      ;;
  esac
}

detect_elevation() {
  if [ "$(id -u)" = "0" ]; then
    SUDO=""
  elif command -v sudo >/dev/null 2>&1; then
    SUDO="$(command -v sudo)"
  else
    SUDO="missing"
  fi
}

detect_missing_runtime() {
  NEED_MEDIA="0"
  NEED_ARIA2="0"
  NEED_WGET2="0"
  NEED_WGET="0"
  case "$MODE" in
    full) NEED_MEDIA="1"; NEED_ARIA2="1"; NEED_WGET2="1"; NEED_WGET="1" ;;
    minimal|media-only) NEED_MEDIA="1" ;;
    mirrors) NEED_WGET2="1"; NEED_WGET="1" ;;
    *) echo "Unknown install mode: $MODE" >&2; exit 2 ;;
  esac
  if [ "$NEED_MEDIA" = "1" ] && {
    ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1
  }; then
    queue_runtime_package "ffmpeg"
  fi
  if [ "$NEED_ARIA2" = "1" ] && ! command -v aria2c >/dev/null 2>&1; then
    queue_runtime_package "aria2"
  fi
  if [ "$NEED_WGET2" = "1" ] && ! command -v wget2 >/dev/null 2>&1; then
    queue_runtime_package "wget2"
  fi
  if [ "$NEED_WGET" = "1" ] && ! command -v wget >/dev/null 2>&1; then
    queue_runtime_package "wget"
  fi
  if [ "$PACKAGE_MANAGER" = "pacman" ] && [ -n "$BREW_PACKAGES" ] && [ -z "$BREW" ]; then
    append_native "base-devel"
    append_native "procps-ng"
    append_native "curl"
    append_native "file"
    append_native "git"
  fi
}

native_command() {
  prefix=""
  [ -n "$SUDO" ] && [ "$SUDO" != "missing" ] && prefix="$SUDO "
  case "$PACKAGE_MANAGER" in
    apt) echo "${prefix}${PACKAGE_MANAGER_PATH} update"; echo "${prefix}${PACKAGE_MANAGER_PATH} install -y $NATIVE_PACKAGES" ;;
    dnf) echo "${prefix}${PACKAGE_MANAGER_PATH} install -y $NATIVE_PACKAGES" ;;
    pacman) echo "${prefix}${PACKAGE_MANAGER_PATH} -S --needed --noconfirm $NATIVE_PACKAGES" ;;
  esac
}

planned_brew_path() {
  if [ -n "$BREW" ]; then
    printf "%s" "$BREW"
  elif [ "$OS_NAME" = "Linux" ]; then
    printf "/home/linuxbrew/.linuxbrew/bin/brew"
  elif [ "$(uname -m)" = "arm64" ]; then
    printf "/opt/homebrew/bin/brew"
  else
    printf "/usr/local/bin/brew"
  fi
}

show_plan() {
  echo "Atlas Installer"
  echo
  echo "Mode: $MODE"
  echo "OS: $OS_NAME"
  echo "Package manager: $PACKAGE_MANAGER"
  echo "Repository: $ATLAS_REPO"
  echo
  echo "Plan:"
  if [ -n "$NATIVE_PACKAGES" ]; then
    if [ "$SUDO" = "missing" ]; then
      echo "  blocked: root access or sudo required for $NATIVE_PACKAGES"
    else
      native_command | while IFS= read -r command; do echo "  $command"; done
    fi
  fi
  if [ -n "$BREW_PACKAGES" ]; then
    if [ -z "$BREW" ]; then
      echo "  NONINTERACTIVE=1 /bin/bash -c \"\$(curl -fsSL $HOMEBREW_INSTALL_URL)\""
    fi
    echo "  $(planned_brew_path) install $BREW_PACKAGES"
  fi
  case "$ATLAS_INSTALL_METHOD" in
    existing)
      echo "  Atlas already installed: $ATLAS_BIN"
      ;;
    formula)
      echo "  $BREW install $ATLAS_TAP_FORMULA || $BREW upgrade $ATLAS_TAP_FORMULA"
      ;;
    formula-after-bootstrap)
      echo "  if HOMEBREW_NO_AUTO_UPDATE=1 $(planned_brew_path) info $ATLAS_TAP_FORMULA succeeds:"
      echo "    $(planned_brew_path) install $ATLAS_TAP_FORMULA || $(planned_brew_path) upgrade $ATLAS_TAP_FORMULA"
      echo "  otherwise:"
      show_uv_plan "    "
      ;;
    uv)
      show_uv_plan "  "
      ;;
  esac
  if [ -z "$NATIVE_PACKAGES" ] && [ -z "$BREW_PACKAGES" ]; then
    echo "  All selected runtime tools already installed"
  fi
  echo "  atlas setup --$MODE"
  echo "  atlas setup --$MODE --no-install"
  echo "  atlas doctor"
  echo
}

show_uv_plan() {
  indent="$1"
  if ! command -v uv >/dev/null 2>&1; then
    echo "${indent}curl -LsSf $UV_INSTALL_URL | sh"
  fi
  echo "${indent}uv tool install --force git+$ATLAS_REPO"
}

install_homebrew() {
  [ -n "$BREW" ] && return 0
  command -v curl >/dev/null 2>&1 || {
    echo "curl is required to install Homebrew." >&2
    return 1
  }
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL "$HOMEBREW_INSTALL_URL")"
  candidate="$(planned_brew_path)"
  if [ -x "$candidate" ]; then
    BREW="$candidate"
  elif command -v brew >/dev/null 2>&1; then
    BREW="$(command -v brew)"
  else
    echo "Homebrew installation completed, but brew was not found." >&2
    return 1
  fi
  PATH="$(dirname "$BREW"):$PATH"
  export PATH
}

install_runtime() {
  if [ -n "$NATIVE_PACKAGES" ]; then
    if [ "$SUDO" = "missing" ]; then
      echo "Root access or sudo is required to install: $NATIVE_PACKAGES" >&2
      return 1
    fi
    case "$PACKAGE_MANAGER" in
      apt)
        ${SUDO:+"$SUDO"} "$PACKAGE_MANAGER_PATH" update
        # shellcheck disable=SC2086
        ${SUDO:+"$SUDO"} "$PACKAGE_MANAGER_PATH" install -y $NATIVE_PACKAGES
        ;;
      dnf)
        # shellcheck disable=SC2086
        ${SUDO:+"$SUDO"} "$PACKAGE_MANAGER_PATH" install -y $NATIVE_PACKAGES
        ;;
      pacman)
        # shellcheck disable=SC2086
        ${SUDO:+"$SUDO"} "$PACKAGE_MANAGER_PATH" -S --needed --noconfirm $NATIVE_PACKAGES
        ;;
      *)
        echo "No supported native package manager found for: $NATIVE_PACKAGES" >&2
        return 1
        ;;
    esac
  fi
  if [ -n "$BREW_PACKAGES" ]; then
    install_homebrew
    # shellcheck disable=SC2086
    "$BREW" install $BREW_PACKAGES
  fi
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
    return 0
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf "$UV_INSTALL_URL" | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$UV_INSTALL_URL" | sh
  else
    echo "curl or wget is required to install uv." >&2
    return 1
  fi
  if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
  elif [ -x "${HOME:?}/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
  else
    echo "uv installation completed, but uv was not found." >&2
    return 1
  fi
}

resolve_atlas() {
  if command -v atlas >/dev/null 2>&1; then
    ATLAS_BIN="$(command -v atlas)"
  elif [ -x "${HOME:?}/.local/bin/atlas" ]; then
    ATLAS_BIN="$HOME/.local/bin/atlas"
  elif [ -n "$BREW" ] && [ -x "$("$BREW" --prefix)/bin/atlas" ]; then
    ATLAS_BIN="$("$BREW" --prefix)/bin/atlas"
  else
    ATLAS_BIN=""
  fi
}

plan_atlas_install() {
  resolve_atlas
  if [ -n "$ATLAS_BIN" ] && { [ "$NO_INSTALL" = "1" ] || \
       "$ATLAS_BIN" setup --help >/dev/null 2>&1; }; then
    ATLAS_INSTALL_METHOD="existing"
  elif [ "$MODE" = "full" ] && [ -n "$BREW" ] && { \
       [ "$NO_INSTALL" = "1" ] || \
       HOMEBREW_NO_AUTO_UPDATE=1 "$BREW" info "$ATLAS_TAP_FORMULA" \
       >/dev/null 2>&1; }; then
    ATLAS_INSTALL_METHOD="formula"
  elif [ "$MODE" = "full" ] && [ -z "$BREW" ] && [ -n "$BREW_PACKAGES" ]; then
    ATLAS_INSTALL_METHOD="formula-after-bootstrap"
  else
    ATLAS_INSTALL_METHOD="uv"
  fi
}

install_atlas() {
  if [ "$ATLAS_INSTALL_METHOD" = "existing" ]; then
    echo "Atlas already installed: $ATLAS_BIN"
    return 0
  fi
  if [ "$ATLAS_INSTALL_METHOD" = "formula" ] || {
    [ "$ATLAS_INSTALL_METHOD" = "formula-after-bootstrap" ] &&
    "$BREW" info "$ATLAS_TAP_FORMULA" >/dev/null 2>&1
  }; then
    "$BREW" install "$ATLAS_TAP_FORMULA" || "$BREW" upgrade "$ATLAS_TAP_FORMULA"
  else
    install_uv
    "$UV" tool install --force "git+$ATLAS_REPO"
  fi
  resolve_atlas
  if [ -z "$ATLAS_BIN" ]; then
    echo "Atlas installation completed, but atlas was not found on PATH or in ~/.local/bin." >&2
    return 1
  fi
}

verify_runtime() {
  failed="0"
  check_tool() {
    command -v "$1" >/dev/null 2>&1 || {
      echo "Missing after installation: $1" >&2
      failed="1"
    }
  }
  case "$MODE" in
    full) check_tool ffmpeg; check_tool ffprobe; check_tool aria2c; check_tool wget2; check_tool wget ;;
    minimal|media-only) check_tool ffmpeg; check_tool ffprobe ;;
    mirrors) check_tool wget2; check_tool wget ;;
  esac
  [ "$failed" = "0" ]
}

detect_manager
detect_elevation
detect_missing_runtime
plan_atlas_install
show_plan

if [ "$NO_INSTALL" = "1" ]; then
  echo "Plan only; no changes made."
  exit 0
fi

if [ "$PACKAGE_MANAGER" = "none" ] && [ "$OS_NAME" != "Darwin" ] && \
   [ -n "$BREW_PACKAGES" ]; then
  echo "No supported package manager was detected for missing runtime tools." >&2
  exit 1
fi

if ! confirm_plan "Install Atlas and all listed prerequisites?"; then
  echo "Installation cancelled."
  exit 0
fi

install_runtime
install_atlas
verify_runtime

echo
"$ATLAS_BIN" setup "--$MODE"
echo
"$ATLAS_BIN" setup "--$MODE" --no-install
echo
"$ATLAS_BIN" doctor

if [ "$OPEN_MENU" = "1" ] && [ -t 0 ] && [ -t 1 ]; then
  echo
  "$ATLAS_BIN"
fi
