# Smart Download Sessions

`atlas` should feel like one adaptive download system, not separate wrappers for
yt-dlp, aria2c, native HTTP, Wget2, and Wget. The shared contract is
`SmartDownloadSession`. The normative boundary and artifact rules live in
[System Contracts](system-contracts.md).

Every user-facing mode follows the same shape:

```text
input
  -> probe / scan / inspect
  -> classify
  -> build manifest
  -> recommend plan
  -> customize typed options
  -> execute through the selected backend
  -> report normalized progress
  -> save summary / manifest / retry data when applicable
```

The backend changes by intent, but the session object stays consistent.
In the menu, that means Atlas probes or scans first, then offers only valid
next actions for the detected source instead of asking the user to guess.

## Session Fields

`SmartDownloadSession` records:

| Field | Meaning |
| --- | --- |
| `source` | The URL or batch source that started the session. |
| `detected_kind` | The routed `HubKind`, such as `video`, `audio`, `file`, `site`, or `dir`. |
| `intent` | The user-level goal, such as `video`, `audio`, `directory_mirror`, or `batch_file`. |
| `session_type` | Stable preset label, such as `single_video`, `media_playlist`, or `batch_session`. |
| `manifest` | Work items known before execution. Some modes have one item; scans can add many. |
| `plan` | The adaptive or preset `AdaptiveDownloadPlan` that describes scheduler policy. |
| `customization` | User-facing option state, such as codec, depth, retry, cookies, and output choices. |
| `scheduler_policy` | JSON-friendly summary of queue, host, connection, segment, and postprocess budgets. |
| `progress_reporter` | The normalized progress surface expected for the mode. |
| `final_summary` | The result/artifact contract expected after completion. |

## Mode Presets

| User mode | Session type | Backend owner | Scheduler shape |
| --- | --- | --- | --- |
| Download video | `single_video` | yt-dlp | One media job with separate transfer/postprocess phases. |
| Extract audio | `single_audio` | yt-dlp + ffmpeg | One media job with audio extraction postprocessing. |
| Explicit playlist | `media_playlist` | yt-dlp + ffmpeg | Media queue lane plus bounded postprocessor budget. |
| Direct file | `direct_file` | native, aria2c, or wget2 | One file item; adaptive mode can pick queue/segment policy. |
| Website mirror | `site_session` | Wget2 or Wget | Recursive mirror policy with explicit scope and politeness. |
| Directory mirror | `directory_session` | native exact-index or Wget2/Wget | Exact same-host file list for complete CopyParty indexes; otherwise recursive no-parent/same-host policy. |
| Batch | `batch_session` | Mixed adapters | Queue-level scheduler with per-item backend plans. |

The interactive Directory Explorer is a first-class directory preset on top of
the same envelope. Its root-map stage records visible folder/file rows, then the
selected roots become either a `directory_session` for recursive mirror policy
or a generated `batch_session` when Atlas has exact file URLs from the selected
scope. The menu owns folder selection; typed options, adaptive planning,
progress, artifacts, and retry files stay on the normal session path.

Media sessions have the same early-resolution rule. Before a menu user chooses
quality/container/codec details, Atlas probes the source formats, builds a
`MediaCapabilityCatalog`, and recommends profiles such as Best quality,
Balanced, Apple compatible, Small file, Audio only, or Custom formats. That
capability step is part of the session, not a side path.

Ordinary watch URLs with playlist or radio query parameters remain single-media
sessions by default. An explicit playlist session is deliberate: use
`atlas playlist`, `--playlist`, or an interactive playlist choice.
YouTube channel/tab collections require `--playlist` and a finite item/end
bound. Their probe and transfer share that bound, and their preview uses the
resolved output template rather than inventing one selected filename from
collection metadata.

## Adaptive Versus Preset Plans

`SmartDownloadSession.plan` is always present for optimized command plans, but it
can describe either adaptive or preset behavior:

- Adaptive plans start with a pre-execution evidence pass: direct files are
  probed, allowed mirrors are scanned, and media entries are route-classified
  without a metadata probe. Plans include queue concurrency, per-host caps,
  per-file segments, connection budgets, size buckets, selected backend, safety
  notes, and scheduler decisions.
- Runtime adaptive sessions add evidence to those decisions: host EWMA speed,
  active connections, retry/error signals, current host caps, and the latest
  AIMD action. Human UI renders this as a short scheduler line; JSON/NDJSON keeps
  the normalized progress fields stable for automation.
- Preset plans describe the fixed policy that will be used when adaptive tuning
  is disabled. They still expose the same JSON shape so progress and artifacts do
  not need separate handling.

This preserves the key boundary:

- Batch and playlist-style sessions own queue concurrency.
- aria2c/Wget2/native/yt-dlp own per-item transfer mechanics.
- ffmpeg postprocessing is treated as a separate budget from media download
  concurrency.

## Scan States

Scanned sessions must keep scan state honest:

- `success`: fetch and discovery succeeded
- `partial`: discovery succeeded with warnings, such as a safe fetcher fallback
- `empty`: fetch succeeded but no actionable links were discovered
- `failed`: fetch, TLS, timeout, HTTP, or parse failure blocked discovery

Failed scans do not pretend to be empty successful scans, and empty scans do not
offer discovered-file actions. Menu actions branch from scan state first, then
from the discovered content.

## Customization

Customize screens should change controls by intent while preserving the same
flow:

| Intent | Typical customization |
| --- | --- |
| Video | Probe-driven profile, exact format picker, quality, resolution, container, video codec, HDR, FPS, subtitles, sidecar-only modes, metadata, thumbnail, archive, cookies, scheduler. |
| Audio | Probe-driven profile, exact audio picker, codec, quality, sidecar-only modes, metadata, artwork, chapters, archive, cookies, scheduler. |
| Playlist | Video/audio mode, all/range/selected item controls, output layout, archive, retries, media concurrency, postprocessor budget. |
| File | Backend, filename, resume, overwrite, checksum, user agent, headers, referrer, proxy, rate limit, segments. |
| Site | Scope, depth, no-parent, domains, max files, max total size, max runtime, filters, HTML preservation, link conversion, page requisites, wait/random wait, timeout, tries, resume, overwrite. |
| Directory | Scope, selected roots, depth, no-parent, domains, max files, max total size, max runtime, filters, file-tree preservation, wait/random wait, timeout, tries, resume, overwrite. |
| Batch | Source, kind, concurrency, adaptive bounds, site/directory allowance, media codecs. |

The primary interface should be user goals and policy. Raw backend pass-through
remains available, but it is not the normal session flow.

## Progress

Progress reporters consume normalized `ProgressEvent` values. The session tells
the UI what kind of surface to show:

- Single media: transfer, fragments, merge, metadata, thumbnail, finalize.
- Playlist media: overall item progress, transfer, postprocess lane, active
  media table, retry count, archive skips.
- Direct file: probe, transfer, verify, finalize.
- Site/directory: discovery/mirror phase, transfer, Wget2 stats, failed samples.
- Exact directory: bounded discovery, per-file native transfer, verify/finalize,
  and a selected/downloaded/current summary.
- Batch: stacked overall/transfer/lane/failure bars and one calm active table.

Unknown totals must never fake a percent. They should show bytes so far, speed,
state, and an indeterminate pulse.

## SmartSessionView

Human terminal output should be assembled with `atlas.views.SmartSessionView`
where possible. It is the reusable render layer for:

- header cards
- scan/probe panels
- plan previews
- customization overlays
- live progress dashboards
- active work tables
- scheduler decision panels
- failure drawers
- final summaries
- syntax-highlighted previews for manifests, dry-run plans, config, logs, and
  error reports

The view layer deliberately consumes simple `ViewField`, `ProgressMetric`,
`ActiveWorkRow`, and `FailureRow` values. It should not know backend-specific log
formats. Backends emit normalized events and artifacts; the smart session/view
layer chooses calm human presentation.

Interactive surfaces should keep these behavior boundaries:

- Long menus and discovered-item lists are searchable.
- Plan/manifest/config/log/error previews use the shared preview panel rather
  than raw `print()`.
- Compact/full/plain output use the same information architecture.
- `--progress json` and `--json` remain machine-only and bypass human views.
- Plain and color-disabled output preserve text labels and ASCII glyphs.

## Artifacts

Batch sessions and successful or failed non-dry-run site/directory attempts write durable artifacts
under `<output>/.atlas/` after non-dry-run execution:

- `batch-summary-*.json`: normalized `BatchSummary`
- `batch-manifest-*.json`: result items plus `smart_session` and adaptive plan
- `batch-retry-*.txt`: failed URLs, present only when failures occurred
- `latest/summary.json`: stable pointer to the newest normalized summary
- `latest/manifest.json`: stable pointer to the newest manifest, including
  artifact paths
- `latest/failed.txt`: failed URLs, written even when empty
- `latest/skipped.txt`: skipped URLs, written even when empty
- `latest/canceled.txt`: queued or active-controlled canceled URLs, written even when empty
- `latest/retry.atlas.json`: machine-readable retry/resume hints for failed-only,
  checksum-only, skipped-unknown, canceled-only, save-manifest, load-manifest,
  and resume flows

Site and directory mirrors do not create timestamped batch history files, but
they do write the same stable `latest/` session files. Successful Wget2 stats
enrich the summary. A failed Wget2 error can display parsed failed-row samples,
but the current saved recovery item is the mirror seed URL.

The user-facing commands are:

- `atlas resume [SESSION]`: retry failed URLs, skipped unknown-route URLs, and
  canceled URLs
- `atlas retry [SESSION] --failed-only`: retry failed URLs, the default selector
- `atlas retry [SESSION] --checksum-failures-only`: retry checksum failures only
- `atlas retry [SESSION] --skipped-unknowns-only`: retry skipped unknown-route URLs
- `atlas retry [SESSION] --canceled-only`: retry only canceled URLs
- `atlas export-failed [SESSION] --output failed.txt`: export retryable URLs
- `atlas inspect-session [SESSION] --preview plan|manifest`: inspect counts,
  scheduler policy, item samples, failures, artifact paths, focused session
  plans, and full JSON manifests
- `atlas inspect-session [SESSION] --preview backend`: inspect saved redacted
  backend commands for the current filtered manifest view
- `atlas inspect-session [SESSION] --preview errors|logs|config`: open
  bat-style panes for structured failures, the atlas log, or the active config
  file without starting a retry
- `atlas inspect-session [SESSION] --panel canceled|failed|scheduler|summary`:
  focus a lazygit-style state panel for queue, active, completed, canceled,
  failed, scheduler, logs, or summary views while keeping JSON report fields stable
- `atlas inspect-session [SESSION] --status failed --filter checksum`: narrow
  the saved manifest like a lightweight operator search view while retaining
  total and matched counts for JSON automation
- `atlas inspect-session [SESSION] --status failed --export-urls failed.txt`:
  write every URL in the current filtered view to a reusable text file
- `atlas inspect-session [SESSION] --copy-command resume`: copy a concrete
  operator command with `pbcopy` when available
- `atlas inspect-session [SESSION] --copy-command backend --filter NAME`: copy
  the first saved redacted backend command in the current filtered view
- `atlas inspect-session [SESSION] --open-output`: open the saved output folder

`SESSION` can be `retry.atlas.json`, `manifest.json`, `.atlas/latest`, or an
output directory. These commands load the manifest, write a scoped
`.atlas/retry/*.txt` URL file, then reuse the normal batch runner.

Future session types should follow the same pattern when they gain durable
resume/retry support: summary first, manifest second, retry/resume data third.
When item-level backend args are stored, they must be redacted before entering
stable manifests, previews, copy actions, or JSON reports.

## Classification Notes

`WorkItem` and `DirectFileProbe` can carry planner-facing notes and warning
flags. These are meant for calm UI messages and JSON inspection, not raw backend
logs. Examples:

- `This looked like a page, but resolved to a ZIP.`
- `This looked like a file, but returned HTML.`
- `No extension in URL, but Content-Disposition named release.zip.`
- `Redirected from HTTP to HTTPS.`
- signed query parameters are preserved and deduplicated conservatively
- tracking parameters are ignored only for the duplicate-detection fingerprint

Site and directory scans also carry `scan_warnings`, including the explicit
unbounded-recursion warning:

```text
Scan warning: this looks unbounded; review depth, scope, and reject rules before recursive download.
```

That warning appears for calendar/search/tag/pagination-style pages before the
user starts a recursive mirror.

## Safety Boundaries

Smart sessions can use legitimate compatibility and politeness controls:
authorized browser cookies, referrers, headers, user agents, rate limits, waits,
proxies, and yt-dlp-supported impersonation. They must not introduce stolen
session workflows, fake browser fingerprinting, browser automation to defeat bot
challenges, DRM circumvention, or access-control bypass.

## Audit Checklist

When adding or changing a download mode:

- Route it through typed options and `DownloadOptimizer` or the equivalent menu
  action path.
- Attach a `SmartDownloadSession` to the optimized plan or artifact.
- Keep backend adapters UI-free.
- Emit normalized `ProgressEvent` values.
- Update command docs, planning docs, architecture/module map, menu docs,
  [System Contracts](system-contracts.md), and responsible-use notes if the
  user-visible policy changes.
- Add focused tests for session type, customization, scheduler policy, and JSON
  artifact shape.
