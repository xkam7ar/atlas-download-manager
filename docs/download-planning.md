# Download Planning

`atlas` is intent-driven. Users choose outcomes. Every mode now starts as a
`SmartDownloadSession`: source, detected kind, intent, manifest, plan,
customization, scheduler policy, progress reporter, and final summary. The media
planner converts media intent into concrete `yt-dlp` options, while the hub path
routes direct files and explicit website mirrors to the appropriate backend.

## Pipeline

```text
HubRequest or DownloadRequest
  -> MediaProbe when metadata is needed
  -> MediaCapabilityResolver for media catalogs
  -> SmartPlanner for media
  -> EngineRouter for hub requests
  -> DownloadOptimizer
  -> SmartDownloadSession
  -> PresetBuilder or backend planner
  -> engine adapter
```

The session is the contract between user intent, backend-specific plans, live
progress, and final artifacts. Video, audio, playlist, file, site, directory,
and batch modes use the same session envelope with different intent presets on
top.

## Hub Routing

`atlas get URL` is conservative:

- YouTube/Rumble hosts route to `video`.
- `--audio` routes to audio extraction.
- Obvious file extensions route to direct-file download.
- Unknown non-media URLs route to direct-file download.
- Recursive website mirroring requires `--kind site` or `atlas site`.
- Open-directory/file-tree mirroring requires `--kind dir` or `atlas dir`.

The hub does not make raw backend flags the primary user model. It decides
between yt-dlp, native file streaming, aria2c, wget2, and wget from intent and
backend availability.

`DownloadOptimizer` produces an `OptimizedDownloadPlan` for previews. That plan
contains the selected route, engine, output path, safety notes, summary fields,
backend args when applicable, and the shared `SmartDownloadSession`. `atlas get
--dry-run --json` emits this plan.

Explicit commands use the same path. For example, `atlas video` builds full
`VideoDownloadOptions`, then converts those typed options into a hub execution
plan before dispatching to the yt-dlp adapter. This lets `video`, `audio`,
`file`, `site`, `dir`, playlist, and batch preserve their specialized options
without forking the routing/optimization architecture.

Session presets:

| Mode | Session type | Preset |
| --- | --- | --- |
| Video | `single_video` | one yt-dlp media worker with phase-aware postprocessing |
| Audio | `single_audio` | one yt-dlp media worker plus audio extraction |
| Playlist | `media_playlist` | adaptive media lane with bounded ffmpeg postprocessors |
| File | `direct_file` | direct-file manifest plus native/aria2/wget2 policy |
| Site mirror | `site_session` | bounded recursive mirror policy |
| Directory mirror | `directory_session` | scanned file-tree mirror policy |
| Batch | `batch_session` | mixed manifest queue with retry/summary artifacts |

For the full shared session contract, see [Smart Sessions](smart-sessions.md).
For ownership boundaries, artifacts, JSON output, and verification rules, see
[System Contracts](system-contracts.md).

## Adaptive Planning

`--adaptive` adds a scan-first pass before execution. The scan is intentionally
lightweight:

- direct files use HTTP metadata probes
- site and directory mirrors fetch the starting document and extract a bounded
  link sample up to 2,000 discovered URLs; open-directory indexes are parsed as
  visible rows when possible so folders, files, parent links, visible sizes, and
  modified times survive into `WorkItem` metadata
- batches probe direct files, scan allowed site/directory candidates, and
  route-classify media URLs without a media metadata probe

Scan requests try normal certificate verification first using the shared Atlas
fetch client and CA bundle. If Python cannot verify a certificate chain but an
installed backend fetcher such as `curl` can fetch the page safely, Atlas may
use that backend response for discovery and marks the scan `partial` with a
warning. Atlas must not silently disable certificate verification during a scan.
If verified fetching fails, the scan is `failed` with a typed error such as
`tls_failed`, `timeout`, `connection_failed`, or `http_error`. If fetching
succeeds but discovery finds no links, the scan is `empty` with `no_links`
rather than a fake success.

The scan creates `WorkItem` records with URL, kind, host, content length, size
class, bucket, range support, redirect/final host, checksum metadata when known,
recursion depth, selected backend, priority, scheduler decision, discovered
links, discovered child work items, scan status, typed scan errors, scan counts,
scan type, recommendation, and errors. Direct-file probes also include a URL dedupe fingerprint, mirror
fingerprint, classification notes, and warning flags for cases such as
page-looking URLs resolving to ZIP files, file-looking URLs returning HTML,
extensionless URLs named by `Content-Disposition`, HTTP-to-HTTPS redirects,
shortlinks, signed CDN query strings, and tracking parameters. Site and
directory scans add `scan_warnings` for parent-directory links, missing
trailing-slash redirects, encoded or spaced filenames, case-sensitive duplicate
paths, query-based folder navigation, and calendar/search/tag pages that look
unbounded. External links are counted separately and marked skipped by default
so they do not inflate same-host file totals. The `AdaptiveScheduler` then
builds an `AdaptiveDownloadPlan` with:

- `queue_concurrency`
- `per_host_concurrency`
- `per_file_segments`
- `per_file_segment_cap`
- `global_min_concurrency` and `global_max_concurrency`
- `max_active_files`
- `max_total_connections`
- `max_per_host_connections`
- `max_active_postprocessors`
- `max_disk_write_bytes_per_sec`
- `speed_limit`
- selected backend preference
- size, bucket, and host counts
- politeness profile
- safety notes

`--explain` prints this plan without transferring files. It is the preferred way
to audit large direct-file batches or open-directory mirrors before starting the
download.

Adaptive direct-file and mixed-engine batch behavior separates queue concurrency from
per-file segmented downloading. A batch can run six URLs at once while each
medium ranged URL uses three aria2c segments, and a same-host cap can still
limit how many URLs hit one host concurrently. Adaptive site and directory
mirrors use the same scheduler model for crawler politeness, then delegate the
actual recursion to Wget2 or Wget. When the scan finds many same-host tiny/small
discovered files, the recursive plan can open a small-file lane with higher
queue concurrency. When it finds large archive or media candidates, the plan
keeps active jobs lower and waits for range support before splitting a single
transfer.

Atlas enforces a three-layer speed budget:

```text
global connection budget
  -> per-host budget
    -> per-item segments, fragments, or mirror threads
```

The scheduler clamps both queue size and per-item segments so
`queue_concurrency * per_file_segments` never exceeds the effective
`max_total_connections` for the plan. This keeps `--max-concurrency 32` plus
`aria2 -x16 -s16` from turning into hundreds of sockets by accident. aria2c batch
downloads use the shared RPC queue path when possible, so Atlas can tune
`max-concurrent-downloads` globally and observe aria2 status/speed/error samples
instead of launching many independent aria2 processes.

Runtime adaptation is evidence-driven. Progress events feed host-level speed
samples into the scheduler: EWMA speed, active connections, retry count, status,
backend, bucket, and downloaded bytes. Healthy hosts can increase cap slowly
after stable samples. Hosts that emit 429/503/403, timeouts, retry spikes, or
speed collapse are cut back with multiplicative decrease and held before
increasing again. Local pressure signals such as disk, CPU, or postprocess
bottlenecks reduce the global queue and can apply a temporary speed limit.

The mixed-engine runtime queue submits only work that is currently runnable. Global caps,
dynamic host caps, and operator pause state are checked before an item enters
the executor; a blocked row remains pending instead of parking a worker thread.
This lets unrelated hosts continue while one host is paused or backed off.
The runtime API can use a resolved/CDN host when a caller supplies a host
resolver. The built-in CLI currently feeds back against the original item host.

The scheduler favors tiny/small direct-file buckets with higher queue
concurrency and no per-file splitting. Large/huge ranged buckets get lower queue
concurrency and more aria2 segments. Media buckets remain on yt-dlp, and
recursive mirror buckets remain bounded/explicit before Wget2 or Wget owns the
crawl. Unknown-size items start with conservative queue behavior; when progress
events later report a total byte count, Atlas reclassifies the row and clamps
future queue starts if the item turns out to be medium, large, or huge.

## Website Mirror Planning

`atlas site` deliberately keeps website mirroring explicit. The typed
`SiteDownloadOptions` model maps high-value Wget2 behavior into stable Atlas
flags instead of exposing a raw command string as the default interface.

`atlas dir` uses `DirectoryMirrorOptions` for open HTTP directory indexes and
file trees. It shares the wget2/wget backend planner with `site`, but defaults
page requisites and link conversion off, keeps host spanning off, prevents
parent ascent, and uses separate `dir_depth`, `dir_wait`, and `dir_backend`
configuration.

Real directory downloads scan before execution. Conventional HTML indexes stay
on the recursive Wget2/Wget path. Signature-recognized CopyParty HTML and
plain-text indexes take a stricter path: Atlas walks only same-host supported
indexes to the requested depth, refuses truncated or unsupported nested pages,
applies accept/reject filters, and builds an exact list capped at 2,000 selected
files. `--max-files` is enforced against that list. If `--max-total-size` is
present, every selected file must expose a size and the sum must fit the bound.
`--max-runtime` covers both exact-list discovery and transfer.

The resulting `native-exact-index` executor preserves safe relative paths and
uses the native direct-file engine for each selected URL. It rejects traversal,
symlink escapes, and case-insensitive destination collisions before transfer,
then honors native resume, timestamps, overwrite, rate, TLS, and retry policy.
Cancellation is checked between files and at native progress checkpoints. The
transfer loop is currently sequential, one native file at a time. Recursive
mirror `--wait` and `--random-wait` settings do not apply to this exact loop.

The planner passes through Wget2-native behavior for:

- scope and recursion controls such as `--no-parent`, `--depth`, `--domains`,
  `--exclude-domains`, `--span-hosts`, `--same-host-only`,
  `--same-domain-www`, and `--include-subdomains`
- parser controls such as `--follow-tags`, `--ignore-tags`, `--force-html`, and
  `--force-sitemap`
- content filters such as `--accept`, `--reject`, `--include-directories`,
  `--exclude-directories`, `--accept-regex`, and `--reject-regex`
- website offline-copy controls such as `--convert-links`,
  `--adjust-extension`, and `--page-requisites`
- URL and filename shaping such as `--filter-urls`, `--cut-url-get-vars`,
  `--cut-file-get-vars`, `--keep-extension`, and `--convert-file-only`
- network controls such as `--bind-interface`, `--prefer-family`,
  `--dns-cache-preload`, `--tcp-fastopen`, `--wait`, `--random-wait`,
  `--timeout`, and `--tries`
- TLS, OCSP, HSTS, and detached signature controls such as `--ocsp-stapling`,
  `--tls-session-file`, `--hsts-file`, and `--verify-sig`

Atlas adapts two areas around Wget2:

- Friendly scope presets compile to Wget2 host/domain controls. `same-host-only`
  disables spanning; `same-domain-www` expands the seed domain to include the
  `www` variant; `include-subdomains` enables domain-bounded spanning.
- `max_files`, `max_total_size`, and `max_runtime` keep recursive plans bounded.
  `max_files` is a scan-planning guard, `max_total_size` maps to quota, and
  `max_runtime` is enforced by Atlas around the mirror process.
- `--cookies-from-browser` exports browser cookies through yt-dlp's cookie
  extractor into a temporary Netscape cookie jar, then passes that file to
  Wget2 with `--load-cookies`.
- Wget2 stats files are parsed back into JSON-friendly summaries for
  site/server/DNS/TLS/OCSP state.
- Wget2 nonzero exits include parsed stats where possible. If a mirror exits
  after partial work, Atlas reports downloaded bytes and failed URL samples in
  the `EngineError`.

## Media Capability Resolution

Atlas probes media sources before asking normal interactive users to choose
quality, codec, or container details. `MediaProbe` calls yt-dlp with
`download=False`, then `media_capabilities.py` normalizes the returned formats
into a `MediaCapabilityCatalog`.

The catalog separates three different concepts:

- container: `mkv`, `mp4`, `webm`, source extension, or generated output
- video codec: H.264, HEVC, VP9, AV1, or another source codec
- audio codec: AAC/M4A, Opus, MP3, FLAC, WAV, or another source codec

The resolver produces source-aware profiles:

| Profile | Meaning |
| --- | --- |
| Best quality | Best available video plus best available audio, preserving source quality. |
| Balanced | Best source at or below the 1440p planning target when available. |
| Apple compatible | MP4/H.264-or-HEVC video plus M4A/AAC audio when available. |
| Small file | Smallest good source near 720p when available. |
| Audio only | Best available source audio without unnecessary conversion. |
| MP3 | Audio source plus explicit ffmpeg conversion. |
| Custom formats | Exact probed video/audio format IDs selected by the user. |

Each profile carries a `CapabilityStatus`:

- `available`: the requested result exists without conversion
- `fallback`: Atlas found the nearest safe source alternative
- `requires_remux`: the source can be repackaged without re-encoding
- `requires_transcode`: ffmpeg conversion is required and must be confirmed
- `unavailable`: the menu hides the impossible choice and offers alternatives

The interactive menu uses these profiles before the plan preview. It should not
ask for `h264 + mp4` or `vp9 + webm` before it knows whether those streams
exist. Raw `--format` remains an advanced command/menu escape hatch, but the
normal UI recommends what works, shows what exists, warns about conversion, and
does not silently offer impossible media plans.

`atlas formats` reuses the same resolver. Human output includes recommended
profiles plus the exact format table; `--json` remains a stable list of raw
`FormatInfo` rows without human UI.

## Video Quality Intents

| Intent | Goal | Typical Format |
| --- | --- | --- |
| `max` | Preserve best source quality. | `bestvideo*+bestaudio/best` |
| `balanced` | High quality with a size-friendly cap. | Best video/audio with a default 1440p cap. |
| `compatible` | Apple/QuickTime/iPhone/iPad friendly. | MP4 video plus M4A audio. |
| `small` | Smaller file bias. | Lower resolution and bitrate filters. |

### `max`

Default for video.

```text
bestvideo*+bestaudio/best
```

With `container=auto`, this resolves to `mkv`.

### `balanced`

Adds a default cap unless the user chooses a specific resolution.

```text
bestvideo*[height<=1440]+bestaudio/best[height<=1440]/best
```

### `compatible`

Prefers MP4 and Apple-friendly codecs. When a catalog is available, Atlas first
tries exact probed streams. Without a catalog, the planner uses a fallback
ladder rather than one brittle selector:

```text
bestvideo*[ext=mp4][vcodec~='^(avc1|hvc1|hev1)'][height<=1080]
+bestaudio[ext=m4a]
/bestvideo*[ext=mp4][height<=1080]+bestaudio[ext=m4a]
/best[ext=mp4][height<=1080]
/bestvideo*[height<=1080]+bestaudio
/best[height<=1080]
/best
```

The planner rejects incompatible combinations:

- `--quality compatible --container webm`
- `--quality compatible --video-codec av1`
- `--quality compatible --video-codec vp9`

### `small`

Uses resolution and bitrate caps:

```text
bestvideo*[height<=720][tbr<=2500]+bestaudio/best[height<=720]/best
```

It also prefers smaller formats in format sort.

## Container Resolution

`--container auto` is intentional:

| Quality | Auto Container |
| --- | --- |
| `max` | `mkv` |
| `balanced` | `mkv` |
| `small` | `mkv` |
| `compatible` | `mp4` |

Max quality does not default to MP4 because MKV better preserves AV1, VP9, and
Opus combinations.

## Audio Planning

Default:

```text
bestaudio/best
```

In the menu, audio source selection is catalog-driven. `best` preserves the best
available source audio. Choosing MP3, FLAC, or WAV is treated as a workflow
conversion choice and requires ffmpeg; it does not improve a lossy source.

Audio postprocessor:

```python
{
    "key": "FFmpegExtractAudio",
    "preferredcodec": codec,
    "preferredquality": quality,
}
```

Codec choices:

| Codec | Purpose |
| --- | --- |
| `best` | Preserve best source audio when possible. |
| `opus` | Modern compressed audio. |
| `m4a` | Apple-friendly audio. |
| `mp3` | Broad compatibility. |
| `flac` | Lossless container workflow, not source improvement. |
| `wav` | Editing workflow. |

## Metadata, Thumbnail, Chapters, Subtitles

Default media postprocessors:

- `FFmpegMetadata`
- `EmbedThumbnail`

Defaults:

- metadata enabled
- thumbnail writing enabled
- thumbnail embedding enabled
- info JSON enabled
- chapters enabled
- subtitles disabled
- split chapters disabled

Subtitle modes:

| Mode | yt-dlp Options |
| --- | --- |
| `none` | no subtitle options |
| `manual` | `writesubtitles` |
| `auto` | `writeautomaticsub` |
| `all` | manual, automatic, all subtitles |

Sidecar-only shortcuts normalize before the yt-dlp preset is built:

| Atlas option | Planner effect |
| --- | --- |
| `--subtitle-only` | Enables `skip_download`, writes manual subtitles if no subtitle mode was set, and disables metadata/thumbnail embedding. |
| `--thumbnail-only` | Enables `skip_download`, writes the thumbnail, and disables info/metadata embedding. |
| `--info-only` | Enables `skip_download`, writes info JSON, and disables thumbnail/metadata embedding. |
| `--skip-download` | Leaves selected sidecars enabled but disables embed postprocessors that require a media file. |

Only one of `subtitle_only`, `thumbnail_only`, and `info_only` may be selected.

## Selection, Sections, and SponsorBlock

Media requests can carry yt-dlp selection controls through the typed plan:

- `match_filters` and `break_match_filters` become yt-dlp `match_filter`
  callables.
- `max_downloads`, `break_on_existing`, `break_on_reject`, and
  `break_per_input` map to yt-dlp queue-break controls.
- `date`, `date_before`, `date_after`, `min_filesize`, and `max_filesize` map to
  yt-dlp date and downloader size filters.
- `reject_live` and `reject_upcoming` synthesize `!is_live` and `!is_upcoming`
  match filters.
- `download_sections` becomes yt-dlp `download_ranges`, including chapter regexes
  and `*start-end` time ranges.

SponsorBlock controls add the same postprocessor pair used by yt-dlp's CLI:
`SponsorBlock` runs after filtering, then `ModifyChapters` marks or removes the
selected categories before metadata embedding.

## Playlist Safety

The planner distinguishes explicit playlist URLs from bounded YouTube
channel/tab collection URLs.

Effective playlist mode is:

```python
requested_playlist and (
    is_explicit_playlist_url(url) or is_youtube_collection_url(url)
)
```

That means:

- `atlas video WATCH_URL`: single video.
- `atlas audio WATCH_URL`: single video.
- `atlas video WATCH_URL --playlist`: still single video if the URL is a watch URL.
- `atlas playlist PLAYLIST_URL`: playlist.
- `atlas playlist WATCH_URL_WITH_LIST`: refused by the CLI.

YouTube channel and tab URLs are collection URLs. `atlas video` and
`atlas audio` accept them only with `--playlist` plus a finite
`--playlist-items` or `--playlist-end` bound. The same bound is used for the
metadata probe and transfer, preventing an unbounded pre-download enumeration.
Collection plan previews show the resolved yt-dlp template because
collection-level metadata cannot truthfully predict one selected item's final
filename.

Effective playlist plans set `ignoreerrors = "only_download"` so removed,
private, or unavailable entries can be skipped without hiding merge/extract
postprocessor failures. If an explicit playlist URL is pasted into Video or
Audio in the interactive menu, Atlas asks whether to run it as playlist video or
playlist audio before planning.

## Downloader Engine Planning

`--download-engine` values:

| Value | Behavior |
| --- | --- |
| `auto` | Use aria2c for HTTP/HTTPS when enabled and installed. |
| `native` | Use yt-dlp native downloader. |
| `aria2` | Require aria2c and fail early if missing. |

aria2 options:

```text
-x16 -s16 -k1M --continue=true
```

Advanced media reliability options are passed through to yt-dlp's native option
names where possible: `file_access_retries`, `concurrent_fragment_downloads`,
`retry_sleep_functions`, `skip_unavailable_fragments`, `throttledratelimit`,
`http_chunk_size`, `socket_timeout`, `source_address`, `impersonate`, and
`extractor_args`.

The external downloader is scoped to HTTP/HTTPS:

```python
{"http": "aria2c", "https": "aria2c"}
```

`atlas` does not claim aria2 improves every DASH or HLS stream. Benefits vary.

## Direct-File Probe

For real direct-file downloads in `auto` backend mode, the optimizer performs a
lightweight HTTP probe before choosing native streaming or aria2c. Wget2 is an
explicit file backend for users who want that downloader for a single target. The
probe records:

- content type
- content length
- `Content-Disposition` filename
- byte-range support
- ETag and Last-Modified
- HTTP `Link` headers that advertise `.meta4` or `.metalink` resources
- redirect target
- file extension

Backend choice:

| Probe result | Choice |
| --- | --- |
| Large file with byte ranges and aria2c available | aria2c |
| Small file | native |
| aria2c missing or disabled | native |
| Unknown size | aria2c when enabled and available, otherwise native |

Selecting `--backend wget2` or setting `file_backend = "wget2"` bypasses the
auto native/aria2 choice and builds a Wget2 single-file plan with
`--output-document`.

Metalink expansion is an aria2-only feature in Atlas. `.meta4` and `.metalink`
URLs, or ordinary file URLs that advertise a Metalink through HTTP `Link`
headers, expand through aria2 unless `--no-metalink` is used to save the manifest
itself. Explicit native or Wget2 file downloads reject Metalink expansion with a
recovery hint.

Dry runs do not probe the network. Their plan marks the probe as skipped and
keeps the choice deterministic from config and local tool availability.

Adaptive explain runs do probe because their purpose is to size and classify the
work before download. That distinction is intentional: `--dry-run` is a
no-network command preview, while `--adaptive --explain` is a metadata audit.

## Preflight

Real downloads call `ensure_download_dependencies` before metadata extraction.

Required:

- `ffmpeg`
- `ffprobe`

Missing tools raise `DependencyMissingError` with a host-specific setup or
package-manager install hint.

Dry runs skip preflight because they only print resolved options.

## Progress Events

Every engine emits the same neutral event shape before UI rendering:

```python
ProgressEvent(
    engine="yt-dlp",
    kind="video",
    phase="download",
    status="downloading",
    filename="Example.mkv",
    downloaded_bytes=742_000_000,
    total_bytes=1_200_000_000,
    fragment_index=42,
    fragment_count=180,
    speed_bytes_per_sec=18_400_000,
    eta_seconds=31,
    files_done=None,
    files_total=None,
    retry_count=None,
    percent=None,
    message=None,
)
```

Video and audio downloads still attach yt-dlp-compatible hooks:

```python
create_progress_hook(reporter)
create_postprocessor_hook(reporter)
```

The download hook converts raw yt-dlp byte/fragment dictionaries into
`ProgressEvent(phase="download")`. The postprocessor hook converts yt-dlp
postprocessor events into phases such as `merge`, `extract`, `postprocess`, and
`finalize`. This prevents the UI from showing a job as complete while ffmpeg is
still merging, extracting audio, embedding metadata, embedding thumbnails, or
moving final files.

`YtdlpEngine` remains UI-free and only receives ordinary `progress_hooks` and
`postprocessor_hooks` lists.

Direct-file and mirror backends emit the same model:

| Backend | Progress source |
| --- | --- |
| `native` | byte counts from internal response reads, then verify/finalize events |
| `aria2c` | localhost JSON-RPC `tellStatus`, including bytes, speed, ETA, and connections |
| `aria2c` fallback | streamed console readout parsed into bytes, speed, and ETA |
| `wget2` / `wget` | streamed subprocess phases and coarse progress/percent lines |

The CLI passes a `Callable[[ProgressEvent], None]` into direct-file and site
adapters. That callback is the only contract the UI needs.

For direct-file aria2 downloads, `atlas` starts a short-lived local aria2c
process with RPC enabled, `--rpc-listen-all=false`, an ephemeral localhost port,
and a random RPC secret. The secret is generated only at runtime and dry-run
plans show it as `<redacted>`. Downloads are submitted with `aria2.addUri`;
Metalink manifests use `aria2.addMetalink`. Progress comes from
`aria2.tellStatus`, including ids, file lists, integrity verification state,
piece metadata, and follow-chain fields when aria2 reports them. If the local
RPC process cannot be started or reached, the legacy streamed subprocess
downloader remains available as a startup fallback.

When `save_session` is set, Atlas starts aria2 with `--save-session` and
`--force-save=true`, sets per-download `force-save` for RPC submissions, and
calls `aria2.saveSession` before shutdown. `input_file` reloads an existing
aria2 session file at daemon startup. Metalink preference options and
`uri_selector=adaptive` are passed through to aria2; `server_stat_if` and
`server_stat_of` let aria2 reuse mirror speed profiles across runs.

Batch downloads attach one hook per active item:

```python
create_batch_progress_hook(reporter, line_no=entry.line_no, url=entry.url)
create_batch_postprocessor_hook(reporter, line_no=entry.line_no, url=entry.url)
```

`BatchProgressReporter` renders an Atlas card, a small dashboard card, stacked
semantic bars, and one active table. The bars separate whole-job progress,
aggregate transfer bytes, direct-file lanes, media lanes, mirror lanes, and
failures. Known-total bars are colored by meaning and carry a subtle shimmer
while active; retry and unknown-total rows pulse instead of pretending to know a
percentage. Each active URL gets one calm row with line number, kind, name, size,
progress, speed, ETA, and engine. Scheduler decisions live in the dashboard so
the table does not become a noisy backend log. The batch runner owns concurrency
and result aggregation; engines still receive ordinary hooks/callbacks and
remain unaware of Rich.

Interactive Batch starts with source choices. `Use URL file` follows the normal
`atlas batch FILE` queue path, `Paste multiple URLs` writes a generated queue
under `<output>/.atlas/menu/`, playlist mode builds a media playlist plan, and
resume/retry delegate to saved session artifacts. `Paste URL and scan` performs
a bounded `scan_site` pass first, shows seed, scan
type, host, link counts, same-host files/folders/HTML/media, skipped external
links, rough estimated size, and a recommended mode.

If the seed looks like an open directory index, the menu switches to the
Directory Explorer before execution. Stage 1 is a fast root map: Atlas parses
the visible index rows, skips Parent Directory by default, shows folder and file
counts, and lets the user choose everything, one folder, multiple folders, only
visible files, a tree preview, a deep scan, or offline website treatment. Stage
2 only scans the selected roots. If that selected scope yields exact file URLs,
Atlas writes an exact-list queue under `<output>/.atlas/menu/` and runs the
normal adaptive batch planner. If the selected roots still need recursion,
Atlas queues those explicit directory roots with `--allow-dirs` semantics. This
keeps the menu smart and scope-first while preserving typed options and batch
artifacts.

Non-directory scans route the chosen action into typed hub options or generated
queues: a same-host downloadable file queue for all direct file/media links, a
selected-file queue from a checkbox multi-select over discovered files,
`DirectoryMirrorOptions` for recursive mirrors, `SiteDownloadOptions` for an
offline website mirror, or `FileDownloadOptions` for a single direct file.
Offline website choices reuse site controls such as no-parent, domains, page
requisites, convert links, adjust extension, wait/random-wait, timeout, tries,
continue partial files, backend selection, and adaptive planning. Directory
choices reuse the directory mirror policy: no-parent, same-host domains,
wait/random-wait, timeout, tries, continue partial files, backend selection,
timestamping, no-if-modified-since, browser-style user agent, and adaptive
planning, with page requisites and link conversion left off. When Wget2 is the
directory backend, Atlas includes `--recursive`, `--no-parent`, `--mirror`,
`--continue`, `--timestamping`, `--no-if-modified-since`,
`--directory-prefix=<output>`, and the directory user agent by default. Atlas
then appends the configured depth bound, such as `--level=2`, so the default
mirror remains scoped.

After a non-dry-run batch completes, Atlas writes durable run artifacts under
`<output>/.atlas/`:

- `batch-summary-*.json`: the normalized `BatchSummary`
- `batch-manifest-*.json`: result items plus `smart_session` and adaptive plan data
- `batch-retry-*.txt`: failed URLs, present only when failures occurred
- `latest/summary.json`: stable newest summary
- `latest/manifest.json`: stable newest manifest
- `latest/failed.txt`: failed URLs, always present
- `latest/skipped.txt`: skipped URLs, always present
- `latest/canceled.txt`: queued or active-controlled canceled URLs, always present
- `latest/retry.atlas.json`: retry/resume manifest with failed-only,
  checksum-only, skipped-unknown, canceled-only, save/load manifest,
  export-failed, and resume pointers

`atlas resume`, `atlas retry`, and `atlas export-failed` can load either the
retry manifest, the latest manifest, `.atlas/latest`, or the output directory.
Retry and resume commands write a scoped `.atlas/retry/*.txt` file and then run
the regular batch path so planner, progress, and artifact behavior remains
shared.

On the mixed-engine executor, the execution-layer `BatchControl` primitive supports global pause/resume,
per-host pause/resume, cancel-all, and cancel-by-line for queued and controlled
active items. Canceled items become `DownloadStatus.canceled`, are counted
separately, and are also included in skipped accounting so legacy totals remain
balanced. Batch execution supplies an optional `BatchItemContext` with one
runner-level `ProcessControl` per started row. Wget2/Wget work uses it to
terminate the child process. Native direct files and exact-index work check it
at progress and file boundaries; media work checks it in yt-dlp progress and
postprocessor hooks. These in-process paths are cooperative and do not pretend
to suspend arbitrary code between checkpoints. Every path surfaces operator
cancellation as a distinct state rather than backend noise.
`BatchOperatorController` applies the shared live keys to this state: `g` for
global pause/resume, `h` for focused-host pause/resume, `s` for focused queued
line pause/resume, `x` for focused-line cancel, and `X` for cancel-all. The
queued-line pause only gates items that have not started; Atlas does not fake
active transfer suspension. `BatchProgressReporter` handles UI-only keys first:
normalized arrows move a color-independent focused row marker, `tab` cycles the
queue/active/completed/failed/scheduler/logs/summary panels, and `?` toggles the
shared shortcut overlay. The live key reader is enabled only for interactive
full-progress batch sessions so scripts and JSON output remain stable.

Successful and failed non-dry-run standalone `atlas site` and `atlas dir`
attempts also write the stable
`latest/summary.json`, `latest/manifest.json`, `latest/failed.txt`,
`latest/skipped.txt`, `latest/canceled.txt`, and `latest/retry.atlas.json` files.
Successful Wget2 stats contribute summary detail. On a failed Wget2 process,
the displayed error retains parsed failed-row samples, but the current recovery
artifact retries the mirror seed URL rather than individual stat rows.

Batch result events preserve the routed plan, so final tables continue to show
the actual kind and engine after a backend emits its final done event. Direct
file batches also compute duplicate basename overrides before planning. The
override is applied to both ordinary per-item execution and the shared aria2 RPC
queue, so optimized large-file batches do not silently overwrite colliding
filenames.

Concurrency is queue-level. It controls how many URLs are active at once.
aria2 settings remain item-level and control connections/splits inside a single
HTTP/HTTPS download when aria2 is selected. For all-aria2 direct-file batches,
atlas keeps one local aria2 RPC process alive, queues every item into that
session, and maps batch concurrency to `--max-concurrent-downloads`.
With `--adaptive`, mixed-engine batches use the adaptive queue value even when
`--concurrency` is present; the shared aria2 path uses explicit
`--concurrency` as its global queue value. The shared path queues all items
immediately, does not apply per-host runnable gating, and deliberately does not
bind `BatchControl`, so pause/cancel mutation keys are hidden.
Adaptive queue/speed changes call `aria2.changeGlobalOption` only when the
effective option dictionary changes; the cached value is cleared when the RPC
process stops so a later session starts cleanly.
If RPC startup fails, Atlas falls back to ordinary per-item batch execution. A
mid-session RPC loss preserves completed items, fails unresolved items, and
removes active GIDs best-effort. TLS-chain failures receive the verified-curl
per-item retry; general mid-session RPC errors do not automatically retry via
the legacy subprocess.
When Wget2 is selected for a direct file, the file command maps `connections` to
Wget2 `--max-threads` and `chunk_size` to `--chunk-size`.

Progress modes:

| Mode | Meaning |
| --- | --- |
| `auto` | Rich progress on TTY; no progress mixed into JSON output. |
| `compact` | Restrained Atlas card, colored stacked bars, and active table. |
| `full` | Adds scheduler diagnostics while keeping the same card/bar/table structure. |
| `json` | Newline-delimited progress events for machine consumers, including adaptive scheduler fields when known. |
| `none` | No live progress. |

## Dry Run

Dry-run behavior:

- Builds the same `ydl_opts` as a real download.
- Redacts runtime-only objects such as logger and progress hooks.
- Redacts cookie file paths.
- Does not call the network.
- Does not download anything.
