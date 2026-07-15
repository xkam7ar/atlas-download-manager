# Installation

[Documentation home](README.md) · [Quick start](quick-start.md) ·
[Commands](commands.md) · [Troubleshooting](troubleshooting.md)

Atlas uses Homebrew for runtime tools on macOS. Until a release-complete tap
formula is published, install the `atlas` command with the guided installer or
`uv tool`; Homebrew supplies the runtime tools downloads need: `ffmpeg`,
`ffprobe`, `aria2c`, `wget2`, and `wget`.

The Python package does not install system tools during import, build,
installation, or startup. System changes happen only through `install.sh`,
Homebrew packaging, `atlas setup --install`, or `atlas doctor --fix` after the
user has seen the plan.

## Installation paths

| Method | Availability | What it changes |
| --- | --- | --- |
| Guided installer | Recommended for macOS | Shows a plan, then installs approved runtime tools and Atlas. |
| `uv tool` | Available anywhere supported by Python and uv | Installs Atlas only; system runtime tools remain explicit. |
| Local checkout | Contributor path | Installs the current working tree. |
| Homebrew tap | Release packaging target | Available only after a complete formula is published to the tap. |

## Guided installer

Use the guided bootstrap installer:

```bash
curl -fsSL https://raw.githubusercontent.com/xkam7ar/atlas/main/install.sh | bash
```

> [!IMPORTANT]
> The one-line command downloads a mutable script and executes it immediately.
> For a reviewable path, download it first, inspect it, then run the local copy:
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/xkam7ar/atlas/main/install.sh -o /tmp/atlas-install.sh
> less /tmp/atlas-install.sh
> bash /tmp/atlas-install.sh --no-install --no-menu --yes
> ```

The installer detects the host, shows the package plan, installs runtime tools
with Homebrew when available, installs Atlas, runs `atlas setup`, runs
`atlas doctor`, and offers to open the interactive menu.

It does not silently install Homebrew. If Homebrew is missing, the installer
prints the official Homebrew install command and exits.
`install.sh --no-install` is allowed without Homebrew; it prints the detected
gap and exits without installing packages.

Bootstrap options:

```bash
bash install.sh --full
bash install.sh --minimal
bash install.sh --media-only
bash install.sh --mirrors
bash install.sh --no-install --no-menu --yes
```

The installer verifies an existing `atlas` command by checking that it supports
`atlas setup`. If an older command is found and installation is allowed, the
installer updates/reinstalls Atlas instead of treating the old command as
complete. If Atlas is installed by `uv tool` but not yet on `PATH`, the installer
prints a PATH hint and does not claim the menu is ready.

## Homebrew release packaging

Once the tap contains a release-complete formula, the install path is:

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
> Python resource blocks.

## Manual and developer fallback

`uv tool` is the best Python-only fallback:

```bash
uv tool install git+https://github.com/xkam7ar/atlas.git
atlas setup
```

`uv tool install` does not install system tools such as `ffmpeg`, `aria2c`,
`wget2`, or `wget`. Preview the full runtime plan after a uv install:

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
or `--yes`.

> [!CAUTION]
> `atlas setup --full` plans and checks. `atlas setup --full --install` can run
> the displayed package-manager commands after confirmation. Adding `--yes`
> removes the interactive confirmation, so reserve it for a plan you already
> reviewed.

```bash
atlas setup
atlas setup --full
atlas setup --minimal
atlas setup --media-only
atlas setup --mirrors
atlas setup --no-install
atlas setup --install --yes
atlas setup --json
```

Modes:

| Mode | Runtime tools |
| --- | --- |
| `--full` | `ffmpeg`, `ffprobe`, `aria2c`, `wget2`, `wget` |
| `--minimal` | `ffmpeg`, `ffprobe` |
| `--media-only` | `ffmpeg`, `ffprobe` |
| `--mirrors` | `wget2`, `wget` |

`atlas setup` also creates:

- `~/Library/Application Support/atlas/config.toml`
- `~/Downloads/atlas`
- Atlas config, data, cache, and log directories

When Homebrew is detected, setup uses the detected executable path such as
`/opt/homebrew/bin/brew` or `/usr/local/bin/brew` for install commands. When no
supported package manager is detected, setup prints manual commands and keeps
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
```

`atlas update` detects Homebrew, uv-tool, source-checkout, or unknown installs
and shows the matching update command. Unknown installs are not modified
automatically.

Detected update plans:

| Install method | Update command |
| --- | --- |
| Homebrew | `brew upgrade xkam7ar/tap/atlas` |
| uv tool | `uv tool install --force git+https://github.com/xkam7ar/atlas.git` |
| source checkout | `git -C <checkout> pull --ff-only` |
| unknown | explain the situation and do not modify the system |

## Contract

Atlas setup must be explicit and reversible:

- never install system packages during Python import or package installation
- never silently install Homebrew
- show all package-manager commands before running them
- support `--no-install` and `--json`
- allow installer `--no-install` even when Homebrew is missing
- create config/output paths before reporting setup success
- run `atlas doctor` before a guided installer declares success
- keep the interactive menu as the first post-install human experience

## Related

- [Quick start](quick-start.md) for first verification and a dry run.
- [Configuration](configuration.md) for paths and persistent defaults.
- [Troubleshooting](troubleshooting.md) for PATH, Homebrew, and runtime-tool issues.
- [Development](development.md) for editable-checkout setup and packaging.
- [Documentation home](README.md) for the complete guide map.
