# Atlas Download Manager — plan-first terminal downloads

[![Quality](https://github.com/xkam7ar/atlas-download-manager/actions/workflows/quality.yml/badge.svg?branch=main)](https://github.com/xkam7ar/atlas-download-manager/actions/workflows/quality.yml)
[![CodeQL](https://github.com/xkam7ar/atlas-download-manager/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/xkam7ar/atlas-download-manager/actions/workflows/codeql.yml)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MIT license](https://img.shields.io/badge/License-MIT-2ea44f.svg)](LICENSE)

**Paste one URL. Review one plan. Let Atlas choose the right engine.**

Atlas is an open-source Python CLI and interactive terminal download manager for
video, audio, playlists, direct files, website mirrors, open directories, and
repeatable batches. It detects intent, builds a typed plan, and shows what will
happen before handing work to `yt-dlp`, native Python, `aria2c`, `wget2`, or
`wget`.

```text
URL  ->  detect  ->  review  ->  download  ->  summary
```

> [!IMPORTANT]
> Use Atlas only for content you are allowed to access. Atlas does not bypass
> DRM, paywalls, access controls, platform restrictions, bot challenges, or
> other protections. Cookies and request controls are for legitimate,
> user-authorized access.

## Why Atlas

Use a downloader directly when one command already does the job. Atlas is for
workflows that benefit from a consistent layer across several engines:

- **Plan before execution.** Dry runs expose the selected backend, output, and
  recursive scope before a transfer begins.
- **Keep one interface.** The menu and CLI cover media, files, mirrors, open
  directories, and mixed URL batches without hiding the underlying engine.
- **Recover instead of restart.** Batch and mirror sessions retain private
  manifests, failure states, and narrow retry paths.
- **Use it interactively or automate it.** Human-friendly terminal views coexist
  with stable JSON results and NDJSON progress events.

Read [Why Atlas](docs/why-atlas.md) for a practical comparison with using
`yt-dlp`, `aria2c`, `wget2`, or `wget` directly.

## Verification evidence

- The [quality workflow](https://github.com/xkam7ar/atlas-download-manager/actions/workflows/quality.yml)
  tests Python 3.12–3.14, runs Ubuntu and macOS smoke coverage, audits locked
  dependencies, builds both distributions, and smoke-tests the installed wheel.
- A bounded [50-target open-directory field audit](docs/open-directory-audit-2026-07-15.md)
  finished with 50 expected outcomes after fixes under same-origin, no-parent,
  non-recursive, no-download constraints. The audit is dated and does not claim
  universal compatibility.
- The first-run example below uses a small public file, supports a no-transfer
  dry run, and verifies the real result in the [Quick start](docs/quick-start.md).

## Start here

| I want to… | Start with |
| --- | --- |
| Decide whether Atlas fits | [Why Atlas](docs/why-atlas.md) |
| Try Atlas for the first time | [Quick start](docs/quick-start.md) |
| Install or repair Atlas | [Installation](docs/installation.md) |
| Find a command | [Command reference](docs/commands.md) |
| Fix a problem | [Troubleshooting](docs/troubleshooting.md) |
| Automate downloads | [`atlas get`, batch, JSON, and progress](docs/commands.md) |
| Understand the system | [Architecture](docs/architecture.md) |
| Contribute | [Contribution guide](CONTRIBUTING.md) |

<details>
<summary>On this page</summary>

- [Why Atlas](#why-atlas)
- [Verification evidence](#verification-evidence)
- [Install](#install)
- [First run](#first-run)
- [What Atlas handles](#what-atlas-handles)
- [Common workflows](#common-workflows)
- [Interactive menu](#interactive-menu)
- [Automation and saved sessions](#automation-and-saved-sessions)
- [Display and accessibility](#display-and-accessibility)
- [Configuration](#configuration)
- [Documentation](#documentation)
- [Support Atlas](#support-atlas)
- [Development](#development)

</details>

## Install

Atlas requires Python 3.12 or newer. Installing the source preview also requires
[`uv`](https://docs.astral.sh/uv/getting-started/installation/). Media workflows
that merge streams or post-process audio or video need `ffmpeg` and `ffprobe`.
The Python package includes `yt-dlp` and `mutagen`; accelerated files and mirrors
can additionally use `aria2c`, `wget2`, and `wget`.

### Try the current source preview

```bash
git clone https://github.com/xkam7ar/atlas-download-manager.git
cd atlas-download-manager
uv tool install . --force
atlas --version
atlas setup --no-install
```

To preview the local guided installer without changing the machine:

```bash
bash install.sh --no-install --no-menu --yes
```

> [!NOTE]
> Atlas is currently an alpha source preview for macOS and Linux. The clone path
> above installs the checked-out source for evaluation; no supported PyPI,
> Homebrew, or automatic-update channel exists yet. Packages named `atlas` on
> PyPI and Homebrew are unrelated.

See [Installation](docs/installation.md) for the immutable-release, checksum,
update, and packaging policy.

## First run

1. Verify the command and network path.

   ```bash
   atlas --version
   atlas doctor --network
   ```

   Run full `atlas doctor` before media or accelerated-transfer work; it reports
   missing system tools that do not block this native-file first run.

2. Open the menu and paste a URL.

   ```bash
   atlas
   ```

3. Review a plan without downloading.

   ```bash
   atlas get "https://raw.githubusercontent.com/xkam7ar/atlas-download-manager/main/LICENSE" \
     --kind file --backend native --output-dir ./atlas-demo --dry-run
   ```

4. Run the same intent when the plan looks right.

   ```bash
   atlas get "https://raw.githubusercontent.com/xkam7ar/atlas-download-manager/main/LICENSE" \
     --kind file --backend native --output-dir ./atlas-demo
   ```

The [Quick start](docs/quick-start.md) walks through menu, CLI, dry-run, output,
and recovery behavior in one short path.

## What Atlas handles

| Area | Atlas provides |
| --- | --- |
| Smart routing | `atlas get URL` detects media and direct files while keeping recursive mirrors explicit. |
| Interactive use | Searchable menus, focused customization, plan review, progress, and recovery. |
| Media | Video, audio, playlists, formats, subtitles, chapters, thumbnails, and metadata through `yt-dlp`. |
| Direct files | Native, `aria2c`, and `wget2` downloads with resume, checksums, and safe output naming. |
| Mirrors | Explicit, bounded website and open-directory workflows through `wget2` or `wget`. |
| Batches | Per-URL routing, bounded concurrency, partial-failure handling, manifests, and retries. |
| Automation | Dry runs, stable JSON summaries, NDJSON progress, quiet mode, and backend pass-through. |

## Common workflows

| Goal | Example |
| --- | --- |
| Let Atlas choose | `atlas get URL` |
| Download video | `atlas video URL --quality compatible` |
| Extract audio | `atlas audio URL --codec mp3` |
| Download a playlist | `atlas playlist PLAYLIST_URL --type audio` |
| Download one channel item | `atlas video CHANNEL_URL --playlist --playlist-items 1` |
| Download a file | `atlas file URL --backend aria2` |
| Mirror a website | `atlas site URL --depth 2 --dry-run` |
| Mirror an open directory | `atlas dir URL --depth 2 --dry-run` |
| Process a URL list | `atlas batch urls.txt --concurrency 3` |
| Inspect a previous run | `atlas inspect-session OUTPUT --preview plan` |

Mirroring is always explicit. `atlas get` does not recursively copy an ordinary
page unless you choose `--kind site` or `--kind dir`.

For full flags and behavior, use the [Command reference](docs/commands.md).

## Interactive menu

Running `atlas` in a real terminal opens the primary human interface:

```text
Paste URL
Media
Files
Batch
Sessions
Tools
Settings
Quit
```

The normal path is deliberately short:

```text
choose intent  ->  enter URL  ->  review plan  ->  start
                                     |
                                     +-> customize / dry run / back
```

The menu and CLI use the same typed request models and planner. The menu does
not open automatically for pipes, non-TTY sessions, JSON output, `--no-menu`,
or common CI environments.

`Tools` → `Help` lists only controls supported by the current prompt: move,
select, type-to-filter, multi-select, and cancel/back.

## Automation and saved sessions

Use JSON for stable command results and NDJSON for live progress:

```bash
atlas get URL --dry-run --json
atlas batch urls.txt --json
atlas file URL --progress json
```

`--json` always wins over `--progress`: it emits one final JSON document and
suppresses human/live progress. Use `--progress json` without `--json` for an
NDJSON event stream.

Completed batch, site, and directory runs write private artifacts under
`<output>/.atlas/`. The stable newest generation is under
`<output>/.atlas/latest/`:

```text
summary.json
manifest.json
failed.txt
skipped.txt
canceled.txt
retry.atlas.json
```

Recover or inspect without reconstructing the original command:

```bash
atlas resume OUTPUT
atlas retry OUTPUT --checksum-failures-only
atlas inspect-session OUTPUT --panel failed
atlas export-failed OUTPUT --output failed.txt
```

Saved-session links and output paths are constrained to their trusted Atlas
artifact boundary. Inspection does not create a missing output folder.

## Display and accessibility

```bash
atlas --theme auto COMMAND ...
atlas --theme high-contrast COMMAND ...
atlas --plain COMMAND ...
atlas --no-unicode COMMAND ...
atlas --no-animation COMMAND ...
```

Atlas also honors `NO_COLOR`, `TERM=dumb`, and `ATLAS_NO_ANIMATION`. JSON and
NDJSON modes remain free of human UI.

## Configuration

```bash
atlas config path
atlas config show
```

Atlas uses `platformdirs` for host-native locations. Common configuration paths
are:

```text
macOS  ~/Library/Application Support/atlas/config.toml
Linux  ~/.config/atlas/config.toml
```

`atlas config path` is authoritative for the current host. The default download
directory is `~/Downloads/atlas` on both supported platform families.

See [Configuration](docs/configuration.md) for keys, environment variables,
output organization, archive behavior, and authorized cookie use.

## Design principles

- Plan before execution.
- Keep common paths short and advanced controls discoverable.
- Never hide recursive scope, overwrite behavior, or system changes.
- Keep human output readable and machine output stable.
- Continue batches after item failures and preserve recovery artifacts.
- Keep planning, execution, progress, and rendering independently testable.

The human-facing rules live in [UI and UX guidelines](docs/ui-ux.md). Normative
engineering behavior lives in [System contracts](docs/system-contracts.md).

## Documentation

The [documentation home](docs/README.md) organizes the full set by reader goal.

| Use Atlas | Understand Atlas | Build Atlas |
| --- | --- | --- |
| [Quick start](docs/quick-start.md) | [Why Atlas](docs/why-atlas.md) | [Architecture](docs/architecture.md) |
| [Commands](docs/commands.md) | [Download planning](docs/download-planning.md) | [System contracts](docs/system-contracts.md) |
| [Configuration](docs/configuration.md) | [Media edge cases](docs/media-edge-cases.md) | [UI and UX](docs/ui-ux.md) |
| [Troubleshooting](docs/troubleshooting.md) | [Mirror policy](docs/mirror-policy.md) | [Development](docs/development.md) |
| [Responsible use](docs/responsible-use.md) | [Migration](docs/migration.md) | [Downloader research](docs/download-research.md) |

## Support Atlas

If Atlas has earned a place in your workflow, consider
[starring the repository](https://github.com/xkam7ar/atlas-download-manager)—it helps other
people find the project. Stars do not unlock features or change support
priority; a clear bug report, documentation fix, or focused pull request is just
as valuable.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
sh -n install.sh
uv build
git diff --check
```

See the [Development guide](docs/development.md) for contributor workflows,
project layout, documentation ownership, packaging, and manual smoke tests.

## License

Atlas is licensed under the [MIT License](LICENSE).
