# Quick start

[Documentation home](README.md) · [Installation](installation.md) ·
[Commands](commands.md) · [Troubleshooting](troubleshooting.md)

This path takes Atlas from installation check to a reviewed plan and first
completed download. It uses one command at a time and keeps every system change
explicit.

## 1. Verify the command

```bash
atlas --version
atlas doctor --network
```

`atlas --version` confirms which executable your shell found. The focused
network check then makes one verified HTTPS GET to `https://www.python.org/`
with a three-second timeout to validate Python's TLS and CA path without making
optional downloader tools a prerequisite for the first native-file example.

Run full `atlas doctor` before media, mirror, or accelerated-transfer work. It
checks Python, paths, TLS, `yt-dlp`, `mutagen`, required media tools, and optional
transfer backends. Default human mode also creates/checks Atlas directories with
temporary write probes; `--json` and `--fix --no-install` use non-mutating path
checks but still perform the HTTPS probe.

> [!NOTE]
> Missing `aria2c`, `wget2`, or `wget` does not block ordinary media and native
> file downloads. Missing `ffmpeg` or `ffprobe` does block media workflows that
> need merging, extraction, or post-processing.

If the command is missing or Doctor reports a required gap, start with
[Installation](installation.md) or jump to [Troubleshooting](troubleshooting.md).

## 2. Open the menu

```bash
atlas
```

The first launcher action is **Paste URL**, followed by grouped Media, Files,
Batch, Sessions, Tools, and Settings areas.

Menu prompts support:

- arrow keys to move;
- Enter to choose;
- typing to filter the visible choices;
- Space to toggle rows in a multi-select prompt;
- Ctrl-C to cancel the prompt and go back.

Choose **Tools → Help** whenever you need the contextual key guide.

## 3. Review without downloading

Use Atlas's own small, public source-preview license file for a deterministic
first run. This baseline selects the built-in backend so the reviewed and
executed plans do not depend on optional transfer tools:

```bash
atlas get "https://raw.githubusercontent.com/xkam7ar/atlas-download-manager/main/LICENSE" \
  --kind file --backend native --output-dir ./atlas-demo --dry-run
```

A dry run resolves the intent and prints the planned backend, output, and safety
notes without starting the transfer. For automation, add `--json`:

```bash
atlas get "https://raw.githubusercontent.com/xkam7ar/atlas-download-manager/main/LICENSE" \
  --kind file --backend native --output-dir ./atlas-demo --dry-run --json
```

Some adaptive plans perform lightweight network probes before producing a plan.
They do not download the target payload, but they can issue verified HTTP
requests to inspect size, range support, or directory links.

## 4. Run the reviewed plan

Remove `--dry-run` when the source, scope, backend, and output look right:

```bash
atlas get "https://raw.githubusercontent.com/xkam7ar/atlas-download-manager/main/LICENSE" \
  --kind file --backend native --output-dir ./atlas-demo
```

This example writes `LICENSE` under `./atlas-demo`. Without an explicit output
directory, the normal folder on macOS and supported Linux hosts is:

```text
~/Downloads/atlas
```

Atlas shows phase-aware progress and does not claim completion until required
verification or post-processing is finished.

## 5. Confirm the result

A successful run ends with a summary containing the saved path. Confirm that
the deterministic first-download result exists:

```bash
test -f ./atlas-demo/LICENSE && echo "Atlas first download complete"
```

Batch, site, and directory sessions also write recovery data under:

```text
<output>/.atlas/latest/
```

After one of those session-producing workflows, inspect its output directory
without retrying anything:

```bash
atlas inspect-session OUTPUT
```

## Recover an interrupted session

Start with inspection:

```bash
atlas inspect-session ~/Downloads/atlas --panel failed
atlas inspect-session ~/Downloads/atlas --preview errors
```

Then choose the narrowest recovery action:

```bash
atlas resume ~/Downloads/atlas
atlas retry ~/Downloads/atlas --failed-only
atlas retry ~/Downloads/atlas --canceled-only
atlas export-failed ~/Downloads/atlas --output failed.txt
```

`resume` includes failed, skipped-unknown, and canceled items, whether they were
canceled while queued or during controllable active work. `retry` defaults to
failed items and can target a more specific status.

## Choose the next guide

| Goal | Continue with |
| --- | --- |
| Learn command flags | [Command reference](commands.md) |
| Change defaults | [Configuration](configuration.md) |
| Understand planning | [Download planning](download-planning.md) |
| Work with sessions | [Smart sessions](smart-sessions.md) |
| Mirror safely | [Mirror policy](mirror-policy.md) |
| Fix a failure | [Troubleshooting](troubleshooting.md) |
| Understand access boundaries | [Responsible use](responsible-use.md) |

> [!IMPORTANT]
> URLs can contain temporary credentials or signatures. Avoid pasting sensitive
> URLs into shared shells, screenshots, tickets, or chat. Atlas redacts known
> secrets in human output, plans, and backend-command previews. Owner-only
> retry artifacts can retain the original URL so recovery remains possible;
> they are sensitive and must not be shared. Atlas also cannot remove secrets
> from shell history or third-party tools outside its process.
