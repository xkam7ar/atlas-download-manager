# Atlas Migration Notes

Atlas uses the `atlas` command, the `atlas` Python package, the `ATLAS_`
environment variable prefix, and atlas-named macOS application directories.

## Current Names

| Area | Atlas value |
| --- | --- |
| CLI command | `atlas` |
| Python package | `atlas` |
| Config env prefix | `ATLAS_` |
| Config file | `~/Library/Application Support/atlas/config.toml` |
| Data/archive dir | `~/Library/Application Support/atlas` |
| Cache dir | `~/Library/Caches/atlas` |
| Log dir | `~/Library/Logs/atlas` |
| Default output dir | `~/Downloads/atlas` |

## Install Atlas

From a local checkout:

```bash
uv tool install .
```

From GitHub:

```bash
uv tool install git+https://github.com/xkam7ar/atlas.git
```

Verify the command:

```bash
atlas --help
atlas doctor
atlas config path
```

## Move Existing Config

Atlas uses its own macOS application directory. To keep an existing config, copy
it into the atlas config location:

```bash
mkdir -p "$HOME/Library/Application Support/atlas"
cp "/path/to/existing/config.toml" \
  "$HOME/Library/Application Support/atlas/config.toml"
```

Then update any saved paths inside the file so they point at atlas locations:

```toml
default_output_dir = "~/Downloads/atlas"
archive_file = "~/Library/Application Support/atlas/download-archive.txt"
```

If you want completed files to remain skipped, copy any existing download archive
into the atlas data directory:

```bash
cp "/path/to/existing/download-archive.txt" \
  "$HOME/Library/Application Support/atlas/download-archive.txt"
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
