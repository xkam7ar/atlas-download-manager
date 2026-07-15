# Development guide

[Documentation home](README.md) · [Architecture](architecture.md) ·
[System contracts](system-contracts.md) · [UI and UX](ui-ux.md)

This page is for contributors working in the local checkout.

## Requirements

- Python 3.12+
- `uv`
- `ffmpeg` and `ffprobe`
- optional `aria2c` for accelerated direct-file and Metalink workflows
- optional `wget2` for direct-file and website-mirror workflows
- optional `wget` as a website-mirror fallback

Install the common local tools on macOS:

```bash
brew install ffmpeg aria2 wget2 wget
```

## Setup

```bash
uv sync --group dev
uv run atlas --help
uv run atlas setup --no-install
```

Install as a user tool:

```bash
uv tool install . --force
```

The executable is installed under the uv tool bin directory, typically:

```text
~/.local/bin/atlas
```

## Quality gate

Run before claiming work is complete:

```bash
uv run pytest
uv run ruff check src/atlas tests
uv run mypy
uv build
git diff --check
```

Current test style:

- no network required for unit tests
- CLI tests use `typer.testing.CliRunner`
- engine interactions are faked where needed
- planner/preset tests assert concrete `ydl_opts`

## Project layout

```text
src/atlas/
  __init__.py
  __main__.py
  cli.py
  menu.py
  models.py
  planner.py
  media_capabilities.py
  presets.py
  engine.py
  aria2_rpc.py
  adapters.py
  hub.py
  optimizer.py
  sessions.py
  backends.py
  passthrough.py
  network.py
  file_probe.py
  directory_index.py
  directory_parser.py
  directory_scanner.py
  directory_tree.py
  directory_explorer.py
  adaptive.py
  progress.py
  theme.py
  views.py
  formats.py
  config.py
  paths.py
  doctor.py
  setup.py
  preflight.py
  batch.py
  urls.py
  runner.py
  errors.py
  logging.py
  py.typed
tests/
docs/
```

## Documentation and architecture reviews

Start from the implementation, then update the docs that own the behavior. Do
not treat architecture docs as aspirational when source code says otherwise.

Minimum review loop:

1. Inspect affected code paths with the codebase knowledge graph first; use
   `rg` for literal text, configuration, and non-code files.
2. Update [System Contracts](system-contracts.md) if ownership boundaries,
   artifacts, JSON output, progress phases, menu capability, safety policy, or
   verification rules change.
3. Update [Architecture](architecture.md) for data-flow or module-boundary
   changes.
4. Update [Download Planning](download-planning.md) and
   [Smart Sessions](smart-sessions.md) for routing, scan, manifest, scheduler,
   or preset changes.
5. Update [UI and UX Guidelines](ui-ux.md) for visible human UI changes.
6. Update [Commands](commands.md), [Configuration](configuration.md), and the
   root [README](../README.md) when user-visible commands, flags, config keys,
   or common workflows change.
7. Run `rg` drift checks for renamed commands, stale launcher labels, old scan
   state text, stale backend names, and old
   safety language before finishing.

## Adding a command

Commands are the scriptable API; the interactive menu is the primary human
interface. Any new normal operator command must have menu parity unless it is
explicitly documented and tested as script-only.

1. Start with tests in `tests/test_cli.py`.
2. Add or reuse Pydantic models in `models.py`.
3. Keep raw option construction out of `cli.py`.
4. Route new download behavior through `SmartPlanner`, `DownloadOptimizer`, or
   the matching typed backend planner.
5. Attach or update a `SmartDownloadSession` in `sessions.py` when the command
   changes the scan-plan-execute contract.
6. Add JSON output if the command is useful for automation.
7. Add a `MenuCapability` entry in `menu.py`, add a focused menu flow or reuse an
   existing one, and update `tests/test_menu.py` parity coverage. Raw backend
   commands should stay grouped under `Tools` -> `Advanced backend`.
8. Document the command in [Commands](commands.md) and update
   [Smart Sessions](smart-sessions.md) when the session preset changes.

## Adding planner behavior

1. Add a failing planner test in `tests/test_planner.py`.
2. Update enums or fields in `models.py`.
3. Implement mapping in `planner.py`.
4. Ensure `presets.py` translates new media plan fields to `ydl_opts`.
5. Update `sessions.py` if the planner change affects manifest, customization,
   scheduler policy, or artifact expectations.
6. Add dry-run, preset, optimizer, or session tests when the resulting output
   shape matters.

## Typed package

`src/atlas/py.typed` marks the package as typed.

`yt-dlp` does not currently provide complete typing for this project, so mypy
ignores missing imports for `yt_dlp` in `pyproject.toml`.

## Release and build

Build artifacts:

```bash
uv build
```

This produces:

```text
dist/atlas-0.1.0.tar.gz
dist/atlas-0.1.0-py3-none-any.whl
```

Installer and packaging checks:

```bash
zsh -n install.sh
ruby -c packaging/homebrew/atlas.rb
./install.sh --no-install --no-menu --yes
uv run atlas setup --json
uv run atlas update --dry-run --json
```

The Homebrew formula in `packaging/homebrew/atlas.rb` is a tap template until a
release tarball SHA and generated Python resource blocks are added. Before
publishing the tap, copy the formula into the tap, replace the SHA, run
`brew update-python-resources atlas`, and test:

```bash
brew install xkam7ar/tap/atlas
atlas doctor --json
```

## Manual smoke tests

Useful local smoke commands:

```bash
uv run atlas --help
uv run atlas setup --json
uv run atlas setup --no-install
uv run atlas update --dry-run
uv run atlas doctor
uv run atlas doctor --fix --no-install
uv run atlas doctor --json
uv run atlas config path
uv run atlas config show
uv run atlas video "https://example.com/watch?v=1" --dry-run
uv run atlas video "https://example.com/watch?v=1" --subtitle-only --dry-run --json
uv run atlas audio "https://example.com/watch?v=1" --dry-run --json
uv run atlas audio "https://example.com/watch?v=1" --info-only --dry-run --json
uv run atlas playlist "https://www.youtube.com/playlist?list=PL123" --type audio --dry-run
uv run atlas get "https://example.com/archive.zip" --kind file --backend wget2 --dry-run --json
uv run atlas file "https://example.com/archive.zip" --adaptive --explain --json
uv run atlas site "https://example.com/docs/" --backend wget2 --depth 1 --same-host-only --max-runtime 60 --dry-run
uv run atlas dir "https://example.com/files/" --backend wget2 --same-host-only --max-files 100 --adaptive --explain --json
uv run atlas batch /tmp/urls.txt --kind file --adaptive --explain --json
uv run atlas batch /tmp/urls.txt --kind file --adaptive --progress json
uv run atlas wget2 --dry-run -- --version
```

For live metadata checks, use `info` or `formats`; those call the network.

For live downloader smoke tests, prefer small bounded fixtures first:

- one tiny direct file
- one medium ranged direct file
- one small open-directory mirror with `--accept`
- one batch that includes duplicate basenames

After downloader changes, run the full quality gate plus focused CLI/backend
tests for the touched path. Useful focused commands include:

```bash
uv run pytest tests/test_backends.py tests/test_cli.py tests/test_progress.py tests/test_adaptive.py tests/test_optimizer.py -q
uv run ruff check src/atlas tests
uv run mypy
```

## Related

- [Architecture](architecture.md)
- [System contracts](system-contracts.md)
- [UI and UX guidelines](ui-ux.md)
- [Command reference](commands.md)
