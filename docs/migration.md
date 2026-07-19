# Atlas Migration Notes

Atlas uses the `atlas` command, the `atlas` Python package, the `ATLAS_`
environment variable prefix, and atlas-named host-native application
directories.

## Current Names

| Area | Atlas value |
| --- | --- |
| Product | Atlas Download Manager |
| Distribution/repository/formula | `atlas-download-manager` |
| CLI command | `atlas` |
| Python package | `atlas` |
| Config env prefix | `ATLAS_` |
| Config file | macOS: `~/Library/Application Support/atlas/config.toml`; Linux: `~/.config/atlas/config.toml` |
| Data/archive dir | macOS: `~/Library/Application Support/atlas`; Linux: `~/.local/share/atlas` |
| Cache dir | macOS: `~/Library/Caches/atlas`; Linux: `~/.cache/atlas` |
| Log dir | macOS: `~/Library/Logs/atlas`; Linux: `~/.local/state/atlas/log` |
| Default output dir | `~/Downloads/atlas` |

## Install This Pre-release Checkout

This repository is distributed as a source preview, not as a supported package
release. The unqualified PyPI and Homebrew `atlas` names belong to unrelated
projects; this project uses the collision-safe distribution name
`atlas-download-manager` while keeping the `atlas` command. No package release
exists yet, so install the current checkout with uv:

```bash
uv tool install . --force
```

The local guided installer can be reviewed without mutation:

```bash
bash install.sh --no-install --no-menu --yes
```

A future supported release must publish a tag and its full commit ID. Verify the
release metadata, then install using the commit ID:

```bash
release_commit=0123456789abcdef0123456789abcdef01234567
uv tool install "git+https://github.com/xkam7ar/atlas-download-manager.git@${release_commit}"
```

Do not execute an installer from `main`; verify the installer and checksum
manifest from the same immutable release before passing that ref to
`install.sh --release-ref`.

Then review and install the full system runtime:

```bash
atlas setup --full --install
```

Setup detects Homebrew, apt, dnf, or pacman, shows the complete package plan,
and asks before mutation. A bare uv install includes Python dependencies such
as yt-dlp and mutagen, but not ffmpeg/ffprobe or optional transfer backends.

Verify the command:

```bash
atlas --help
atlas doctor
atlas config path
```

## Move Existing Config

Atlas uses its own application directory. Run `atlas config path` first, then
copy an existing config to the path it prints. The default commands are:

```bash
# macOS
mkdir -p "$HOME/Library/Application Support/atlas"
cp "/path/to/existing/config.toml" \
  "$HOME/Library/Application Support/atlas/config.toml"

# Linux with default XDG locations
mkdir -p "$HOME/.config/atlas"
cp "/path/to/existing/config.toml" "$HOME/.config/atlas/config.toml"
```

Then update any saved paths inside the file so they point at atlas locations:

```toml
default_output_dir = "~/Downloads/atlas"
# macOS
archive_file = "~/Library/Application Support/atlas/download-archive.txt"
# Linux with default XDG locations
# archive_file = "~/.local/share/atlas/download-archive.txt"
```

If you want completed files to remain skipped, copy any existing download archive
into the atlas data directory:

```bash
# macOS
cp "/path/to/existing/download-archive.txt" \
  "$HOME/Library/Application Support/atlas/download-archive.txt"

# Linux with default XDG locations
mkdir -p "$HOME/.local/share/atlas"
cp "/path/to/existing/download-archive.txt" \
  "$HOME/.local/share/atlas/download-archive.txt"
```

## Update Shell Scripts

Use the atlas command:

```bash
atlas video URL
atlas audio URL
atlas get URL
```

Use the atlas environment variable prefix:

```bash
ATLAS_OUTPUT_DIR="$HOME/Downloads/Media"
ATLAS_ARCHIVE=false
```

Raw backend pass-through commands keep the same shape under atlas:

```bash
atlas ytdlp -- --help
atlas aria2 -- --version
atlas wget2 -- --recursive --level=2 https://example.com/docs/
atlas wget -- --mirror https://example.com/docs/
```

Direct-file and site backend preferences now live in config as explicit Atlas
settings:

```toml
file_backend = "auto" # auto, native, aria2, or wget2
site_backend = "auto" # auto, wget2, or wget
dir_backend = "auto"  # auto, wget2, or wget
```

If older scripts mirrored open archive directories with raw `wget2`, prefer the
explicit Atlas directory command:

```bash
atlas dir URL --depth 2 --accept pdf --adaptive --explain
```

If older scripts generated an exact URL list, prefer batch file mode:

```bash
atlas batch urls.txt --kind file --adaptive --per-host-concurrency 2
```

Use `--adaptive --explain --json` when migrating older exact-list scripts. The
manifest now exposes each item's bucket, priority, selected backend, recursion
depth when relevant, and scheduler decision before the batch starts.

Optimized plans also include a `SmartDownloadSession`, and non-dry-run batch
manifests include a `smart_session` block. Use that stable session shape for new
automation instead of scraping backend output.

## Clean Up

After confirming `atlas config show`, `atlas doctor`, and a dry run work, archive
or remove obsolete local tool data manually.

Keep any old download folders that still contain media you want.
