# Downloader Research Notes

This note captures the design inspiration behind `atlas` as a downloader hub.
The goal is not to turn `atlas` into an unbounded website crawler. The goal is to
borrow useful queue, backend, and progress ideas from mature download tools
while keeping media downloads on the yt-dlp Python API and making website
mirroring explicit.

The current implementation folds those lessons into `SmartDownloadSession`.
Research conclusions should become mode presets, scheduler policies, progress
contracts, or artifact expectations instead of one-off backend behavior.

## Sources Reviewed

- aria2 manual: https://aria2.github.io/manual/en/html/aria2c.html
- GNU Wget2 project and manual: https://rockdaboot.github.io/wget2/
- SiteSucker macOS manual: https://ricks-apps.com/osx/sitesucker/archive/5.x/5.7.x/5.7/manuals/en/pgs/General.html
- yt-dlp embedding docs and README: https://github.com/yt-dlp/yt-dlp

## aria2 Lessons

aria2 separates queue concurrency from per-item segmented downloading:

- `--max-concurrent-downloads` controls how many queue items run at once.
- `--split` and `--max-connection-per-server` control connections inside one
  item.
- Its input-file format can attach per-item output options.
- It has a periodic summary mode rather than noisy per-fragment logs.

`atlas` mirrors that distinction:

- `atlas batch --concurrency N` controls active URLs.
- `--connections`, `--splits`, and `--chunk-size` remain per-download aria2
  tuning through yt-dlp.
- `atlas file --backend aria2` uses aria2c directly for ordinary HTTP/HTTPS
  files when the user wants that backend.

This avoids pretending aria2 helps every media protocol. It can help HTTP/HTTPS
file transfers, but DASH/HLS and extractor behavior still depend on yt-dlp and
the source site.

## Wget2 Lessons

Wget2 emphasizes unattended robustness:

- non-interactive operation
- retry/resume behavior
- input files
- clean progress modes
- recursive website support with explicit boundaries
- HTTP/2 and parallel transfer improvements

`atlas` adopts:

- batch input files
- continue-after-failure behavior
- stable final summaries
- quiet human progress by default
- explicit concurrency bounds
- an explicit Wget2 direct-file backend for users who prefer Wget2's HTTP
  downloader for a single target
- structured Wget2 stats parsing so site/server/DNS/TLS/OCSP data can be shown
  after mirrors
- partial-failure diagnostics that include downloaded bytes and failed URL
  samples when Wget2 exits nonzero

`atlas` adopts website mirroring only as an explicit command:

```bash
atlas site URL --depth 2
atlas dir URL --depth 2 --accept pdf
```

Playlist downloads are still explicit, and watch URLs with radio playlist
parameters remain single-video by default.

## SiteSucker Lessons

SiteSucker is useful as a macOS UX reference:

- it exposes simultaneous connections as a plain setting
- it has a destination folder concept
- it supports saved settings
- it offers suggested settings after a failed or incomplete site download
- it presents download behavior as a friendly utility, not a raw flag wall

`atlas` adopts:

- macOS-native config/output paths
- conservative defaults
- a simple concurrency setting
- readable status and final summaries
- explicit website mirror controls such as depth, assets, link conversion, and
  host spanning

Future `atlas` diagnostics could borrow the "suggested settings" idea, for
example recommending cookies, lower concurrency, native downloader mode, or a
compatibility quality preset after repeated failures.

## Current Design Decision

`atlas get` is the central hub entrypoint. It routes media URLs to yt-dlp,
direct file URLs to native/aria2c by default, allows explicit Wget2 file
downloads, and mirrors sites only when the user asks for `--kind site`.
Metalink expansion remains assigned to aria2 so Wget2 file downloads stay a
literal single-target backend.

Batch downloads use the same hub planner per input line in a bounded
`ThreadPoolExecutor`. Media items use independent yt-dlp engine instances, direct
files use native/aria2c/wget2 adapters, and possible site or directory mirrors
are skipped unless `--allow-sites` or `--allow-dirs` is passed. Result rows are
sorted back into input line order.

For direct-file batches, `atlas` preserves every input URL as a distinct output
even when URL basenames collide. It scopes duplicate names by path before
planning, and applies the override to both normal per-item execution and the
shared aria2 RPC queue.

Default `batch_concurrency = 2` is intentionally modest because each item may
also use aria2 with multiple HTTP/HTTPS connections. The best path is the
two-level adaptive scheduler: queue concurrency controls active URLs, while
aria2/native/Wget2/yt-dlp own per-item transfer details. Power users can raise
or let adaptive mode choose it:

```bash
atlas batch urls.txt --type audio --concurrency 4
atlas batch urls.txt --kind file --adaptive --max-concurrency 12 --per-host-concurrency 2
```

That two-level rule is now recorded in each smart session's scheduler policy:
batch and playlist sessions own queue lanes, backends own per-item mechanics,
and media postprocessing uses a separate ffmpeg budget.

For strict sequential behavior:

```bash
atlas batch urls.txt --concurrency 1
```

The final summary remains the contract for automation:

- `total`: input lines considered, including skipped blank/comment lines
- `succeeded`: successful or dry-run items
- `failed`: item failures
- `skipped`: blank/comment lines plus item-level skips

Live archive tests showed one practical split in responsibility: recursive
Wget2 directory mirrors are fast and preserve tree layout, but malformed pages
or broken links can cause Wget2 to exit nonzero or skip valid anchors. Exact-list
batch downloads are the auditable path when a scanner or operator already has
the authoritative file list and needs every URL fetched directly.
