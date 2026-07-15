# Quick start

[Documentation home](README.md) · [Installation](installation.md) ·
[Commands](commands.md) · [Troubleshooting](troubleshooting.md)

This path takes Atlas from installation check to a reviewed plan and first
completed download. It uses one command at a time and keeps every system change
explicit.

## 1. Verify the command

```bash
atlas --version
atlas doctor
```

`atlas --version` confirms which executable your shell found. Doctor then checks
Python, paths, TLS, and the optional runtime tools.

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

Use a URL you are authorized to access:

```bash
atlas get "https://example.com/archive.zip" --dry-run
```

A dry run resolves the intent and prints the planned backend, output, and safety
notes without starting the transfer. For automation, add `--json`:

```bash
atlas get "https://example.com/archive.zip" --dry-run --json
```

Some adaptive plans perform lightweight network probes before producing a plan.
They do not download the target payload, but they can issue verified HTTP
requests to inspect size, range support, or directory links.

## 4. Run the reviewed plan

Remove `--dry-run` when the source, scope, backend, and output look right:

```bash
atlas get "https://example.com/archive.zip"
```

The normal macOS output folder is:

```text
~/Downloads/atlas
```

Atlas shows phase-aware progress and does not claim completion until required
verification or post-processing is finished.

## 5. Confirm the result

A successful run ends with a summary containing the saved path. Batch, site,
and directory sessions also write recovery data under:

```text
<output>/.atlas/latest/
```

Inspect the newest session without retrying anything:

```bash
atlas inspect-session ~/Downloads/atlas
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

`resume` includes failed, skipped-unknown, and canceled-before-start items.
`retry` defaults to failed items and can target a more specific status.

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
> secrets in its logs and artifacts, but it cannot remove them from your shell
> history or from third-party tools outside its process.
