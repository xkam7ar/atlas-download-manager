# Why Atlas

[Project README](../README.md) · [Quick start](quick-start.md) ·
[Command reference](commands.md) · [Download planning](download-planning.md)

Atlas is a plan-first Python CLI and interactive terminal download manager. It
gives video, audio, direct-file, website-mirror, open-directory, and batch
downloads one inspectable workflow while leaving execution to proven tools such
as `yt-dlp`, `aria2c`, `wget2`, and `wget`.

## Who Atlas is for

Atlas fits people who already download authorized content from the terminal but
do not want every backend to become a separate workflow. It is especially useful
when you want to:

- paste a URL and review the detected intent before anything is downloaded;
- use the same interface for media, files, recursive mirrors, and mixed batches;
- keep recursive scope, overwrite behavior, and system changes visible;
- recover failed or canceled batch items from saved session artifacts; or
- switch between an interactive menu, normal CLI output, JSON, and NDJSON
  progress without rebuilding the download plan.

If one direct `yt-dlp`, `aria2c`, or `wget` command already does everything you
need, the backend by itself is usually the shorter choice.

## What Atlas is not

Atlas is not a desktop GUI, browser extension, BitTorrent client, remote download
service, or timed scheduler. Windows is not currently a supported platform.
Atlas also does not make a backend faster or more source-compatible than the
underlying tool, and it does not bypass DRM, paywalls, access controls, or
platform restrictions.

## Atlas compared with the underlying downloaders

Atlas does not replace or reimplement its backends. It adds intent detection,
typed plans, a shared terminal experience, and recovery around them.

| Tool or approach | Best fit | What Atlas adds |
| --- | --- | --- |
| `yt-dlp` directly | One known video, audio, playlist, or extractor task | Guided presets, plan review, shared output conventions, and the same session model used by non-media jobs. |
| `aria2c` directly | High-throughput file transfers with known arguments | Backend selection, safe destination planning, checksums, progress normalization, and batch routing. |
| `wget2` or `wget` directly | A known file or explicitly scoped recursive copy | Dry-run summaries, visible mirror bounds, open-directory intent, private recovery artifacts, and consistent result reporting. |
| Handwritten shell scripts | Stable, narrowly defined automation | Per-URL routing, partial-failure continuation, retry manifests, final JSON, and live NDJSON progress. |
| Atlas | Mixed or unfamiliar download workflows that should be reviewed first | One menu and CLI across the tools above, without concealing which engine will run. |

## What makes the workflow different

### The plan is a first-class result

`atlas get URL --dry-run` resolves intent and reports the selected backend,
output, and important safety notes without transferring the target payload.
Explicit site and open-directory commands make recursive behavior opt-in.

### Interactive and automated use share the same model

The searchable menu is the primary human interface, but it builds the same typed
requests as CLI commands. Automation can request one final JSON document or an
NDJSON progress stream without scraping terminal decoration.

### Partial failure is preserved, not flattened

Batch, site, and directory sessions record manifests and item states under the
private `.atlas` artifact boundary. Inspection, resume, and targeted retry
commands can act on those states without reconstructing the original command.

### The backend remains visible

Atlas reports the planned engine and supports controlled pass-through options.
It is an orchestration layer for established downloaders, not a claim that one
engine is best for every URL.

## Frequently asked questions

### Is Atlas a replacement for yt-dlp?

No. Atlas uses `yt-dlp` for media extraction and download work. It adds guided
choices, plan review, consistent output, batches, and recovery alongside direct
file and mirror backends. Advanced users can still use `yt-dlp` directly or pass
through supported backend arguments.

### What can Atlas download?

Atlas handles authorized video, audio, playlists, direct files, bounded website
mirrors, open directories, and text-file batches. The actual source support for
media comes from the installed `yt-dlp` version; file and mirror behavior depends
on native Python, `aria2c`, `wget2`, or `wget` capabilities.

### Does Atlas support macOS and Linux?

Yes. The documented and tested platform families are macOS and Linux, with
Python 3.12 or newer. Optional external tools determine which accelerated file,
media post-processing, and mirror paths are available on a given host.

### Can Atlas be used in scripts and CI?

Yes. Commands support dry runs, stable final JSON, NDJSON progress, quiet output,
and explicit exit behavior. The no-argument interactive menu does not open in
common CI environments, non-TTY sessions, pipes, or machine-output modes.

### Can Atlas bypass DRM, paywalls, or access controls?

No. Atlas is for content you are allowed to access and download. It does not
bypass DRM, paywalls, platform restrictions, bot challenges, or authorization
controls. See [Responsible use](responsible-use.md).

### Is the package available from PyPI or Homebrew?

Atlas is available from the official Homebrew tap:
`brew install xkam7ar/tap/atlas-download-manager`. The unqualified PyPI and
Homebrew name `atlas` belongs to unrelated projects. This project uses
`atlas-download-manager` for its distribution, repository, and formula identity
while keeping the `atlas` command. It is not currently published on PyPI.

## Try the workflow

Start with the [Quick start](quick-start.md) to verify the environment, open the
menu, review a dry run, complete a first download, and inspect recovery data.
