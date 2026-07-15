# Atlas

**Paste a URL. Review the plan. Download with the right engine.**

Atlas is a menu-first download utility for media, direct files, website mirrors,
open directories, and repeatable batches. It detects intent, builds a typed plan,
and shows what will happen before handing work to `yt-dlp`, native Python,
`aria2c`, `wget2`, or `wget`.

```text
URL  ->  detect  ->  review  ->  download  ->  summary
```

> [!IMPORTANT]
> Use Atlas only for content you are allowed to access. Atlas does not bypass
> DRM, paywalls, access controls, platform restrictions, bot challenges, or
> other protections. Cookies and request controls are for legitimate,
> user-authorized access.

## Start here

| I want to… | Start with |
| --- | --- |
| Try Atlas for the first time | [Quick start](docs/quick-start.md) |
| Install or repair Atlas | [Installation](docs/installation.md) |
| Find a command | [Command reference](docs/commands.md) |
| Fix a problem | [Troubleshooting](docs/troubleshooting.md) |
| Automate downloads | [`atlas get`, batch, JSON, and progress](docs/commands.md) |
| Understand the system | [Architecture](docs/architecture.md) |
| Contribute | [Development guide](docs/development.md) |

<details>
<summary>On this page</summary>

- [Install](#install)
- [First run](#first-run)
- [What Atlas handles](#what-atlas-handles)
- [Common workflows](#common-workflows)
- [Interactive menu](#interactive-menu)
- [Automation and saved sessions](#automation-and-saved-sessions)
- [Display and accessibility](#display-and-accessibility)
- [Configuration](#configuration)
- [Documentation](#documentation)
- [Development](#development)

</details>

## Install

Atlas requires Python 3.12 or newer. Media workflows also need `ffmpeg` and
`ffprobe`; accelerated files and mirrors can use `aria2c`, `wget2`, and `wget`.

### Guided installer

```bash
curl -fsSL https://raw.githubusercontent.com/xkam7ar/atlas/main/install.sh | bash
```

The installer shows its plan, checks Homebrew and runtime tools, installs Atlas,
runs setup and Doctor, and can open the menu. It never installs Homebrew
silently.

> [!TIP]
> Inspect before executing:
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/xkam7ar/atlas/main/install.sh -o /tmp/atlas-install.sh
> less /tmp/atlas-install.sh
> bash /tmp/atlas-install.sh --no-install --no-menu --yes
> ```

### Install with uv

From GitHub:

```bash
uv tool install git+https://github.com/xkam7ar/atlas.git
atlas setup
```

From this checkout:

```bash
uv tool install . --force
atlas setup --no-install
```

### Homebrew packaging status

The checked-in Homebrew formula is a release template. It must receive a real
release checksum and generated Python resource blocks before publication to a
tap. See [Installation](docs/installation.md#homebrew-release-packaging) for the
release contract.

## First run

1. Verify the environment.

   ```bash
   atlas doctor
   ```

2. Open the menu and paste a URL.

   ```bash
   atlas
   ```

3. Review a plan without downloading.

   ```bash
   atlas get "https://example.com/archive.zip" --dry-run
   ```

4. Run the same intent when the plan looks right.

   ```bash
   atlas get "https://example.com/archive.zip"
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

The default macOS configuration path is:

```text
~/Library/Application Support/atlas/config.toml
```

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
| [Quick start](docs/quick-start.md) | [Smart sessions](docs/smart-sessions.md) | [Architecture](docs/architecture.md) |
| [Commands](docs/commands.md) | [Download planning](docs/download-planning.md) | [System contracts](docs/system-contracts.md) |
| [Configuration](docs/configuration.md) | [Media edge cases](docs/media-edge-cases.md) | [UI and UX](docs/ui-ux.md) |
| [Troubleshooting](docs/troubleshooting.md) | [Mirror policy](docs/mirror-policy.md) | [Development](docs/development.md) |
| [Responsible use](docs/responsible-use.md) | [Migration](docs/migration.md) | [Downloader research](docs/download-research.md) |

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run mypy src
uv build
git diff --check
```

See the [Development guide](docs/development.md) for contributor workflows,
project layout, documentation ownership, packaging, and manual smoke tests.

## License

Atlas is licensed under the [MIT License](LICENSE).
