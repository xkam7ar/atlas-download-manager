# UI and UX guidelines

[Documentation home](README.md) · [System contracts](system-contracts.md) ·
[Development](development.md)

`atlas` should feel like a beautiful terminal control room: calm, elegant,
fast, informative, and obviously intelligent. Backends can be powerful, but the
user experience should be one unified smart session with clean menus, Rich
cards, aesthetic progress bars, adaptive scheduler state, and polished
summaries.

This document owns the human-facing design language. The non-negotiable
progress, JSON, artifact, safety, and verification contracts live in
[System Contracts](system-contracts.md).

Design principle:

```text
Beautiful first.
Smart by default.
Quiet unless useful.
Detailed only when asked.
Every download is a session.
Every session has: detect -> plan -> customize -> run -> summarize.
```

Emotional target:

```text
GitHub CLI polish
+ Homebrew clarity
+ macOS restraint
+ Activity Monitor usefulness
+ download-manager progress beauty
```

<details>
<summary>On this page</summary>

- [Tone](#tone)
- [Visual language](#visual-language)
- [Interactive menu](#interactive-menu)
- [Advanced backend UX](#advanced-backend-ux)
- [Summaries and signature progress screens](#download-summary)
- [Scriptability](#scriptability)
- [Visual rhythm and state](#visual-rhythm)
- [Scheduler and error UI](#scheduler-ui)
- [Terminal size and accessibility](#terminal-size)
- [Aesthetic rules](#aesthetic-rules)

</details>

## Tone

- Calm
- Clear
- Fast
- Trustworthy
- Minimal

Use Rich, but avoid noisy output.

## Visual language

| Meaning | Style |
| --- | --- |
| Primary active work | cyan / blue |
| Success | green |
| Warning | yellow |
| Error | bold red |
| Muted metadata | dim gray |
| Paths | dim italic or dim cyan |
| User choices | bright cyan |
| Disabled items | dim |
| Dangerous options | yellow/red |
| Progress complete | green |
| Progress active | cyan/blue |
| Progress waiting | dim |

Recommended Rich style names:

```python
THEME = {
    "atlas.title": "bold cyan",
    "atlas.subtitle": "dim",
    "atlas.panel": "cyan",
    "atlas.path": "dim italic",
    "atlas.success": "green",
    "atlas.warning": "yellow",
    "atlas.error": "bold red",
    "atlas.muted": "dim",
    "atlas.active": "cyan",
    "atlas.disabled": "dim",
    "atlas.choice": "bright_cyan",
    "atlas.danger": "bold yellow",
    "atlas.progress.complete": "green",
    "atlas.progress.active": "cyan",
    "atlas.progress.waiting": "dim",
    "atlas.progress.file": "green",
    "atlas.progress.media": "cyan",
    "atlas.progress.mirror": "blue",
    "atlas.progress.shimmer": "bold white",
}
```

Use color as an accent, not decoration. Most text should be normal or dim. The
important thing should glow; everything else should breathe.

The implementation lives in `atlas.theme` and is process-wide:

- `--theme auto|dark|light|high-contrast` selects a named palette.
- `NO_COLOR` disables ANSI color while preserving Unicode by default.
- `TERM=dumb` disables color and Unicode glyphs.
- `--plain` disables both color and Unicode.
- `--no-unicode` keeps color but uses ASCII boxes, icons, and bars.
- `--no-animation` keeps color/Unicode but disables shimmer, moving pulses,
  spinners, and activity frames.
- `ATLAS_NO_ANIMATION=1` also disables motion for accessibility or screenshots.

Reduced-motion live surfaces must not run a background refresh loop. They render
once for each real progress or operator event instead.

Renderers should use `themed_console()`, `atlas_box()`, `table_box()`, and
`status_glyph()` instead of direct Rich defaults. This keeps menus, progress,
previews, summaries, and tests on one visual contract. Known-total and
unknown-total bars should use the shared
`semantic_bar_text()` and `semantic_pulse_bar_text()` helpers so shimmer, pulse
width, inactive tails, plain ASCII fallback, and `--no-animation` behavior stay
identical in SmartSessionView dashboards and live progress renderers.
Progress timelines should also compose `Text` spans with semantic styles for
active, complete, waiting, warning, and error states instead of joining markup
strings; this preserves theme behavior inside context cards and compact rows.
Transient notices and empty-state rows should follow the same rule: pass `Text`
objects with semantic styles to Rich instead of printing inline markup strings.
Shared renderables, especially `SmartSessionView`, should attach semantic Atlas
style keys such as `atlas.progress.mirror`, `atlas.warning`, and `atlas.choice`
to `Text`, `Panel`, and `Table` objects instead of resolving them to literal
colors during construction. The console theme should do final color mapping so
light, dark, high-contrast, `NO_COLOR`, and `--plain` stay coherent.
Panel and table titles must be `Text(..., style="atlas.*")` renderables rather
than markup strings such as `[atlas.title]...[/atlas.title]`; this keeps title
colors inspectable in tests and consistent across theme changes.
CLI summaries and setup/update screens must also use semantic keys for paths,
especially `atlas.path`, instead of hard-coded Rich styles such as `dim italic`.
Long-lived or caller-provided consoles must pass through `ensure_atlas_theme()`
before rendering so light, dark, high-contrast, and color-disabled modes refresh
semantic styles and color policy instead of reusing stale visual state.
Interactive prompts must also use the Atlas visual contract: Questionary styles
derive from the selected theme, high-contrast mode uses a strong highlighted row,
plain/NO_COLOR mode uses reverse video instead of subtle color differences, and
select, multi-select, text input, and confirmation prompts all suppress
Questionary's default qmark/instruction chrome. The prompt palette is owned by
`questionary_style_map()` in `atlas.theme`; menu code should only adapt that map
into a Questionary `Style`.

Avoid:

- rainbow output
- excessive spinners
- decorative motion without state meaning
- fake hacker styling
- raw logs by default
- oversized panels

## Interactive menu

Running `atlas` with no arguments in an interactive terminal should feel like a
small native-feeling utility launcher, not a separate product. The menu is the
primary human product surface. Commands remain available for automation, JSON,
CI, repeatable examples, and advanced users, but a normal operator should be
able to download, plan, customize, inspect, retry, export, configure, and
troubleshoot from the menu without learning command syntax.

Primary menu:

```text
╭─ atlas ───────────────────────────────────────────────────────╮
│ Smart downloads for media, files, mirrors, and batches          │
╰────────────────────────────────────────────────────────────────╯

Action

› Paste URL
  Media
  Files
  Batch
  Sessions
  Tools
  Settings
  Quit

↑/↓ move   enter select   type filter   ctrl-c back
```

Rules:

- Ask for minimum input first, usually a URL.
- Keep `Paste URL` first as the fastest path from intent to a reviewed plan.
- Keep the launcher header app-like and useful: one short product sentence.
  Do not repeat output paths, archive state, or other runtime metadata that is
  not needed to choose an action.
- The launcher panel title uses `atlas.title` and the border uses `atlas.panel`;
  do not leave the app shell title as an unstyled string.
- Route through the same hub planner and typed options as command mode.
- Keep the menu as a first-class adapter into internal typed models. Do not make
  menu actions shell out to `atlas` commands.
- Every normal operator command must have a menu capability. Script-only commands
  must be explicit in tests and docs. Raw backend commands are grouped under
  `Tools` -> `Advanced backend`.
- Beyond the `Paste URL` fast path, top-level launcher choices must stay grouped
  and short. Normal operator actions live under submenus so the first screen
  stays calm.
- Main menu and submenu choices must be visible immediately and navigable with
  arrow keys. Do not turn action pickers into autocomplete-only prompts that
  hide the list until the user types.
- Do not let prompt-library default instructions duplicate Atlas's footer. The
  launcher owns the shortcut line, and prompt messages should be short section
  labels such as `Action`, `Batch`, `Directory`, or `Next`.
- Use boxes sparingly. The main launcher shell, important plan previews, errors,
  final summaries, and full progress dashboards can be boxed. Routine context
  such as detected URLs, scan results, directory entries, and setup checks should
  be compact plain text.
- A real interactive launch should check setup before the normal menu. If
  required media tools are missing, or it is a first run with full-runtime tools
  missing, Atlas should show the setup gate first and offer: install recommended
  tools, show install plan, continue with limited features, open Doctor, or
  quit. This gate must never run package-manager commands without explicit
  confirmation.
- The highlighted row must be readable without depending on subtle colors:
  visible pointer, high-contrast highlight, and plain/NO_COLOR reverse-video
  fallback.
- Unicode prompts use `›` for the selected row. Plain or no-unicode prompts use
  `>` so fallback output remains simple and broadly compatible.

Submenus:

```text
Media
› Download video
  Extract audio
  Download playlist
  Show info
  Show formats
  Back

Files
› Download file
  Browse directory
  Mirror website
  Back

Batch
› Paste URL and scan
  Use URL file
  Paste multiple URLs
  Playlist as batch
  Resume session
  Retry failed
  Inspect session
  Export URLs
  Back

Sessions
› Resume session
  Retry failed
  Inspect session
  Export URLs
  Back

Tools
› Doctor
  Setup tools
  Update Atlas
  Advanced backend
  Help
  Back

Settings
› Config
  Back
```

- Menu video/audio defaults should prefer yt-dlp native transfer progress.
  External aria2 media downloads are still available through customization, but
  should not be the default when they prevent useful progress updates.
- Always show a plan preview before starting a download.
- Offer `Start`, `Customize`, `Dry run`, `Back`, and `Quit`.
- For media, probe first and show source-aware profiles before focused edits.
  The default profile screen should list Best quality, Balanced, Apple
  compatible, Small file, Audio only, and Custom formats when those choices are
  available for the source. Conversion or re-encode paths must be labeled and
  confirmed before execution. Media info, format, and recommended-profile tables
  must use Atlas semantic styles rather than raw Rich colors so light and
  high-contrast themes remain readable. Source-derived format summaries and
  recommendation values must be appended as literal `Text`, not markup-parsed
  strings, so remote metadata cannot alter terminal styling.
- Use arrow-key overlays for focused edits after the profile step: quality,
  format, output folder, cookies, subtitle mode, backend selection, and batch
  queue.
- Select prompts may allow type-to-filter, but the full list should be visible
  before typing. Discovered files, folders, manifests, and playlists can still
  use searchable filtering for scale. Search should be forgiving: case-insensitive
  and able to match text in the middle of action, folder, file, playlist, manifest,
  and session labels when the prompt backend supports it.
- Use checkbox-style multi-select for selected playlist items and future
  discovered-file queues. The selected values must map back to typed request
  fields, such as `playlist_items`, instead of bypassing the planner.
- Multi-select prompts should be searchable with middle matching and
  case-insensitive filtering so long playlist, manifest, and discovered-file
  lists feel fzf-like instead of requiring linear arrow-key scanning.
- Help is contextual. Never advertise an action that the current surface cannot
  perform. `Tools` -> `Help` describes menu prompts; the full-progress overlay
  describes dashboard navigation and only shows mutation controls when an
  operator controller is available.

  Menu prompts:

  | Key | Action |
  | --- | --- |
  | `up/down` or `↑/↓` | Move selection |
  | Type | Filter a searchable list |
  | `space` | Toggle an item in a multi-select prompt |
  | `enter` | Select or confirm |
  | `ctrl-c` | Go back or cancel the prompt |

  Full-progress view controls:

  | Key | Action |
  | --- | --- |
  | `up/down` or `↑/↓` | Move the focused row |
  | `tab` | Cycle queue, active, completed, failed, scheduler, logs, and summary |
  | `?` | Show or hide contextual help |

  Full-progress mutation controls, shown only when supported:

  | Key | Action |
  | --- | --- |
  | `g` | Pause or resume new queue starts |
  | `h` | Pause or resume the focused host |
  | `s` | Pause or resume the focused queued item |
  | `x` | Cancel the focused queued or active item |
  | `X` | Cancel all controlled work |

  Retry, resume, inspect, export, open, and copy actions belong to explicit menu
  choices or commands. They are not universal key bindings.
- `--progress full` batch dashboards should show a one-line shortcut hint for
  active controls. Compact mode keeps the hint hidden. In interactive
  full-progress batch sessions, Atlas reads view keys in the background and
  maintains the active panel, focused row, and `?` overlay. Mutation keys are
  routed through `BatchOperatorController` only when one is bound; shared aria2
  progress and other read-only surfaces must hide them. JSON, NDJSON,
  `--progress none`, compact mode, non-terminal output, and automation paths
  must not start a key reader.
- The focused row must be visible without color, using a text marker in both
  full tables and narrow-terminal compact rows. Panel cycling should move
  between queue, active, completed, failed, scheduler, logs, and summary views
  without corrupting the live table layout.
- After completion, offer Finder/open actions when a saved path is known.
- Do not auto-open in non-TTY sessions, JSON mode, or automation.

The forced `atlas menu` command may be used when a user wants the launcher
explicitly, but it still requires an interactive terminal.

Customize should feel like small modal overlays with short labels:

| Overlay | Controls |
| --- | --- |
| Quality | quality intent, resolution, audio quality |
| Format | container, codec, filename/mirror depth |
| Output | output directory |
| Cookies | no cookies, browser cookies, or cookies file |
| Subtitles | mode, language, embed choice |
| Backend | file: native/aria2/wget2; site/dir: wget2/wget |
| Scope | same host, same domain + www, include subdomains, no parent, depth |
| HTML | site/offline copy: keep HTML, convert links, adjust extension, page requisites; directory: keep or reject HTML/index pages |
| Scheduler | adaptive mode, lane caps, per-host cap, global connections |
| Batch | source, kind, concurrency, adaptive mode, site/directory allowance |
| Setup | full/minimal/media/mirror runtime mode, install/no-install, doctor verification |
| Config | show resolved config, show config path, open config file for editing |

After a customization overlay changes values, Atlas should show a compact
changed-options diff before rebuilding the next plan or dry-run. The diff should
use explicit text such as `old -> new`, not color alone, and should avoid raw
backend flags unless the user opens the advanced backend preview.

Bat-style preview panes for manifests, dry-run plans, configs, logs, and error
reports must use the shared Atlas theme. Dark, auto, and high-contrast palettes
use a dark syntax theme; light mode uses a light syntax theme so highlighted
JSON, TOML, and logs remain readable.

Every menu branch should present the same smart-session shape:

```text
Input
  -> probe / scan / inspect
  -> classify
  -> build manifest
  -> recommend plan
  -> Start / Customize / Dry run / Back / Quit
  -> progress
  -> summary / retry manifest when applicable
```

Video, audio, playlist, file, site mirror, directory mirror, and batch differ by
their customization overlay and scheduler preset, not by having separate
planning concepts. Explicit playlist URLs become a `media_playlist` session;
ordinary watch URLs with playlist/radio parameters still remain single media
downloads unless the user deliberately chooses playlist mode.

Batch sessions should start with a source choice instead of assuming a text file:

```text
Batch
Build a smart queue from URLs, a file, a playlist, or a scanned site.

› Paste URL and scan
  Use URL file
  Paste multiple URLs
  Playlist as batch
  Resume session
  Retry failed
  Inspect session
  Export URLs
  Back
  Quit
```

For `Paste URL and scan`, Atlas performs a bounded scan and shows a compact
summary with seed, scan type, host, link count, same-host folders, HTML pages,
files, media, skipped external links, rough estimated size, recommendation, and
any scan note. If the URL looks like an open directory index, do not stack a
generic `Scan complete` summary above the browser. Atlas should switch directly
into the Directory Explorer first instead of asking the user to understand
site/dir/batch internals:

```text
╭─ Browse Directory ───────────────────────────────────────────────╮
│ Seed       https://example.com/files/                            │
│ Scope      same host · no parent                                 │
│ Visible    9 folders · 1,618 files                               │
│ Estimated  ~21.9 GB                                              │
╰──────────────────────────────────────────────────────────────────╯

Folders (9)
  cours/           2023-12-23
  images/          2025-06-26
  musique/         2024-08-22

Files at this level (1,618)
  readme.txt
  index.pdf
  ...
  showing first 8 of 1,618; use / to filter

Warnings
  • Parent directory links skipped (no-parent policy)
  • URL-encoded or spaced filenames detected

What would you like to do?
› Everything under this folder
  Choose one specific folder
  Choose multiple folders
  Only visible files at this level
  Browse full folder tree first
  Deep scan selected folders first
  Treat as offline website instead
  Back
  Quit
```

This is a two-stage scan. Stage 1 is a fast visible root map: fetch the seed
page, parse common directory-index rows, skip Parent Directory by default,
normalize same-host URLs, and avoid downloading file bodies. Stage 2 starts only
after the user chooses a scope. Atlas scans the selected roots, shows a deep
scan summary, and then either builds an exact-list adaptive batch or queues the
explicit directory roots with directory mirroring enabled.

The root-level file list on this first screen is preview-only. The full root
file set is exposed through `Only visible files at this level`, which opens a
searchable picker with the full visible file list instead of dumping hundreds or
thousands of file rows into the main browse screen.

For non-directory HTML scans, show compact context and ask what kind of session
to build:

```text
Detected URL
textfiles.com/directory.html · same host · scan first

Scan complete
textfiles.com/directory.html

Found     1,842 links
Files     1,219
Folders   87
HTML      132
Skipped   491 external

Actions
› Download discovered files
  Choose discovered files
  Offline website
  Recursive mirror
  Choose folder
  This page only
  Back
```

`Download discovered files` and `Choose discovered files` appear only when
the scan has accepted downloadable file URLs. A transport failure is not an
empty successful scan: Atlas renders a `Scan failed` error panel with `Retry scan`,
`Doctor check`, `Backend fetch`, `Continue as mirror`, `Error details`, and
`Back`.
If fetching succeeds but no links are discovered, Atlas renders `No links found`
with only `Retry scan`, `Treat as website`, `This page only`, and `Back`.
Discovered-file actions must never appear in `failed` or `empty` scan states.

The recursive and offline-site paths should reuse the same site/directory
customization overlays as command mode: scope presets, depth, no-parent,
domains, keep HTML, convert links, adjust extension, page requisites, bounds,
wait, random wait, timeout, tries, continue partial files, overwrite, backend,
and adaptive controls.
The downloadable-file-queue paths write a generated batch file under
`<output>/.atlas/menu/` with same-host file/media/manifest links, skipping HTML
pages, directories, and external links by default. `Download discovered files`
writes every accepted file link when accepted file links exist. `Choose
discovered files` opens a checkbox-style multi-select over accepted discovered
files and writes only the selected URLs. Both generated queues then use the
normal batch plan preview, customization, dry-run, and execution flow.

Scan progress should look alive but quiet:

```text
╭─ Scanning ──────────────────────────────────────────────────────────╮
│ Seed        http://textfiles.com/directory.html                      │
│ Boundary    same host · no parent · depth 2                          │
│ Policy      wait 0.5s · random wait · timeout 60s · tries 5           │
╰──────────────────────────────────────────────────────────────────────╯
Discovery     ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░  scanning links
Depth         ████████████░░░░░░░░░░░░  1 / 2
Accepted      █████████████████░░░░░░░  1,219 files
Rejected      ████████░░░░░░░░░░░░░░░░  491 external/filtered
Found         1,842 links
Folders       87
HTML pages    132
Files         1,219
Estimated     14.8 GB
```

Plan preview should show the useful policy and execution details, with raw
backend flags hidden behind an advanced view. Avoid redundant fields such as
`Intent` when the screen title, selected command, or session type already says
what the user is doing.

```text
╭─ atlas Smart Mirror Plan ───────────────────────────────────────────╮
│ Seed        http://textfiles.com/directory.html                      │
│ Mode        recursive directory mirror                               │
│ Output      ~/Downloads/atlas/textfiles.com                          │
│ Backend     wget2 discovery · atlas adaptive scheduler                │
│ Archive     enabled                                                  │
╰──────────────────────────────────────────────────────────────────────╯
Scope
  Recursive          yes
  Start from         directory.html
  Depth              2
  No parent          yes
  Domains            textfiles.com, www.textfiles.com
  Span hosts         no
HTML
  Keep HTML          yes
  Convert links      yes
  Adjust extension   yes
  Page requisites    yes
Network
  Wait               0.5s
  Random wait        yes
  Timeout            60s
  Tries              5
  Continue partials  yes
Scheduler
  Mode               adaptive
  Small files        high concurrency
  Large files        lower concurrency + segmented transfer
  Per-host cap       dynamic, max 6
  Global cap         dynamic, max 96 connections
Safety
  ✓ same host boundary
  ✓ no parent traversal
  ✓ bounded recursion
  ✓ resumable downloads
  ○ no checksum manifest found
Actions
  [Start]  [Customize]  [Dry run]  [Save manifest]  [Back]  [Quit]
```

## Advanced backend UX

atlas should support complete backend flag coverage without making raw flags the
default experience. The advanced pass-through commands are:

```bash
atlas ytdlp -- [yt-dlp args...]
atlas aria2 -- [aria2c args...]
atlas wget2 -- [wget2 args...]
atlas wget -- [wget args...]
```

UX rules:

- Show an `Advanced Backend` plan panel before execution.
- Make it clear this is raw pass-through, not an intent plan.
- Preserve backend argv exactly after `--`.
- Never use `shell=True`.
- Support `--dry-run` and `--json`.
- Keep missing backend errors friendly and actionable.
- Prefer atlas intent commands in examples unless raw backend coverage is the point.

## Download summary

Before a real media download, show one card with the essentials and keep
backend details out of the default view:

```text
╭─ Media › Download video ────────────────────────────────────────────╮
│ Title      Example Interview                                         │
│ Source     YouTube · Channel                                         │
│ Quality    Best quality · 1080p · AV1 + Opus                         │
│ Container  MKV                                                       │
│ Output     ~/Downloads/atlas/Channel                                 │
╰──────────────────────────────────────────────────────────────────────╯

Options
  Metadata   on
  Thumbnail  on
  Archive    on
  Playlist   single item only

Next
  › Start
    Customize
    Choose exact format
    Dry run
    Back
    Quit
```

For bounded channel/tab collections, the output row must show the resolved
yt-dlp template rather than a fabricated concrete path based on collection-level
title/id metadata. The selected-item card should summarize the chosen quality,
container, and codecs without dumping the full format catalog; the exact-format
picker and `atlas formats` own that detailed table.

## Audio summary

Audio summary focuses on extraction:

```text
Audio Extraction
Title     Example Interview
Source    YouTube
Codec     best
Format    bestaudio/best
Metadata  enabled
Artwork   enabled
Output    ~/Downloads/atlas/...audio
Engine    aria2c auto
```

## Signature progress screens

Single media downloads use one primary card for durable context, then compact
rows for live state. Byte transfer completion is not success until
post-processing and finalize finish:

```text
╭─ Downloading ───────────────────────────────────────────────────────╮
│ Title      Example Interview                                         │
│ Source     YouTube · Channel                                         │
│ Quality    1080p · H.264 · MP4                                       │
│ Output     ~/Downloads/atlas                                         │
╰──────────────────────────────────────────────────────────────────────╯

Download     ████████████████████▓▓░░  84%   107.0 MB / 126.8 MB
Speed        9.1 MB/s                              ETA 00:01

Steps
  ▸ Download video          84%
  ○ Merge video/audio
  ○ Embed metadata
  ○ Add thumbnail
  ○ Finalize

Elapsed      00:16
```

When transfer finishes:

```text
Steps
  ✓ Download video          100%
  ▸ Merge video/audio
  ○ Embed metadata
  ○ Add thumbnail
  ○ Finalize
```

Audio extraction should focus on download, extract, metadata, artwork, and
finalize:

```text
╭─ Downloading ───────────────────────────────────────────────────────╮
│ Title      Longform Interview                                        │
│ Source     YouTube · Channel                                         │
│ Quality    Opus · WebM                                               │
│ Output     ~/Downloads/atlas                                         │
╰──────────────────────────────────────────────────────────────────────╯

Download     ███████████████████░░░░░  76%   742 MB / 980 MB
Speed        16.2 MB/s                             ETA 00:14

Steps
  ▸ Download audio          76%
  ○ Embed metadata
  ○ Add artwork
  Finalize
```

Explicit playlists are media batches. In the interactive menu, if video/audio
receives an explicit playlist URL, ask before converting the run into a playlist
session; non-interactive video/audio commands instead require explicit playlist
intent. Watch URLs with playlist/radio parameters always remain single-item
media downloads. Playlist work requires a canonical explicit playlist URL.

```text
╭─ atlas Audio Playlist ──────────────────────────────────────────────╮
│ Playlist    Example Archive                                          │
│ Items       84 total · 2 active · 12 done · 0 failed                  │
│ Codec       best · metadata on · artwork embedded                    │
│ Output      ~/Downloads/atlas/Example Archive                         │
╰──────────────────────────────────────────────────────────────────────╯
Overall       ███████░░░░░░░░░░░░░░░░░  14%   12 / 84
Transfer      ███████████░░░░░░░░░░░░░  45%   4.8 GB / 10.7 GB
Postprocess   ███░░░░░░░░░░░░░░░░░░░░░  11%   extracting audio
Speed         22.4 MB/s                       ETA 00:38:12
Active        2 downloads · 1 ffmpeg job · 0 retries
Scheduler     media lane stable · CPU normal
Safety        archive on · playlist explicit · cookies off
```

Direct files should show the chosen engine and resume/verify state:

```text
╭─ atlas File ────────────────────────────────────────────────────────╮
│ File       dataset-part-04.tar.zst                                   │
│ Size       18.0 GB                                                   │
│ Engine     aria2c · 16 connections                                   │
│ Resume     enabled                                                   │
│ Output     ~/Downloads/atlas/dataset-part-04.tar.zst                  │
╰──────────────────────────────────────────────────────────────────────╯
Download      ███████████░░░░░░░░░░░░░  44%   7.9 GB / 18.0 GB
Connections   ████████████████░░░░░░░░  16 active
Speed         62.0 MB/s                       ETA 03:12
Verify        waiting
Finalize      waiting
```

Smart mirror progress is the signature atlas experience:

```text
╭─ atlas Smart Mirror ────────────────────────────────────────────────╮
│ Seed        http://textfiles.com/directory.html                      │
│ Mode        recursive directory mirror                               │
│ Boundary    same host · no parent · depth 2                          │
│ Output      ~/Downloads/atlas/textfiles.com                          │
│ Backend     wget2 discovery · adaptive scheduler                     │
╰──────────────────────────────────────────────────────────────────────╯
Discovery     ███████████████████░░░░░  78%   1,842 links found
Download      ████████████░░░░░░░░░░░░  51%   622 / 1,219 files
Transfer      █████████░░░░░░░░░░░░░░░  39%   5.8 GB / 14.8 GB
Speed         42.6 MB/s                       ETA 00:12:31
Active        24 jobs · 38 connections · 3 retries
Scheduler     small-file lane increased 16 -> 24
Safety        no-parent · same-host · random-wait · continue on
```

Mixed batches should keep one dashboard and one active table, never one backend
log per item:

```text
╭─ atlas Batch ───────────────────────────────────────────────────────╮
│ Queue       1,284 items · 27 active · 821 done · 1 failed             │
│ Backends    yt-dlp · aria2c · native · wget2                          │
│ Scheduler   adaptive · jobs 27/40 · connections 74/96                 │
│ Output      ~/Downloads/atlas                                         │
╰──────────────────────────────────────────────────────────────────────╯
Overall       ████████████████░░░░░░░░  64%   821 / 1,284
Transfer      ███████████████████░░░░░  78%   412.8 GB / 529.1 GB
Files         ███████████████████░░░░░  79%
Media         ███████████░░░░░░░░░░░░░  48%
Mirrors       ███████░░░░░░░░░░░░░░░░░  31%
Speed         186.4 MB/s                      ETA 01:42:18
Scheduler     tiny lane increased 24 -> 32
Safety        archive on · dirs bounded · sites skipped
```

## Info

Default `info` output is a card, not raw JSON.

Use `--json` for machine-readable output.

## Formats

`formats` should use a readable table and a single recommendation line.

Recommended rows may be highlighted, but the table should remain calm and
scannable.

## Doctor

Doctor output is a setup checklist:

```text
atlas doctor

✓ Python
✓ atlas
✓ yt-dlp
✓ ffmpeg
✓ ffprobe
○ aria2c
○ wget2
○ wget

Status: ready
```

Setup output should use the same calm card/table language:

```text
╭─ atlas Setup ───────────────────────────────────────────────────────╮
│ Mode       full                                                      │
│ OS         macOS arm64                                               │
│ Package    homebrew                                                  │
│ Config     ~/Library/Application Support/atlas/config.toml            │
│ Output     ~/Downloads/atlas                                         │
╰──────────────────────────────────────────────────────────────────────╯
Runtime tools
✓ ffmpeg    media download post-processing
✓ ffprobe   media metadata probing
○ aria2c    segmented file and batch downloads
○ wget2     website and open-directory mirroring
○ wget      mirror fallback backend
```

`atlas update` should show the detected install method and either one safe
command or an explicit blocked reason. Unknown
install methods should be explanatory, not alarming.

Required missing dependencies should include an install hint.

Doctor, setup, certificate repair, and update screens must use Atlas semantic
styles rather than raw Rich colors so first-run and recovery surfaces honor
light, dark, high-contrast, plain, and `NO_COLOR` modes.

Setup and update must also be reachable from the interactive menu. The menu
should open the same setup plan, doctor verification, and update detection paths
as the CLI commands instead of shelling out to a separate wrapper.

## Scriptability

Human output is default. Machine output is opt-in:

```bash
atlas info URL --json
atlas formats URL --json
atlas batch urls.txt --json
atlas doctor --json
```

`--quiet` should reduce human output and must not hide nonzero failures.

Progress output is controlled by:

```text
--progress auto|compact|full|json|none
```

Mode behavior:

| Mode | UX |
| --- | --- |
| `auto` | Rich progress on an interactive terminal; no live progress mixed into JSON. |
| `compact` | Atlas card plus a small stack of colored semantic bars; batch adds a calm active table. |
| `full` | Adds scheduler decisions and row diagnostics without adding backend noise. |
| `json` | Newline-delimited `ProgressEvent` JSON for tooling. |
| `none` | No live progress; errors and final summaries still show unless quiet. |

Single media/file downloads should start with a compact card: title, source,
quality or mode, engine, output, and safety badges. Below that, show separate
semantic rows for `Download`, `Fragments` when available, `Speed`, `Phase`, and
`Next`. A byte-transfer row may reach done while `Merging`, `Extracting`,
`Postprocess`, or `Finalizing` is still running. In that state, switch from the
normal transfer stack to phase rows such as `Merge`, `Extract`, `Metadata`,
`Thumbnail`, and `Finalize`; do not print final success until
postprocessor/finalize events have completed.

Batch progress should show one calm Live table, not one noisy block per backend.
Batch compact mode should render, in order:

- an `atlas` card with `Batch Download`, output, mode, backends, and safety
- a dashboard card with done/active/queued/failed counts, total speed, scheduler
  summary, and the latest scheduler decision
- stacked bars for `Overall`, `Transfer`, `Files`, `Media`, `Mirrors`, and
  `Failures`
- one active table with `Line`, `Kind`, `Name`, `Size`, `Progress`, `Speed`,
  `ETA`, and `Engine`

Avoid logging every fragment or postprocessor event; users need current state
plus the final succeeded/failed/skipped summary. Rows should preserve the routed
kind and engine through final done/error events. If the backend reports a percent
without bytes, show a percent bar; if it reports bytes and totals, derive the bar
from those values. Unknown totals must never fake a percent; show a pulsing
indeterminate bar with bytes downloaded, speed, and state instead.

## Visual rhythm

Human progress modes should use layered bars with distinct meaning:

- mode
- backend
- output directory
- archive on/off when relevant
- queue count
- adaptive queue/per-host/segment notes when relevant
- adaptive bucket/backend/priority/scheduler-decision details in batch full mode
- total active speed
- elapsed time
- safety badges such as `single video`, `playlist disabled`, `cookies active`,
  `aria2`, `checksum`, `adaptive normal`, `per-host 2`, `segments 3`,
  `sites skipped`, or `dirs skipped`

Use color-coded filled bars for known ratios and a pulse for unknown totals:

```text
Overall      cyan     ████████████████░░░░░░░░  whole job
Transfer     green    ███████████████████░░░░░  bytes downloaded
Files        green    ███████████████████░░░░░  direct-file lane
Media        magenta  ███████████░░░░░░░░░░░░░  media lane
Mirrors      blue     ███████░░░░░░░░░░░░░░░░░  recursive lane
Postprocess  cyan     ███░░░░░░░░░░░░░░░░░░░░░  ffmpeg/metadata work
Verify       yellow   ██████░░░░░░░░░░░░░░░░░░  checksum/size validation
Backoff      yellow   ▒▒▒▒▒▒▒▒▒▒▒▒              retry wait / paused host
Failures     red      ██░░░░░░░░░░░░░░░░░░░░░░  failures/backoff
Discovering  cyan     ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓          unknown total / scan
```

Shared dashboards and live progress use the same bar grammar. Active known-total
bars may shimmer inside the filled segment. Unknown totals use a moving pulse in
the metric's semantic color, with the inactive pulse tail styled as
`atlas.progress.waiting` so unknown streaming and retry states still have
readable active/waiting contrast. Tiny nonzero warning/error ratios should render
as `<1%` with a one-cell warning/error sliver instead of pretending the count is
zero.

Motion should stay useful and restrained:

- Single-download and file progress may show a small spinner next to the active
  phase when motion is enabled.
- Known-total bars may use a subtle shimmer in the filled portion while active;
  finished bars should settle into stable blocks.
- Batch rows may show a tiny activity marker for running items and a yellow
  pulse for retries.
- Full semantic progress rows and compact active rows must use the same state
  colors: retry and backoff stay yellow even when the item also belongs to a
  file, media, or mirror lane.
- `--no-animation`, `--plain`, and `TERM=dumb` must replace moving shimmer/pulse
  effects with stable bars and suppress transient spinners/activity frames while
  preserving the same labels and semantic color where color is enabled.
- Menu planning may use a transient status spinner in interactive terminals when
  motion is enabled.
- Completed, skipped, and error states should settle into stable text.
- Internal event processing may run up to 10 Hz, but terminal render should stay
  at 4 Hz maximum.
- Interactive Rich live progress may use the terminal alternate screen; scripts,
  JSON/NDJSON, `--progress none`, and `--plain` stay inline/plain.
- Verbose DEBUG logs should go to the Atlas log file instead of the terminal
  stream, so live Rich surfaces do not get torn by diagnostic output.
- Scheduler decision messages should update at about 1 Hz.
- Repeated warnings from the same host should be rate-limited heavily.

Avoid:

- multiple spinners per row
- rapid flicker
- scrolling logs
- constantly changing layout
- large animated banners
- raw fragment lines

The phase line is driven by normalized `ProgressEvent` values:

```text
Probing > Inspecting metadata > Downloading > Merging > Extracting > Embedding metadata > Embedding thumbnail > Finalizing
```

The UI may emit local probe/extract phase markers around preflight work, but
backend-specific download, merge, postprocess, verify, finalize, done, and error
states must still come from the normalized progress event path.

Mirror errors should be concise but evidence-bearing. For Wget2 partial mirrors,
the user should see transferred bytes and failed URL samples when structured
stats are available. A bare backend exit code is not enough for the default UI.

## State model

Every item should have one clear state, and every state should map to a visual:

| State | Visual |
| --- | --- |
| `planned` / `queued` | dim |
| `probing` / `resolving` / `scanning` | cyan pulse |
| `downloading` | cyan progress |
| `mirroring` | blue/cyan progress |
| `extracting` / `merging` | cyan progress |
| `embedding_metadata` / `embedding_thumbnail` | cyan phase row |
| `verifying` | yellow/cyan progress |
| `finalizing` | cyan phase row |
| `done` | green |
| `skipped` | dim yellow |
| `retrying` / `backoff` | yellow pulse |
| `paused` | dim yellow |
| `failed` | red |
| `canceled` | dim red |

The UI should always show phase because normalized progress events are the
contract across yt-dlp, native, aria2c, wget2, and wget. Phase labels must use
the same semantic state colors as the row: `Done` is green, `Error` is red, and
retry/backoff phases are yellow even when the underlying item is a file, media,
or mirror.

Operator cancellation is a visible state, not a hidden failure. Queued and
active-controlled cancellation should render `canceled`, include the URL in the
failure/state drawer only when that drawer is selected, write it to
`latest/canceled.txt`, and make it available to `atlas resume` and
`atlas retry --canceled-only`. Subprocess-backed mirrors stop their child and
avoid raw shutdown logs. Native files, exact-index work, and media stop
cooperatively at transfer, file, or progress/postprocessor hook boundaries.
Batch rows expose one `ProcessControl` through `BatchItemContext`, so the same
line-level action covers every controlled route while keeping the wording
operator-focused.

## Scheduler UI

The adaptive scheduler should be visible as a calm intelligence layer.

Scheduler text should be evidence-driven. Prefer decisions that name the scope,
old cap, new cap, and reason:

```text
Scheduler     decrease host:textfiles.com 6 -> 3; 503 responses
Scheduler     increase host:archive.org 3 -> 4; stable speed and low errors
Scheduler     hold host:example.com at 2; backoff hold active
```

Do not imply that Atlas is guessing. Full mode can show the supporting evidence:
EWMA speed, active connections, retry/error counts, disk pressure, CPU pressure,
postprocess backlog, and the active total-connection budget.

Compact mode:

```text
Scheduler     small-file lane increased 16 -> 24
```

Full mode:

```text
╭─ Scheduler ─────────────────────────────────────────────────────────╮
│ Global       jobs 27/40 · connections 74/96 · disk 91 MB/s           │
│ Tiny lane    24 active · 1 conn each · stable                         │
│ Large lane   2 active · 16 conns each · stable                        │
│ Media lane   2 active · 1 ffmpeg job                                  │
│ Host cap     textfiles.com 4/6 · healthy                              │
│ Decision     increased tiny lane; speed stable and errors low         │
╰──────────────────────────────────────────────────────────────────────╯
```

Backoff and pressure examples:

```text
Scheduler     reduced textfiles.com 6 -> 3 connections after 503 responses
Scheduler     reduced large-file lane 3 -> 1 because disk write pressure is high
```

## Error UI

Errors should be concise, beautiful, and actionable:

```text
╭─ Download Failed ───────────────────────────────────────────────────╮
│ Line       93                                                        │
│ URL        https://example.org/file.zip                              │
│ Reason     checksum mismatch                                         │
│ Expected   sha256:ab12...                                            │
│ Actual     sha256:91fe...                                            │
╰──────────────────────────────────────────────────────────────────────╯
What you can do:
  Retry failed only     atlas retry batch-2026-06-09.atlas.json
  Keep partial file     ~/Downloads/atlas/.partials/file.zip.part
  Show details          rerun with --verbose
```

Generic command errors, recovery hints, dry-run labels, and status lines must
use Atlas semantic styles (`atlas.error`, `atlas.warning`, `atlas.muted`,
`atlas.success`) rather than raw Rich color names. High-contrast mode should
upgrade those labels to bright/bold variants automatically, and `NO_COLOR` or
`--plain` must preserve the text labels without ANSI color.

Batch result tables, saved-session action notices, artifact panels, and final
summary counts follow the same rule. Success counts use `atlas.success`, failure
counts use `atlas.error`, skipped/canceled counts use `atlas.warning`, labels
use `atlas.muted`, and panel titles/borders use `atlas.title` / `atlas.panel`
from the selected palette instead of hard-coded `green`, `red`, `yellow`,
`cyan`, or `dim`.

Batch failures should roll into a failure drawer, not destroy the whole layout:

```text
Failures      4
Line 93       checksum mismatch
Line 211      404 not found
Line 877      extractor error
Line 901      depth limit reached with errors
```

## Final summary

The final screen should feel satisfying and useful:

```text
╭─ Complete ──────────────────────────────────────────────────────────╮
│ Session     textfiles-mirror-2026-06-09                              │
│ Output      ~/Downloads/atlas/textfiles.com                          │
│ Elapsed     02:14:33                                                 │
╰──────────────────────────────────────────────────────────────────────╯
Succeeded     1,251
Failed        4
Skipped       29
Downloaded    412.8 GB
Average speed 52.1 MB/s
Archive       updated
Manifest      saved
Retry file    saved
Next actions
  Open folder
  Retry failed
  Show summary JSON
  Copy command / backend command
  Quit
```

Single media completion:

```text
╭─ Download Complete ─────────────────────────────────────────────────╮
│ ✓ Saved  ~/Downloads/atlas/Channel                                   │
│          2025-03-09 - Example Interview [abc123].mp4                 │
╰──────────────────────────────────────────────────────────────────────╯

Details
  Size      133.5 MB
  Format    MP4
  Video     H.264
  Metadata  embedded
  Thumbnail embedded
  Archive   updated

Next
  Reveal in desktop file manager
  Open file
  Download another video
  Back to menu
  Quit
```

## Terminal size

The UI should adapt to both terminal width and height:

- At 110 columns or wider, live batches use the full active table.
- Below 110 columns, live batches use compact rows.
- Below 64 columns, each live item becomes a stacked row with identity,
  progress, and detail lines. Speed and ETA remain visible even at 40 columns.
- Short terminals budget visible rows instead of overflowing. Selection priority
  is the focused row, active work, retry/backoff warnings, failures, then the
  most recently updated remaining rows.
- Omitted rows are summarized as `+N hidden (...)` with state counts, so height
  reduction does not hide queue health.
- Narrow completed batches use stacked result cards that preserve status,
  kind/engine, URL, and outcome text below 72 columns.

If a table is too wide, collapse columns in this order:

1. host
2. threads
3. engine
4. size
5. path
6. title

Always preserve status, progress, speed, ETA, and failure count during live
work. Final batch results must always preserve status, engine, and URL, even
when that requires stacked rows instead of a table.

## Accessibility

Beauty must not depend only on color. Use text and symbols too:

- `✓` success
- `!` warning
- `x` error
- `○` optional/missing
- `->` transition

Also provide text labels: `done`, `warning`, `failed`, `retrying`, and
`paused`.

Design and implementation should preserve or add terminal and automation
fallbacks:

- `NO_COLOR`
- `TERM=dumb`
- `--plain`
- `--no-unicode`
- `--no-animation`
- `--progress none`

Plain fallback progress bar:

```text
[############--------] 64%
```

Plain mode should also use ASCII panel/table borders and explicit text statuses:
`done`, `failed`, `retrying`, `paused`, `skipped`, and `waiting`. JSON and NDJSON
progress modes must never include human UI, ANSI color, alternate-screen escape
sequences, or explanatory prose.

Every server-, filename-, metadata-, and backend-derived string must be treated
as untrusted terminal input. Before rendering, Atlas strips ANSI CSI/OSC
sequences, C0/C1 controls, and bidirectional override/isolate controls. Rich
escaping alone is not a terminal-injection boundary. `--no-unicode` also uses
ASCII spinner/bar frames, not merely ASCII labels around Unicode animation.

Plain or `--no-unicode` shortcut overlays must replace glyph keys such as
`↑/↓` with text such as `up/down`; the action label and status text must still
be present so the overlay is usable without color or Unicode.

## Aesthetic rules

Use these rules everywhere:

- One header panel maximum per screen.
- One focused prompt at a time.
- Progress bars stacked by meaning.
- Dim metadata, bright active state.
- No backend logs unless verbose.
- No raw flags unless advanced view.
- No fake percentages.
- No success until finalize is done.
- No table wider than the terminal.
- No repeated warning spam.

## Related

- [Command reference](commands.md)
- [Configuration](configuration.md)
- [System contracts](system-contracts.md)
- [Architecture](architecture.md)
