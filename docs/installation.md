# Installation

[Documentation home](README.md) · [Quick start](quick-start.md) ·
[Commands](commands.md) · [Troubleshooting](troubleshooting.md)

Atlas automatically provisions runtime tools on macOS, Debian/Ubuntu, Fedora,
and Arch-family Linux through Homebrew, apt, dnf, or pacman. The full runtime is
`ffmpeg`, `ffprobe`, `aria2c`, `wget2`, and `wget`; `yt-dlp` and `mutagen` are
installed as Atlas Python dependencies so media extraction and artwork embedding
work immediately.

The Python package does not install system tools during import, build,
installation, or startup. System-package changes happen only through
`install.sh`, Homebrew packaging, `atlas setup --install`, or
`atlas doctor --fix` after the user has seen the plan. Plain `atlas setup`
initializes Atlas-owned paths and configuration without installing packages;
plain `atlas doctor` creates/checks those paths and uses temporary write probes.

## Installation paths

| Method | Availability | What it changes |
| --- | --- | --- |
| Local guided installer | Source-preview plan | `--no-install` shows the complete plan; uv installation is blocked without `--release-ref`. |
| `uv tool` | Tested on macOS and Linux | Installs Atlas only; system runtime tools remain explicit. |
| Local checkout | Contributor path | Installs the current working tree. |
| Remote installer / GitHub uv tool | Release-only target | Requires the verified release's full 40-character commit ID; the default branch and tag names are never install channels. |
| Homebrew tap | Pre-release packaging target | Unavailable until a complete, collision-safe formula is published to the tap. |

Windows is not in the current CI or guided-installer support matrix. A Python
installation may work, but it is best-effort and must not be advertised as
supported until native Windows CLI, path, cancellation, and downloader smoke
tests are added.

## Guided installer

From this checkout, inspect and preview the guided bootstrap installer:

```bash
less install.sh
bash install.sh --no-install --no-menu --yes
```

> [!IMPORTANT]
> This repository is a source preview rather than a supported package release.
> Repository visibility alone does not make the raw installer, a release tag,
> or the tap supported. Do not use
> `pip install atlas` or `brew install atlas`: those names resolve to unrelated
> projects. A collision-free distribution identity and public release must be
> established before remote install instructions can become active.

The installer detects the host and missing executables, shows every bootstrap,
package-manager, Atlas, and verification command, then asks once. `--yes`
accepts that displayed plan without an Atlas prompt; `sudo` may still request an
OS password. Homebrew is installed only when included in the approved plan:
when missing on macOS, or on pacman hosts that need the `wget2` fallback.

Linux mappings:

| Manager | Full runtime packages |
| --- | --- |
| apt | `ffmpeg aria2 wget2 wget` |
| dnf | `ffmpeg-free aria2 wget2 wget1-wget` |
| pacman | `ffmpeg aria2 wget`; Linuxbrew supplies `wget2` |

`install.sh --no-install` prints the same plan and exits without installing
packages, installing Atlas, or creating Atlas paths. Without an explicit
`--release-ref`, the plan marks remote uv installation blocked; if uv would be
needed to install Atlas, a mutating run exits before changing the host.

The mutation order is fixed: detect and plan, render the complete plan, obtain
one Atlas-level approval, run each listed bootstrap/package/Atlas command in
order, run `atlas setup --MODE` to initialize Atlas paths and configuration,
repeat that mode with `--no-install` as a final plan-only verification, then run
`atlas doctor`. Any failed command stops the installer, and required Doctor
failures prevent a success message. A second run is idempotent because installed
executables are removed from the package plan.

Bootstrap options:

```bash
bash install.sh --full
bash install.sh --minimal
bash install.sh --media-only
bash install.sh --mirrors
bash install.sh --no-install --no-menu --yes
# Release only, after verifying a downloaded release installer and checksums:
bash install.sh --release-ref 0123456789abcdef0123456789abcdef01234567
```

The installer verifies an existing `atlas` command by checking that it supports
`atlas setup`. If the tap formula is unavailable, it installs `uv` with the
official standalone installer and uses `uv tool`; uv downloads compatible Python
and Python dependencies. Installation fails unless every selected executable is
present and `atlas doctor` succeeds.

## Homebrew release packaging

Once the tap contains a release-complete and collision-safe formula, the
intended install path is:

```bash
brew install xkam7ar/tap/atlas
```

The formula is expected to depend on the full runtime:

```bash
brew install ffmpeg aria2 wget2 wget
```

> [!NOTE]
> The checked-in formula is not itself a published release. The template lives at
> [`packaging/homebrew/atlas.rb`](../packaging/homebrew/atlas.rb). Copy it into
> the tap, replace the release SHA, and run `brew update-python-resources atlas`
> before publishing so Python dependencies are declared as Homebrew resources.
> It is release-ready only after the tap has a tagged tarball SHA and generated
> Python resource blocks. Homebrew core already has an unrelated `atlas`
> executable, so the tap formula also needs an explicit conflict/naming plan.

## Manual and developer fallback

`uv tool` is the best Python-only path from this checkout:

```bash
uv tool install . --force
atlas setup
```

A supported remote release publishes a version tag and its full commit ID. Verify
both against the release metadata, then install by commit ID rather than a mutable
ref name:

```bash
release_commit=0123456789abcdef0123456789abcdef01234567
uv tool install "git+https://github.com/xkam7ar/atlas.git@${release_commit}"
```

Do not execute `install.sh` from `main`. Download it and the published checksum
manifest from the same immutable release, verify the checksum, inspect the
script, and pass the same ref through `--release-ref`.

`uv tool install` does not install system tools such as `ffmpeg`, `aria2c`,
`wget2`, or `wget`. Initialize Atlas paths/configuration and preview the full
runtime plan after a uv install:

```bash
atlas setup --full
```

Install that reviewed plan only with an explicit install action:

```bash
atlas setup --full --install
atlas doctor
atlas
```

## Setup modes

`atlas setup` checks paths and runtime tools. It prints an install plan by
default and only runs package-manager commands with `--install` and confirmation
or `--yes`. `--no-install` and `--json` are non-mutating plan modes.

Setup JSON contains the selected `mode`, detected `environment`, per-tool rows,
missing tools, executable `install_commands`, exact `manual_commands`, config
and output paths, `can_install`, `complete`, and notes. On supported hosts,
`environment.package_manager` is `homebrew`, `apt`, `dnf`, or `pacman`, and each
tool's `package` value is the host-specific mapping from the table above.

> [!CAUTION]
> `atlas setup --full` initializes Atlas paths/configuration, then plans and
> checks runtime tools without running the package manager.
> `atlas setup --full --install` can run the displayed package-manager commands
> after confirmation. Adding `--yes` removes the interactive confirmation, so
> reserve it for a plan you already reviewed.

```bash
atlas setup
atlas setup --full
atlas setup --minimal
atlas setup --media-only
atlas setup --mirrors
atlas setup --no-install
atlas setup --install --yes
atlas setup --json
atlas setup --open-menu
```

Modes:

| Mode | Runtime tools |
| --- | --- |
| `--full` | `ffmpeg`, `ffprobe`, `aria2c`, `wget2`, `wget` |
| `--minimal` | `ffmpeg`, `ffprobe` |
| `--media-only` | `ffmpeg`, `ffprobe` |
| `--mirrors` | `wget2`, `wget` |

Unless `--no-install` or `--json` is used, `atlas setup` creates the config path
reported by `atlas config path`, the configured output directory (default
`~/Downloads/atlas`), and the host-native config, data, cache, and log
directories. `--open-menu` launches the interactive menu after setup completes.

Setup uses the detected manager executable path and omits packages whose tools
already exist. apt plans refresh metadata before installing; dnf and pacman use
noninteractive install flags. Native Linux commands omit `sudo` when already
root. Without root or `sudo`, setup prints manual commands and keeps
`can_install` false.

## Repair and update

Use doctor for verification:

```bash
atlas doctor
atlas doctor --fix
atlas doctor --fix --yes
atlas doctor --json
```

Use update for install-method-aware upgrade instructions:

```bash
atlas update
atlas update --dry-run
atlas update --yes
atlas update --json
atlas update --release-ref 0123456789abcdef0123456789abcdef01234567
```

`atlas update` detects Homebrew, uv-tool, source-checkout, or unknown installs
and shows the matching update command. Unknown installs are not modified.
Homebrew consumes a versioned formula archive and checksum. uv-tool updates are
blocked until the operator supplies the verified release's full commit ID.
Source-checkout pulls remain an explicit local development workflow.

Detected update plans:

| Install method | Update command |
| --- | --- |
| Homebrew | `brew upgrade xkam7ar/tap/atlas` |
| uv tool without a release ref | blocked; explain `--release-ref` and do not modify the system |
| uv tool with a full `--release-ref COMMIT_ID` | `uv tool install --force git+https://github.com/xkam7ar/atlas.git@COMMIT_ID` |
| source checkout | `git -C <checkout> pull --ff-only` |
| unknown | explain the situation and do not modify the system |

## Contract

Atlas setup must be explicit and reversible:

- never install system packages during Python import or package installation
- install Homebrew only after showing it in the approved plan
- show all package-manager commands before running them
- keep `--no-install` and `--json` non-mutating
- allow installer `--no-install` even when Homebrew is missing
- never treat `main`, `master`, `HEAD`, or another mutable branch as a remote Atlas install/update source
- require the same verified full commit ID in guided uv installation and `atlas update`
- create config/output paths before reporting setup success
- run `atlas doctor` before a guided installer declares success
- keep the interactive menu as the first post-install human experience

The current implementation has no Atlas uninstall or automatic rollback
command. “Reversible” here means that mutations are previewed and limited to
ordinary package-manager/tool installs and Atlas-owned paths; release readiness
still requires documented uninstall and failed-install cleanup procedures.

## Related

- [Quick start](quick-start.md) for first verification and a dry run.
- [Configuration](configuration.md) for paths and persistent defaults.
- [Troubleshooting](troubleshooting.md) for PATH, Homebrew, and runtime-tool issues.
- [Development](development.md) for editable-checkout setup and packaging.
- [Documentation home](README.md) for the complete guide map.
