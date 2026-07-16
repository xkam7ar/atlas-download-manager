# Troubleshooting

[Documentation home](README.md) · [Quick start](quick-start.md) ·
[Commands](commands.md) · [Installation](installation.md)

## Find your issue

| Symptom | Likely area | Jump to |
| --- | --- | --- |
| Atlas is missing or the wrong executable runs | Installation or PATH | [Atlas installed but not on PATH](#atlas-installed-but-not-on-path) |
| Doctor reports a missing runtime | Runtime tools | [ffmpeg](#missing-ffmpeg-or-ffprobe), [aria2c](#missing-aria2c), or [wget](#missing-wget2-or-wget) |
| A scan fails while the browser works | TLS or CA bundle | [Certificate verification](#scan-fails-with-tls-certificate-verification) |
| A mirror stops or is only partially complete | Scope, bounds, or broken links | [Mirror bounds](#mirror-stops-before-downloading) or [partial downloads](#wget2-mirror-exits-after-partial-downloads) |
| Transfer completes but the command fails | Media post-processing | [Post-processing](#media-post-processing-fails-after-transfer) |
| A playlist is unexpectedly large or refused | Playlist intent | [Playlist surprises](#playlist-surprises) |
| A completed item is skipped | Download archive | [Download archive](#download-archive) |
| A batch or mirror was interrupted | Saved session | [Recover a failed or canceled session](#recover-a-failed-or-canceled-session) |
| More detail is needed | Diagnostics | [Verbose mode](#verbose-mode) |

Start with:

```bash
atlas doctor
atlas doctor --fix
atlas doctor --network
```

For automation:

```bash
atlas doctor --json
atlas setup --json
```

`atlas doctor --fix` prints a full runtime repair plan and can run Homebrew,
apt, dnf, or pacman commands with confirmation or `--yes`.
Use `atlas doctor --network --fix-certs` when scans fail with TLS or certificate
errors. It prints CA-bundle and Homebrew/Python repair guidance, but it never
turns off certificate verification silently.

Every Doctor mode performs one verified HTTPS GET to `https://www.python.org/`
with a three-second timeout. Default human mode creates/checks Atlas directories
and temporary write probes. `--json` and `--fix --no-install` keep path checks
non-mutating, but they are not offline modes.

## Installer plans a Homebrew bootstrap

Homebrew is the macOS package layer and the `wget2` fallback on pacman-based
Linux. When missing and needed, its official bootstrap command appears in the
complete installer plan. Approving that plan authorizes the bootstrap; Atlas
never runs it before showing it.

In this pre-release checkout, inspect the local installer plan:

```bash
bash install.sh --no-install --no-menu --yes
```

`--no-install` prints the bootstrap and package commands without creating files

## Installer finds an old atlas command

The installer only accepts an existing command when `atlas setup --help` works.
If an older Atlas command is on `PATH`, the installer updates or reinstalls
Atlas when installation is allowed.

If the shell still resolves the old executable after reinstalling:

```bash
command -v atlas
hash -r
atlas setup --no-install
```

Open a new shell if your terminal cached the old path.

## atlas installed but not on PATH

`uv tool install` installs executables into the uv tool bin directory. If the
installer prints `atlas is not on PATH yet`, add that directory to `PATH` or open
a new shell after letting uv modify your shell profile.

Common checks:

```bash
uv tool dir
uv tool list
command -v atlas
```

Then verify:

```bash
atlas setup --no-install
atlas doctor
```

## Homebrew formula is not available

Before the public repository and tap are published, both
`xkam7ar/tap/atlas` and the bootstrap installer's GitHub fallback are
unavailable. Install this checkout with `uv tool install . --force`; use the
installer only in `--no-install` mode to review its future plan. Do not replace
the tap-qualified command with `brew install atlas`, which installs an unrelated
database tool.

The checked-in Homebrew formula under `packaging/homebrew/atlas.rb` is a release
template. The tap formula must have the release tarball SHA and generated Python
resource blocks before it is publishable.

## Missing ffmpeg or ffprobe

Symptoms:

- Doctor shows `ffmpeg` or `ffprobe` missing.
- Real downloads fail before metadata extraction.
- Audio extraction or video merging cannot proceed.

Fix:

```bash
atlas setup --minimal --install
```

Direct manager equivalents are `brew install ffmpeg`,
`sudo apt-get install -y ffmpeg`, `sudo dnf install -y ffmpeg-free`, and
`sudo pacman -S --needed ffmpeg`.

Why required:

- `ffmpeg` performs audio extraction, metadata embedding, thumbnail embedding,
  subtitle embedding, and video/audio merging.
- `ffprobe` is part of the same expected media toolchain.

## Missing aria2c

`aria2c` is optional.

Install:

```bash
atlas setup --full --install
```

Atlas maps this to package `aria2` on every supported manager.

If missing:

- `--download-engine auto` falls back to yt-dlp native.
- `--download-engine aria2` fails early.

aria2 is only configured for HTTP/HTTPS external downloads. It is not guaranteed
to improve DASH or HLS streams.

For `atlas file --backend aria2`, atlas starts a local aria2c RPC subprocess on
`127.0.0.1` with a random secret and shuts it down when the download finishes.
If RPC startup fails, atlas can fall back to the older streamed subprocess path.

For an all-aria2 batch, RPC startup failure falls back to ordinary per-item
batch execution. If the shared RPC session disappears after transfers begin,
Atlas preserves completed items, marks unresolved items failed, and removes
active GIDs best-effort. TLS-chain failures receive a verified-curl per-item
retry; other mid-session RPC failures do not automatically retry through the
legacy aria2 subprocess. Inspect `latest/failed.txt` before retrying.

## Missing wget2 or wget

`wget2` is optional unless you choose `atlas file --backend wget2`, set
`file_backend = "wget2"`, or mirror a site with the default preferred backend.
`wget` is optional as a website-mirror fallback.

Install Wget2:

```bash
atlas setup --mirrors --install
```

Install Wget fallback support:

The same plan installs `wget`. On pacman hosts it installs `wget` natively and
bootstraps Linuxbrew for `wget2`; apt and dnf provide both through native
repositories.

If Wget2 is missing:

- `atlas file` still works through native or aria2c backends.
- `atlas file --backend wget2` fails early with an install hint.
- `atlas site --backend auto` can use `wget` when it is installed.
- `atlas site --backend wget2` fails early with an install hint.
- `atlas dir --backend auto` can use `wget` when it is installed.
- `atlas dir --backend wget2` fails early with an install hint.

Wget2 feature warnings from `atlas doctor` are optional capability checks. For
example, missing Brotli or HTTP/2 support matters only when a workflow selects
those features.

## Wget2 mirror exits after partial downloads

Symptoms:

- `atlas site` or `atlas dir` prints an error such as `wget2 exited 8`.
- Some files were downloaded before the command failed.
- The error includes downloaded bytes and failed URL samples.

Why it happens:

- Recursive pages can contain broken links, malformed anchors, or old directory
  entries.
- Wget2 reports the mirror as failed even when many files were saved.

What to do:

```bash
atlas dir URL --adaptive --explain --json
atlas dir URL --depth 1 --accept pdf --progress full
```

If you need every known file and have an authoritative URL list, use exact-list
batch mode:

```bash
atlas batch urls.txt --kind file --adaptive --per-host-concurrency 2 --progress full
```

Batch mode downloads each URL directly and avoids relying on recursive HTML
traversal.

## Scan fails with TLS certificate verification

Symptoms:

- Smart download, `Browse directory`, or `Batch` -> `Paste URL and scan` shows
  `Scan failed`.
- The details mention TLS certificate verification, CA bundle, or Python SSL.
- Browsers or `curl` may succeed while Atlas scan fetches fail.

What to do:

```bash
atlas doctor --network
atlas doctor --network --fix-certs
atlas setup --minimal
```

Why it happens:

- Atlas scan/probe code uses the shared verified fetch layer, not browser trust.
- Python SSL, certifi, or the local CA bundle can drift from the browser/tool
  trust chain.

What Atlas will do:

- keep TLS verification on by default
- classify the scan as `failed` instead of pretending the page was empty
- allow a safe backend fetch fallback for discovery when available
- retry direct-file downloads with verified `curl` when Python, aria2c, or
  Wget2 cannot build the issuer chain but curl can verify the same URL
- keep `Continue as mirror` and `Backend fetch` explicit recovery actions

Atlas does not silently disable certificate verification. If a scan only works
with `--no-check-certificate`, treat that as an explicit advanced decision and
review the target carefully first.

> [!WARNING]
> Do not make disabled certificate verification a persistent default. Prefer
> repairing Python, certifi, or the local CA chain. If a one-off advanced command
> disables verification, confirm the exact host and avoid sending cookies,
> authorization headers, or signed URLs through that connection.

## Mirror stops before downloading

Symptoms:

- `atlas site` or `atlas dir --adaptive --explain` fails before execution.
- The message says discovered files or estimated bytes exceed the configured
  bound.
- The plan mentions `max-files`, `max-total-size`, or `max-runtime`.

What to do:

```bash
atlas site URL --adaptive --explain --json
atlas site URL --same-host-only --depth 1 --max-total-size 5G --max-runtime 1800
atlas dir URL --same-host-only --depth 1 --max-files 500
atlas dir URL --accept zip,pdf --max-total-size 5G --max-runtime 1800
```

For ordinary recursive mirrors, Atlas rejects `--max-files` because Wget/Wget2
cannot guarantee a hard file-count stop. `--max-total-size` maps to Wget2 quota,
and `--max-runtime` surrounds the mirror process. For a signature-recognized
CopyParty directory index, Atlas builds an exact file list: file count is exact,
all sizes must be known when a total-size bound is requested, and runtime covers
discovery plus transfer. If the scan is too broad, narrow scope with
`--same-host-only`, `--no-parent`, `--depth`,
`--accept`, `--reject`, include/exclude path patterns, or a more specific seed
folder.

Exact-index downloads also refuse truncated indexes, unsupported nested index
formats, unsafe `..`/absolute paths, symlink escapes, and filenames that collide
after case folding. These are safety failures, not retryable backend exits;
narrow or correct the source index rather than forcing the plan.

## yt-dlp extractor errors

Symptoms:

- `EngineError` from a site extractor.
- YouTube or Rumble changes behavior.

Try updating the installed tool:

```bash
atlas update
uv tool install --force git+https://github.com/xkam7ar/atlas.git
```

From a local development checkout, update dependencies or reinstall that
checkout explicitly:

```bash
uv sync --upgrade
uv tool install . --force
```

Use verbose output:

```bash
atlas info URL --verbose
```

## Media post-processing fails after transfer

Symptoms:

- The download bar reached 100%, then the command failed.
- The message mentions `ffmpeg`, merge, metadata, thumbnail, or audio
  extraction.
- The verbose error mentions missing `mutagen` while embedding Opus, Ogg, or
  FLAC artwork.
- The output file may contain the downloaded video, but not the requested final
  audio/container/metadata result.

What to do:

```bash
atlas video URL --progress full --verbose
atlas audio URL --codec best --progress full
```

The guided installer and `uv tool install` include `mutagen`. If an older or
manually assembled environment is missing it, reinstall Atlas and confirm the
`mutagen` check passes in `atlas doctor`.

Atlas treats merge, extract, embed metadata, thumbnail, and finalize as real
phases. A transfer is not successful until those phases finish. If extraction
fails repeatedly, try a more compatible container/codec or keep the original
media with `atlas video URL --container mkv`.

## Cookies

Use cookies only for normal user-authorized access:

```bash
atlas info URL --cookies-from-browser safari
atlas video URL --cookies-from-browser chrome
atlas formats URL --cookies-file ~/Downloads/cookies.txt
```

If browser cookie extraction fails:

- Confirm the browser name is supported by yt-dlp.
- Try a cookies file exported in Netscape format.
- Run with `--verbose` for the underlying yt-dlp details.

Atlas will not steal sessions, extract credentials, bypass DRM, or automate a
browser to defeat bot challenges. Use a normal authorized browser session or an
exported cookies file only when the site's rules allow it.

## Playlist surprises

Safe default:

```bash
atlas video "https://www.youtube.com/watch?v=abc&list=RDabc&start_radio=1"
```

This stays single-video.

Use explicit playlist command only for explicit playlist URLs:

```bash
atlas playlist "https://www.youtube.com/playlist?list=..."
```

If you pass a watch URL to `atlas playlist`, it is refused. This prevents large
accidental downloads from radio/list query parameters.

Channel and tab URLs such as `@name/videos` are collection-only URLs. Use the
video or audio command with explicit playlist intent and a finite bound:

```bash
atlas video "https://www.youtube.com/@name/videos" --playlist --playlist-items 1
```

Atlas applies the same bound during probing so the command cannot silently
enumerate the entire channel before progress starts.

Explicit playlist sessions skip removed, private, or otherwise unavailable
download entries with yt-dlp's download-only ignore mode. Post-processing errors
still fail the run because they can corrupt the requested output.

## Output location

Show config:

```bash
atlas config show
```

Default output:

```text
~/Downloads/atlas
```

Override per command:

```bash
atlas video URL --output-dir ~/Downloads/Clips
```

## Download archive

If a download is skipped unexpectedly, check the archive:

```bash
atlas config show
```

The archive records completed media IDs. If you manually delete a saved file,
yt-dlp can still skip the URL because the ID is still in the archive.

Download it again:

```bash
atlas video URL --overwrite
atlas audio URL --overwrite
```

Disable archive for one command:

```bash
atlas video URL --no-archive
```

## Dry run

Use dry-run to inspect behavior before downloading:

```bash
atlas video URL --dry-run
atlas audio URL --dry-run --json
atlas playlist PLAYLIST_URL --type audio --dry-run
atlas get "https://example.com/archive.zip" --kind file --backend wget2 --dry-run --json
atlas site "https://example.com/docs/" --depth 1 --dry-run
```

Download dry runs do not start real transfers. Direct-file dry runs also skip
the HTTP probe and report probe data as skipped.

For a networked metadata audit, use adaptive explain instead:

```bash
atlas file URL --adaptive --explain --json
atlas batch urls.txt --kind file --adaptive --explain --json
atlas dir URL --adaptive --explain --json
```

`--adaptive --explain` does not download files, but it may perform HEAD/GET
metadata probes so Atlas can classify sizes, ranges, hosts, and crawler safety.
Plans may include classification notes such as `This looked like a page, but
resolved to a ZIP`, `This looked like a file, but returned HTML`, or
`No extension in URL, but Content-Disposition named release.zip`. Site and
directory scans may also warn that a page looks unbounded when pagination,
calendar, search, or tag links make recursion risky. Reduce depth, add reject
rules, or start from a narrower folder before mirroring those pages.
For live automation diagnostics, use `--progress json` rather than `--json`.
`--json` prints the final summary, while `--progress json` emits newline-delimited
progress events with adaptive queue, per-host, segment, bucket, backend,
priority, reclassification, and scheduler-decision fields when known.
When both flags are supplied, `--json` takes precedence and suppresses all live
progress so stdout remains one parseable document.

## Duplicate filenames in batch output

If multiple direct-file URLs share the same basename, Atlas scopes duplicates by
URL path before downloading. For example, two `bestpractices.pdf` URLs may save
as:

```text
academics__bestpractices.pdf
pamphlets__bestpractices.pdf
```

This is intentional. It prevents silent overwrites in flat batch output
directories.

Atlas also reserves case-folded final paths before concurrent work and
re-disambiguates server-provided Content-Disposition/redirect names. This
prevents two URLs from converging on one file on case-insensitive filesystems.

## Recover a failed or canceled session

Atlas writes recovery artifacts for batch, site, and directory sessions under
`<output>/.atlas/latest/`. Inspect first; inspection does not retry or download.

```bash
atlas inspect-session ~/Downloads/atlas
atlas inspect-session ~/Downloads/atlas --panel failed
atlas inspect-session ~/Downloads/atlas --preview errors
```

Choose the narrowest action that matches the inspection:

```bash
atlas retry ~/Downloads/atlas --failed-only
atlas retry ~/Downloads/atlas --checksum-failures-only
atlas retry ~/Downloads/atlas --canceled-only
atlas resume ~/Downloads/atlas
```

`resume` includes failed, skipped-unknown, and canceled items. Canceled items can
come from queued work or from native/media/exact-index/mirror work stopped by an
active control. To review the next retry plans without starting them:

```bash
atlas resume ~/Downloads/atlas --dry-run --json
```

Export URLs when another tool or a manual review is more appropriate:

```bash
atlas export-failed ~/Downloads/atlas --output failed.txt
atlas inspect-session ~/Downloads/atlas --status failed --export-urls filtered.txt
```

If Atlas rejects a linked manifest or output path as untrusted, keep the session
files together under their owning `.atlas` directory. Do not replace them with
symbolic links or edit them to point at unrelated paths.

## Verbose mode

Default errors are concise. Use verbose mode for debugging:

```bash
atlas video URL --verbose
```

Verbose mode prints stack traces and lower-level details.

## Related

- [Quick start](quick-start.md) for a known-good first-run path.
- [Installation](installation.md) for installer, PATH, and runtime footprints.
- [Command reference](commands.md) for exact flags and session selectors.
- [Responsible use](responsible-use.md) for cookies, access, and network policy.
- [Documentation home](README.md) for the complete guide map.
