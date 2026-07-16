# Command reference

[Documentation home](README.md) · [Quick start](quick-start.md) ·
[Configuration](configuration.md) · [Troubleshooting](troubleshooting.md)

This page documents the user-facing CLI. For humans, the interactive menu is the
primary Atlas experience. Commands remain the scriptable layer for automation,
JSON output, CI, repeatable examples, and advanced support/debugging.

## Find a command

| Area | Commands | Use them for |
| --- | --- | --- |
| Start | [`atlas`](#atlas), [`atlas get URL`](#atlas-get-url) | Open the menu or let Atlas route one URL. |
| Media | [`video`](#atlas-video-url), [`audio`](#atlas-audio-url), [`playlist`](#atlas-playlist-url), [`info`](#atlas-info-url), [`formats`](#atlas-formats-url) | Download or inspect media. |
| Files and mirrors | [`file`](#atlas-file-url), [`site`](#atlas-site-url), [`dir`](#atlas-dir-url) | Download files or run explicit bounded mirrors. |
| Batch and recovery | [`batch`](#atlas-batch-file), [saved-session commands](#saved-session-commands) | Process URL lists, inspect runs, and retry narrowly. |
| Setup | [`setup`](#atlas-setup), [`update`](#atlas-update), [`doctor`](#atlas-doctor), [`config`](#atlas-config) | Install, verify, repair, update, and configure Atlas. |
| Advanced | [Backend pass-through](#advanced-backend-pass-through) | Send raw argv to a backend after reviewing the plan. |

Global discovery commands:

```bash
atlas --version
atlas --help
atlas COMMAND --help
```

## `atlas`

Interactive menu launcher. When stdin and stdout are real TTYs and atlas is not
running under automation, no-argument `atlas` opens a keyboard-navigable menu.
This menu is the primary human interface; command mode is the same typed
planning/execution system exposed as an automation API.

```bash
atlas
atlas menu
atlas --no-menu
```

Top-level menu choices:

- Paste URL
- Media
- Files
- Batch
- Sessions
- Tools
- Settings
- Quit

Normal operator commands stay menu-reachable through grouped submenus:

- Media: Download video, Extract audio, Download playlist, Show info, Show formats.
- Files: Download file, Browse directory, Mirror website.
- Batch: Paste URL and scan, Paste multiple URLs, Use URL file, Playlist as batch.
- Sessions: Resume session, Retry failed, Inspect session, Export URLs.
- Tools: Doctor, Setup tools, Update Atlas, Advanced backend, Help.
- Settings: Config.

In a real interactive terminal, Atlas may show a setup gate before this menu
when required media tools are missing or when a first-run full-runtime check
finds missing backends. The setup gate offers install, plan-only, limited-mode,
Doctor, and quit paths. It never installs package-manager tools without an
explicit setup/install action and confirmation.

Download flows ask for the minimum input first, usually only a URL. Atlas then
builds the same typed request models used by the CLI commands, routes through
the hub planner/optimizer, and shows a plan preview. From there choose Start,
Customize, Dry run, Back, or Quit.
`Tools` → `Help` shows the controls supported by menu prompts: arrows to move,
Enter to choose, typing to filter, Space to toggle multi-select rows, and Ctrl-C
to cancel and go back. Live progress and saved-session actions use their own
contextual controls; mutation keys appear only when a real controller is attached.
`Config` can show resolved settings, show the config path, or open the config
file/containing folder for editing.
`Tools` -> `Setup tools` opens the same guided setup plan as `atlas setup`.
`Update Atlas` shows the detected install method and update command.

The menu is intentionally not automatic when stdin/stdout are non-TTY, when
`--json` is passed, when `--no-menu` is passed, or in common automation
environments such as CI.

For directory-like URLs, the menu uses a first-class browse flow instead of a
generic recursive prompt. Atlas shows one compact browse card with seed, scope,
visible counts, and estimated size; preview sections for folders and a short
root-file sample; optional warnings; then actions such as `Everything under
this folder`, `Choose one specific folder`, `Choose multiple folders`, `Only
visible files at this level`, `Browse full folder tree first`, and `Deep scan
selected folders first`. The root-file sample is preview-only; `Only visible
files at this level` opens the full searchable visible-file picker.

## `atlas setup`

Guided setup planner and repair surface. It detects the host, package manager,
install method, selected runtime footprint, config path, and output directory.
By default it prints a plan and creates Atlas paths; it only runs package-manager
commands with `--install` and confirmation or `--yes`. `--no-install` and
`--json` are plan-only and do not create paths or install packages.

```bash
atlas setup
atlas setup --full
atlas setup --minimal
atlas setup --media-only
atlas setup --mirrors
atlas setup --no-install
atlas setup --install --yes
atlas setup --json
atlas setup --open-menu
```

Runtime footprints:

- `--full`: `ffmpeg`, `ffprobe`, `aria2c`, `wget2`, `wget`
- `--minimal`: `ffmpeg`, `ffprobe`
- `--media-only`: `ffmpeg`, `ffprobe`
- `--mirrors`: `wget2`, `wget`

`atlas setup --json` emits the setup plan without human UI.
Its stable top-level fields are `mode`, `environment`, `tools`, `missing_tools`,
`install_commands`, `manual_commands`, `config_file`, `output_dir`,
`can_install`, `complete`, and `notes`. `environment.package_manager` is one of
`homebrew`, `apt`, `dnf`, or `pacman` on supported hosts. Each tool row reports
the executable, host-specific package name, purpose, required status, and
installed status. `ffmpeg` and `ffprobe` intentionally share one package and one
install command.

Setup uses Homebrew on macOS, apt on Debian/Ubuntu, dnf on Fedora, and pacman on
Arch-family Linux. Fedora maps the runtime to `ffmpeg-free`, `aria2`, `wget2`,
and `wget1-wget`. Because official pacman repositories do not provide `wget2`,
the full pacman plan installs that one tool through Linuxbrew. Without root or
`sudo`, native Linux plans remain manual and `can_install` is false.
`--open-menu` opens the interactive menu after the normal setup result; it has
no effect in the early-return `--no-install` or `--json` plan modes.

## `atlas update`

Install-method-aware update planner. Atlas detects Homebrew, uv-tool,
source-checkout, or unknown installs and shows the matching command.

```bash
atlas update
atlas update --dry-run
atlas update --yes
atlas update --json
```

Unknown install methods are not modified automatically.
Source checkout updates use `git -C <checkout> pull --ff-only`, so the command
works even when Atlas is launched from another current directory.

## `atlas get URL`

Smart hub command. It routes the URL to the safest likely outcome:

- YouTube/Rumble hosts: media video defaults through yt-dlp.
- Obvious file extensions such as `.zip`, `.dmg`, `.mp4`, `.pdf`: direct file.
- Other non-media URLs: direct file by default.
- Website mirroring: explicit with `--kind site`.
- Open directory/file-tree mirroring: explicit with `--kind dir`.

```bash
atlas get URL
atlas get URL --audio
atlas get URL --video-codec h264
atlas get URL --kind audio --codec mp3 --audio-quality 3
atlas get URL --kind file --backend aria2
atlas get URL --kind file --backend wget2
atlas get URL --kind file --adaptive --explain
atlas get URL --kind site --backend wget2 --dry-run
atlas get URL --kind dir --backend wget2 --dry-run
atlas get URL --kind dir --adaptive --explain
atlas get URL --kind file --checksum sha256:...
atlas get URL --dry-run --json
```

`get` is conservative. It does not recursively mirror arbitrary pages unless
`--kind site` or `--kind dir` is selected.

`atlas get --dry-run --json` prints an optimized plan with route, selected engine,
output path, safety notes, summary fields, and backend args where applicable.

`--adaptive` asks Atlas to scan lightweight metadata before planning. For direct
files it can tune native versus aria2c, queue caps, per-host caps, and segment
counts. For site and directory mirrors it tunes politeness and exposes crawler
queue guidance. `--explain` prints the adaptive plan without downloading.

## Advanced backend pass-through

For complete backend flag coverage, atlas provides explicit advanced commands:

```bash
atlas ytdlp -- [yt-dlp args...]
atlas aria2 -- [aria2c args...]
atlas wget2 -- [wget2 args...]
atlas wget -- [wget args...]
```

Examples:

```bash
atlas ytdlp -- --help
atlas ytdlp -- --format "bv*+ba/b" URL
atlas aria2 -- --continue=true --split=16 URL
atlas wget2 -- --recursive --level=2 URL
atlas wget -- --mirror URL
```

Options before `--` belong to atlas:

```bash
atlas ytdlp --dry-run -- --format "bv*+ba/b" URL
atlas aria2 --json -- --version
atlas wget2 --backend-help
```

Backend flags after `--` are passed as argv items without a shell. atlas does not
reinterpret, quote-expand, or translate those flags. Human mode shows an
`Advanced Backend` plan panel before execution. `--dry-run` prints only the
command plan; `--json` returns the plan, return code, stdout, and stderr.

These commands are intended as a power-user escape hatch. The interactive menu
and typed intent commands remain the recommended UX for ordinary media, files,
batches, and site mirrors.

## `atlas file URL`

Download a direct HTTP/HTTPS file with native Python streaming, aria2c, or wget2.

```bash
atlas file "https://example.com/archive.zip"
atlas file "https://example.com/archive.zip" --backend native
atlas file "https://example.com/archive.zip" --backend aria2 --connections 16 --splits 16
atlas file "https://example.com/archive.zip" --backend wget2 --connections 6 --chunk-size 4M
atlas file "https://example.com/archive.zip" --adaptive --max-concurrency 12 --per-host-concurrency 2
atlas file "https://example.com/archive.zip" --adaptive --explain --json
atlas file "https://example.com/archive.zip" --filename release.zip
atlas file "https://example.com/archive.zip" --checksum sha256:...
atlas file "https://example.com/release.meta4" --backend aria2
atlas file "https://example.com/archive.zip" --backend aria2 --lowest-speed-limit 32K --max-tries 5 --retry-wait 2
atlas file "https://example.com/archive.zip" --backend aria2 --save-session ~/Downloads/aria2.session
atlas file "https://example.com/release.meta4" --backend aria2 --metalink-preferred-protocol https --uri-selector adaptive
atlas file "https://example.com/archive.zip" --dry-run --json
```

Backend behavior:

- `auto`: probe HTTP metadata and choose native or aria2c.
- `native`: stdlib HTTP download with atlas Rich progress.
- `aria2`: short-lived localhost-only aria2c RPC process with structured progress.
- `wget2`: Wget2 single-file download using `--output-document`.

In `auto` mode, real downloads probe:

- `Content-Type`
- `Content-Length`
- `Content-Disposition` filename
- `Accept-Ranges`
- `ETag`
- `Last-Modified`
- HTTP `Link` headers that advertise Metalink resources
- redirect target
- file extension

Direct-file optimization:

| Condition | Backend |
| --- | --- |
| Large file, byte ranges supported, aria2c available | aria2c |
| Small file | native |
| aria2c missing or disabled | native |
| Unknown size | aria2c when enabled and available, otherwise native |

`auto` keeps Wget2 opt-in for file downloads. Select `--backend wget2` or set
`file_backend = "wget2"` when you want Wget2 for direct files.

If Python, aria2c, or Wget2 fail because a server omits an intermediate
certificate but system `curl` can verify the URL, Atlas retries that direct file
once through curl. This fallback keeps certificate verification on and is not the
same as `--no-check-certificate`.

Direct-file HTTP and output policy flags include `--filename`,
`--trust-server-names/--no-trust-server-names`,
`--content-disposition/--no-content-disposition`,
`--timestamping/--no-timestamping`,
`--use-server-timestamps/--no-use-server-timestamps`, `--overwrite`,
`--no-continue`, `--timeout`, `--connect-timeout`, `--rate-limit`,
`--user-agent`, `--header`, `--referer`, `--cache/--no-cache`,
`--compression`, `--no-compression`, `--method`, `--body-data`, `--body-file`,
`--load-cookies`, `--proxy`, `--http-user`, `--http-password`,
`--check-certificate/--no-check-certificate`, `--ca-certificate`,
`--ca-directory`, `--certificate`, `--private-key`, and `--secure-protocol`.

aria2-specific file controls include `--lowest-speed-limit`, `--max-tries`,
`--retry-wait`, `--file-allocation`, `--check-integrity/--no-check-integrity`,
`--metalink/--no-metalink`, `--force-metalink`, `--input-file`,
`--save-session`, `--save-session-interval`, `--metalink-preferred-protocol`,
`--metalink-language`, `--metalink-os`, `--metalink-location`,
`--metalink-base-uri`, `--metalink-enable-unique-protocol`,
`--server-stat-if`, `--server-stat-of`, `--server-stat-timeout`,
`--uri-selector`, `--remote-time/--no-remote-time`,
`--conditional-get/--no-conditional-get`, and
`--http-accept-gzip/--no-http-accept-gzip`.

Adaptive file planning uses the same probe data plus size classes:

| Size Class | Typical Behavior |
| --- | --- |
| Tiny/small | Native streaming, higher queue concurrency for batches. |
| Medium | Moderate queue concurrency with a few ranged aria2c segments. |
| Large/huge | Lower queue concurrency, ranged aria2c segments when available. |
| Unknown or no ranges | Conservative native fallback unless aria2c is explicitly selected; early progress totals can reclassify the transfer. |

The adaptive plan includes `queue_concurrency`, `per_host_concurrency`,
`per_file_segments`, `max_total_connections`, `max_per_host_connections`,
`max_active_postprocessors`, `backend`, `size_counts`, `bucket_counts`, host
counts, and safety notes. Each manifest item records URL, kind, host, estimated
size, range support, checksum metadata when known, recursion depth for mirrors,
selected backend, priority, bucket, and scheduler decision.

Every optimized command plan also includes a `SmartDownloadSession` with source,
detected kind, intent, session type, manifest, plan, customization,
scheduler policy, progress reporter, and final summary fields. This keeps
`video`, `audio`, `playlist`, `file`, `site`, `dir`, and `batch` on one planning
contract even though their typed options and backends differ. See
[Smart Sessions](smart-sessions.md) for the session presets and artifact model.

aria2 and wget2 transfer settings are per file, not global queue concurrency.
Checksums support `sha256`, `sha512`, `sha1`, and `md5` in the form
`algorithm:hex-digest`. Native downloads verify the saved file after download.
Wget2 downloads verify the saved file after download. aria2 plans pass checksum
verification to aria2c.

Native resume is representation-safe: Atlas binds a partial file to its saved
ETag or Last-Modified validator with `If-Range`, verifies the returned
`Content-Range` starts at the local byte count, and restarts cleanly when the
remote representation changed. Equal file size alone is never treated as proof
that a partial file is complete.

For direct files, aria2 progress is read from JSON-RPC `tellStatus`, not console
scraping. The RPC secret is random per run and redacted from dry-run output.
`.meta4` and `.metalink` URLs route as manifest downloads and expand through
aria2's `addMetalink` path unless `--no-metalink` is used to save the manifest
itself. aria2 policy controls include `--lowest-speed-limit`, `--max-tries`,
`--retry-wait`, `--connect-timeout`, `--file-allocation`, `--check-integrity`,
`--remote-time`, `--conditional-get`, and `--http-accept-gzip/--no-http-accept-gzip`.
Normal file URLs can also upgrade to Metalink expansion when the HTTP response
advertises a `.meta4` or `.metalink` resource through `Link: rel=describedby` or
`rel=duplicate`. Pass `--no-metalink` or select `--backend native` when you want
to save the original URL literally.

For direct files, Wget2 uses `--output-document` for the resolved file path and
maps common HTTP controls such as `--header`, `--referer`, `--method`,
`--body-data`, `--load-cookies`, `--proxy`, `--timeout`, `--connect-timeout`,
`--rate-limit`, `--connections`, and `--chunk-size`. Metalink expansion remains
aria2-only in Atlas; use `--no-metalink` with Wget2 to save a Metalink manifest
itself.

aria2 session and mirror controls:

```text
--input-file PATH
--save-session PATH
--save-session-interval SEC
--metalink-preferred-protocol none|http|https|ftp
--metalink-language LANG
--metalink-os OS
--metalink-location LOC
--metalink-base-uri URI
--metalink-enable-unique-protocol / --no-metalink-enable-unique-protocol
--server-stat-if PATH
--server-stat-of PATH
--server-stat-timeout SEC
--uri-selector inorder|feedback|adaptive
```

## `atlas site URL`

Mirror a website intentionally through wget2 or wget.

```bash
atlas site "https://example.com/docs/"
atlas site "http://textfiles.com/directory.html" \
  --convert-links --adjust-extension --page-requisites --no-parent \
  --domains textfiles.com,www.textfiles.com --wait 0.5 --random-wait \
  --timeout 60 --tries 5 --continue
atlas site "https://example.com/docs/" --depth 1
atlas site "https://example.com/docs/" --no-assets
atlas site "https://example.com/docs/" --span-hosts
atlas site "https://example.com/docs/" --same-domain-www
atlas site "https://example.com/docs/" --max-files 500 --adaptive --explain
atlas site "https://example.com/docs/" --max-total-size 5G --max-runtime 1800
atlas site "https://example.com/docs/" --accept html,css,png,jpg
atlas site from-file urls.txt --force-sitemap --base "https://example.com/"
atlas site "https://example.com/docs/" --warc-file archive.warc.gz
atlas site "https://example.com/docs/" --inet4-only --bind-address 127.0.0.1
atlas site "https://example.com/docs/" --follow-tags img/data-src --cut-url-get-vars
atlas site "https://example.com/private/" --cookies-from-browser safari
atlas site "https://example.com/releases/" --verify-sig fail --signature-extensions asc,sig
atlas site "https://example.com/docs/" --adaptive --max-concurrency 4 --per-host-concurrency 2
atlas site "https://example.com/docs/" --adaptive --explain --json
atlas site "https://example.com/docs/" --dry-run --json
```

Defaults are restrained:

- depth: `2`
- page requisites: enabled
- link conversion: enabled
- span hosts: disabled with `--no-span-hosts`; enable deliberately with
  `--span-hosts`
- parent directory ascent: disabled with `--no-parent`; `--parent` opts out
- wait: `1.0` second between requests
- partial-file resume: enabled with `--continue`
- overwrite: disabled unless `--overwrite` is passed

Use `atlas site` only for websites you are allowed to mirror and keep recursion
bounded.

Successful and failed non-dry-run site mirror attempts write stable session artifacts under
`<output>/.atlas/latest/`: `summary.json`, `manifest.json`, `failed.txt`,
`skipped.txt`, `canceled.txt`, and `retry.atlas.json`.

Wget2-oriented site controls include parser modes (`--input-file`, `from-file`,
`--base`, `--force-html`, `--force-css`, `--force-sitemap`, `--force-atom`,
`--force-rss`, `--force-metalink`), custom scanners (`--follow-tags`,
`--ignore-tags`), URL shaping (`--filter-urls`, `--filter-mime-type`,
`--robots/--no-robots`, `--follow-sitemaps/--no-follow-sitemaps`,
`--ignore-case/--case-sensitive`, `--cut-url-get-vars`,
`--cut-file-get-vars`, `--keep-extension`, `--convert-file-only`, `--unlink`),
path layout (`--directories/--no-directories`,
`--host-directories/--no-host-directories`,
`--protocol-directories/--no-protocol-directories`, `--cut-dirs`,
`--default-page`, `--download-attr`, `--restrict-file-names`, `--backups`,
`--backup-converted`), offline-copy toggles (`--assets/--no-assets`,
`--page-requisites/--no-page-requisites`, `--convert-links/--no-convert-links`,
`--adjust-extension`), resume/overwrite controls (`--continue/--no-continue`,
`--overwrite/--no-overwrite`), archival mode (`--warc-file`,
`--warc-compression/--no-warc-compression`, `--warc-cdx`, `--warc-max-size`),
authenticated mirror UX (`--cookies/--no-cookies`, `--cookies-from-browser`,
`--browser-cookies`, `--load-cookies`, `--save-cookies`,
`--keep-session-cookies`, `--cookie-suffixes`, `--netrc/--no-netrc`,
`--netrc-file`, `--http-user`, `--http-password`, `--proxy-user`,
`--proxy-password`), HTTP request controls (`--user-agent`, `--header`,
`--referer`, `--cache/--no-cache`, `--compression`, `--no-compression`,
`--method`, `--body-data`, `--body-file`, `--post-data`, `--post-file`),
signature verification (`--verify-sig`, `--signature-extensions`,
`--gnupg-homedir`, `--verify-save-failed`), transport controls
(`--hsts/--no-hsts`, `--hsts-file`, `--https-only`, `--https-enforce`,
`--inet4-only`, `--inet6-only`, `--bind-address`, `--bind-interface`,
`--prefer-family`, `--dns-cache/--no-dns-cache`, `--dns-cache-preload`,
`--tcp-fastopen/--no-tcp-fastopen`, `--proxy/--no-proxy`,
`--retry-connrefused`, `--retry-on-http-error`, `--start-pos`, `--quota`,
`--limit-rate`, `--wait`, `--random-wait/--no-random-wait`, `--waitretry`,
`--max-redirect`, `--timeout`, `--dns-timeout`, `--connect-timeout`,
`--read-timeout`, `--ignore-length`, `--content-on-error`,
`--save-content-on`, `--save-headers`, `--server-response`,
`--timestamping/--no-timestamping`, `--http2/--no-http2`, `--http2-only`,
`--http2-request-window`), TLS/OCSP controls (`--check-certificate`,
`--check-hostname/--no-check-hostname`, `--ca-certificate`, `--ca-directory`,
`--certificate`, `--certificate-type`, `--private-key`, `--private-key-type`,
`--crl-file`, `--secure-protocol`, `--ocsp/--no-ocsp`,
`--ocsp-date/--no-ocsp-date`, `--ocsp-nonce/--no-ocsp-nonce`,
`--ocsp-server`, `--ocsp-stapling/--no-ocsp-stapling`, `--ocsp-file`,
`--tls-resume/--no-tls-resume`, `--tls-session-file`,
`--tls-false-start/--no-tls-false-start`), and structured stats summaries
(`--stats/--no-stats`) for site/server/DNS/TLS/OCSP state.

Mirror scope presets are mutually exclusive:

- `--same-host-only`: exact seed host only.
- `--same-domain-www`: seed domain plus its `www` variant.
- `--include-subdomains`: domain-bounded host spanning.

Mirror bounds:

- `--max-files N`: adaptive scan guard; fails before execution when scan counts exceed the limit.
- `--max-total-size SIZE`: friendly alias for Wget2 quota.
- `--max-runtime SEC`: Atlas-enforced subprocess runtime cap.

`--adaptive` scans the starting page, applies the selected politeness profile,
and adds crawler queue guidance to the plan preview. Website plans and ordinary
HTML directory indexes are executed by Wget2/Wget. Signature-recognized
CopyParty indexes can resolve to Atlas's bounded `native-exact-index` path
instead, as described below.

## `atlas dir URL`

Mirror an explicit open HTTP directory index or file tree through wget2 or wget.
Use this for public archive-style listings and file trees, not normal websites.

```bash
atlas dir "https://example.com/files/"
atlas dir "https://example.com/files/" --depth 2
atlas dir "https://example.com/files/" --accept zip,7z,pdf,mp4
atlas dir "https://example.com/files/" --reject html,tmp
atlas dir "https://example.com/files/" --no-parent
atlas dir "https://example.com/files/" --same-host-only --max-files 500 --adaptive --explain
atlas dir "https://example.com/files/" --same-domain-www --max-total-size 5G
atlas dir "https://example.com/files/" --backend wget2
atlas dir "http://textfiles.com/directory.html" \
  --no-parent --domains textfiles.com,www.textfiles.com \
  --wait 0.5 --random-wait --timeout 60 --tries 5 --continue
atlas dir "https://example.com/files/" --adaptive --max-concurrency 4 --per-host-concurrency 2
atlas dir "https://example.com/files/" --adaptive --explain --json
atlas dir "https://example.com/files/" --dry-run --json
```

Defaults for recursive Wget2 directory plans are restrained:

- recursion is enabled only because `dir` was chosen explicitly
- Wget2 gets the directory mirror preset: `--recursive`, `--no-parent`,
  `--mirror`, `--continue`, `--timestamping`, `--no-if-modified-since`,
  `--directory-prefix=<output>`, and the directory user agent
- depth: `2`
- page requisites/assets: disabled
- link conversion: disabled
- span hosts: disabled
- parent directory ascent: disabled with `--no-parent`
- wait: `1.0` second between requests
- partial-file resume: enabled with `--continue`
- user agent: `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36`
- overwrite: disabled unless `--overwrite` is passed

The default Wget2 directory plan is equivalent to:

```bash
wget2 --recursive \
  --no-parent \
  --mirror \
  --continue \
  --timestamping \
  --no-if-modified-since \
  --directory-prefix="<output>" \
  --user-agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" \
  --level=2 \
  URL
```

Dry-run JSON includes `mirror_kind = "dir"`, the selected wget backend, bounded
depth, filters, scope/bounds, output directory, safety warnings, and backend
argv. If both `wget2` and `wget` are missing, atlas fails early with an
installation hint.

Adaptive directory mirrors report the crawler strategy, queue size, per-host cap,
politeness, discovered links from the starting index, and external-host safety
notes. This is useful for open archive directories where a dry run should show
whether Atlas sees a single-host bounded file tree before Wget2 starts.

Discovery supports conventional HTML indexes and signature-recognized
CopyParty HTML or plain-text/ANSI indexes. Ambiguous `text/plain` responses fail
with an explicit parse error. Responses capped at 512 KiB or 2,000 entries are
marked partial; Atlas does not present their counts or size estimates as
complete.

Before a real `atlas dir` download, a complete CopyParty scan is converted into
an exact same-host file list. Atlas applies depth and accept/reject filters,
refuses partial or mixed unsupported nested indexes, enforces `--max-files`, and
requires every selected size to be known when `--max-total-size` is set. The
native exact-index executor preserves relative paths, rejects traversal,
symlink escapes, and case-folded collisions, honors resume/timestamp/overwrite
and runtime bounds, and checks cancellation between files and during native
file progress. Exact-index transfer is currently sequential, one native file at
a time. Mirror `--wait` and `--random-wait` apply to recursive Wget2/Wget work,
not this exact native loop. Ordinary HTML trees remain recursive Wget2/Wget work.

Wget2 stats are parsed after mirror runs. Successful mirrors show URL counts,
failures, redirects, downloaded bytes, hosts, and DNS/TLS/OCSP summaries when
available. If Wget2 exits nonzero after partial work, Atlas still fails the
command but includes downloaded bytes and failed URL samples in the error.
Successful and failed non-dry-run directory mirror attempts write stable session artifacts under
`<output>/.atlas/latest/`: `summary.json`, `manifest.json`, `failed.txt`,
`skipped.txt`, `canceled.txt`, and `retry.atlas.json`. A failed Wget2 attempt
retains parsed stat samples in the displayed error, but the current recovery
artifact retries the mirror seed URL rather than individual failed stat rows.

## `atlas video URL`

Download a single video by default. Even if a YouTube watch URL contains
`list=...`, atlas keeps `noplaylist=true` unless an explicit playlist URL and
playlist mode are used.

YouTube channel/tab URLs are collections, not single videos. Atlas refuses them
unless playlist intent and a finite bound are both explicit:

```bash
atlas video "https://www.youtube.com/@example/videos" --playlist --playlist-items 1
atlas audio "https://www.youtube.com/@example/videos" --playlist --playlist-end 5
```

This bound is applied to both the metadata probe and the download. A collection
URL without `--playlist` and `--playlist-items` or `--playlist-end` fails before
yt-dlp can enumerate the channel.

For a bounded collection, the human plan preview shows the resolved yt-dlp
output template. It does not fabricate a concrete filename from
collection-level metadata. The selected-item summary also stays compact instead
of printing the entire format catalog; use `atlas formats` or the menu's
exact-format picker for that detail.

In the interactive menu, Atlas probes the URL before showing media choices. The
menu builds a format catalog, then recommends profiles such as Best quality,
Balanced, Apple compatible, Small file, Audio only, and Custom formats. Choices
that cannot work for the source are hidden or converted into explicit fallback
or conversion prompts. Commands below remain the scriptable layer for users who
already know the desired policy.

Basic use:

```bash
atlas video "https://www.youtube.com/watch?v=..."
```

Common intent options:

```bash
atlas video URL --quality max
atlas video URL --quality balanced --resolution 1080
atlas video URL --quality compatible
atlas video URL --quality small
```

Video-specific controls:

```text
--quality max|balanced|compatible|small
--container auto|mkv|mp4|webm
--resolution max|4320|2160|1440|1080|720|480
--video-codec auto|av1|vp9|h264|hevc
--hdr auto|prefer|avoid|only
--fps max|60|30
--format FORMAT
```

Selection, playlist, section, and SponsorBlock controls:

```text
--playlist
--playlist-items 1-10,15,20-
--organize flat|channel|channel-date|playlist
--filename-template TEMPLATE
--restrict-filenames
--match-filter FILTER
--match-filters FILTER
--break-match-filter FILTER
--break-match-filters FILTER
--max-downloads N
--break-on-existing
--no-break-on-existing
--break-on-reject
--no-break-on-reject
--break-per-input
--no-break-per-input
--date DATE
--date-before DATE
--datebefore DATE
--date-after DATE
--dateafter DATE
--min-filesize SIZE
--max-filesize SIZE
--reject-live
--allow-live
--reject-upcoming
--allow-upcoming
--live-from-start
--no-live-from-start
--download-section REGEX_OR_RANGE
--download-sections REGEX_OR_RANGE
--sponsorblock-mark CATEGORY
--sponsorblock-remove CATEGORY
--sponsorblock-chapter-title TEMPLATE
--sponsorblock-api URL
```

`--max-downloads N` is a successful requested cap after N distinct items finish;
it is not reported as a failed download merely because yt-dlp signals the stop.

`--match-filter`, `--break-match-filter`, `--download-section`,
`--sponsorblock-mark`, and `--sponsorblock-remove` are repeatable. Section time
ranges use yt-dlp syntax such as `*10:15-inf`; non-`*` values are chapter title
regexes. SponsorBlock categories include `sponsor`, `selfpromo`, `interaction`,
`intro`, `outro`, `preview`, `filler`, `music_offtopic`, and `hook`.

Metadata, subtitles, and chapter controls:

```text
--metadata / --no-metadata
--thumbnail / --no-thumbnail
--info-json / --no-info-json
--subs none|manual|auto|all
--sub-lang LANGS
--embed-subs / --no-embed-subs
--chapters / --no-chapters
--split-chapters
```

Sidecar and metadata-only controls:

```text
--skip-download
--subtitle-only
--thumbnail-only
--info-only
--skip-unavailable-fragments / --abort-unavailable-fragments
--source-address IP
--yes
```

`--subtitle-only`, `--thumbnail-only`, and `--info-only` are mutually exclusive
shortcuts. They imply `--skip-download` and disable impossible embed
postprocessors so a subtitle-only run does not try to merge media or embed a
thumbnail into a file that was never downloaded. `--skip-download` is the lower
level mode for advanced sidecar combinations such as `--info-json --thumbnail`.

Default behavior:

- Quality: `max`
- Format: `bestvideo*+bestaudio/best`
- Container: `auto`, which resolves to `mkv` for max quality
- Archive: enabled
- Info JSON: enabled
- Thumbnail and metadata: enabled
- Playlist: disabled

## `atlas audio URL`

Extract audio from a single URL.

The menu resolves available audio formats before asking about codec details.
`best` keeps source audio where possible. MP3, FLAC, and WAV are conversion
workflows and require ffmpeg; Atlas warns before conversion in interactive mode.

```bash
atlas audio URL
atlas audio URL --codec opus
atlas audio URL --codec m4a
atlas audio URL --codec mp3 --quality 0
```

Audio-specific controls:

```text
--codec best|opus|m4a|mp3|flac|wav
--quality 0-10
--format FORMAT
```

`atlas audio` supports the same selection, date/filesize, live, section, and
SponsorBlock controls as `atlas video`. It also supports `--skip-download`,
`--subtitle-only`, `--thumbnail-only`, and `--info-only` with the same
sidecar-only semantics. For audio extraction runs, transfer success is not final
success: Atlas keeps showing extract/finalize phases until ffmpeg finishes or
reports an Extract-phase error.

Default behavior:

- Format: `bestaudio/best`
- Postprocessor: `FFmpegExtractAudio`
- Codec: `best`
- Quality: `0`
- Archive: enabled
- Info JSON, thumbnail, artwork, metadata: enabled

## `atlas playlist URL`

Intentional playlist command. It accepts explicit playlist URLs only.

```bash
atlas playlist "https://www.youtube.com/playlist?list=..."
atlas playlist "https://www.youtube.com/playlist?list=..." --type video
atlas playlist "https://www.youtube.com/playlist?list=..." --type video --video-codec h264
atlas playlist "https://www.youtube.com/playlist?list=..." --type audio
atlas playlist "https://www.youtube.com/playlist?list=..." --type audio --codec mp3
```

If `--type` is omitted, atlas prompts:

```text
Download playlist as video or audio? [video]:
```

Use `--type` for automation.

Playlist controls:

```bash
--playlist-items 1-10,15,20-
--playlist-start 1
--playlist-end 25
```

Open-ended selections such as `20-` are valid for an explicit playlist URL,
but they are not a finite channel/tab collection bound. Collections require a
closed range such as `1-10`, explicit item numbers such as `1,4,7`, or a finite
`--playlist-end`.

In the interactive menu, playlist customization offers three item-selection
paths: all items, typed yt-dlp range/start-end values, or a checkbox list of
item numbers for quick multi-select sessions. The checkbox path writes the
selected numbers back to `playlist_items`, so the eventual backend plan remains
the same stable yt-dlp selection contract used by command mode.

Safety behavior:

- Explicit playlist URL plus `atlas playlist`: playlist allowed.
- Explicit playlist URL plus `atlas video --playlist` or `atlas audio --playlist`:
  playlist allowed.
- Watch URL with radio/list params: refused by `atlas playlist`.
- Watch URL with radio/list params plus `atlas video` or `atlas audio`: single item.
- Channel/tab collection plus `atlas video` or `atlas audio`: requires
  `--playlist` and a finite item/end bound.
- Removed, private, or unavailable download entries are skipped in effective
  playlist sessions; post-processing failures still fail.

## `atlas info URL`

Show a sanitized media card.

```bash
atlas info URL
atlas info URL --json
atlas info PLAYLIST_URL --playlist
atlas info URL --cookies-from-browser safari
atlas info URL --cookies-file ~/Downloads/cookies.txt
```

Default output is a readable card with:

- Title
- Uploader/channel
- Duration
- Source
- Upload date
- Views
- URL
- Playlist detection
- Best video and audio summaries

`--json` prints a machine-readable `MediaInfo` model. `--playlist` explicitly
permits extraction from a canonical playlist URL; without it, playlist
enumeration is disabled.
Collection/channel URLs are rejected here because `atlas info` has no finite
selection flags; use a bounded `atlas video|audio ... --dry-run --json` plan.

## `atlas formats URL`

Show available formats in a Rich table. Human output also includes the same
recommended media profiles used by the menu: available, fallback, and conversion
choices are labeled before the exact format table.

```bash
atlas formats URL
atlas formats URL --video-only
atlas formats URL --audio-only
atlas formats URL --sort quality
atlas formats PLAYLIST_URL --playlist
atlas formats URL --json
```

Sort choices are `quality`, `size`, and `codec`.

`--playlist` explicitly permits extraction from a canonical playlist URL.
Channel/tab collection URLs are rejected because `atlas formats` has no finite
selection flags; use a bounded `atlas video|audio ... --dry-run --json` plan.

Columns:

- ID
- Ext
- Resolution
- FPS
- VCodec
- ACodec
- Size
- TBR
- Note

`--json` honors filters and sorting. `--video-only` and `--audio-only` cannot be
used together.

## `atlas batch FILE`

Read URLs from a text file and route each item through the smart hub planner.
In the interactive menu, `Batch` starts with source choices: paste a
URL and scan it, use a URL file, paste multiple URLs, download a playlist as a
batch, resume a previous session, retry failed URLs, inspect a previous session,
or export URLs.
The URL-scan path can generate a same-host downloadable queue, let the user
choose discovered files through a checkbox multi-select, build a recursive
directory/site mirror from one seed URL, let the user choose a discovered folder,
or fall back to a single direct-file download.
When the scan sees visible open-directory folders, the menu switches to
Directory Explorer: show one compact browse card, preview visible folders, show
only a short root-file sample, and route users into actions such as
`Everything under this folder`, `Choose one specific folder`, `Choose multiple
folders`, `Only visible files at this level`, `Browse full folder tree first`,
or `Deep scan selected folders first` before building the final exact-list
batch or directory mirror queue.

```bash
atlas batch urls.txt
atlas batch urls.txt --kind auto
atlas batch urls.txt --concurrency 3
atlas batch urls.txt --adaptive --max-concurrency 12 --per-host-concurrency 2
atlas batch urls.txt --adaptive --explain --json
atlas batch urls.txt --dry-run
atlas batch urls.txt --json
atlas batch urls.txt --type audio
atlas batch urls.txt --type audio --codec mp3 --audio-quality 3
atlas batch urls.txt --type video
atlas batch urls.txt --type video --video-codec h264
atlas batch urls.txt --allow-sites
atlas batch urls.txt --allow-dirs
atlas batch urls.txt --kind file
atlas batch urls.txt --kind site --allow-sites
atlas batch urls.txt --kind dir --allow-dirs
```

Input rules:

- Blank lines are skipped.
- Lines beginning with `#` are skipped.
- Auto mode routes each URL independently.
- Media URLs use yt-dlp.
- Obvious direct-file URLs use native/aria2c file backends by default; Wget2 is
  available when explicitly selected.
- Possible website mirrors are skipped unless `--allow-sites` is passed.
- Possible open-directory mirrors are skipped unless `--allow-dirs` is passed.
- Batch continues after individual URL failures.
- The command exits nonzero when any item fails.
- `--concurrency N` controls ordinary batch URL concurrency. With `--adaptive`,
  mixed-engine execution uses the adaptive plan's queue value; an all-aria2
  shared RPC batch instead maps the explicit value to aria2's global queue.
- Direct-file duplicate basenames are disambiguated with path-scoped names.

`--type audio` and `--type video` keep the older explicit media-only behavior.
Use `--codec`/`--audio-quality` for audio batches and `--video-codec` for video
batches.

For direct-file batches, Atlas avoids silent overwrites. If two input URLs would
both save as `bestpractices.pdf`, Atlas preserves both files with scoped names
such as `academics__bestpractices.pdf` and `pamphlets__bestpractices.pdf`. A
line-number suffix is used only if the path-scoped name still collides.

JSON output is a `BatchSummary`. Each result may include a `plan` object showing
the routed kind, selected engine, preview output, safety notes, and backend args
for dry runs or skipped items.
Stable batch manifests also preserve `backend_args` and `backend_command` for
items whose saved plan included backend argv data. These values are redacted
before they are written, previewed, copied, or returned through JSON.

Non-dry-run batch commands also write durable artifacts under `<output>/.atlas/`.
Timestamped `batch-summary-*.json`, `batch-manifest-*.json`, and
`batch-retry-*.txt` files preserve run history. The stable newest session lives
under `<output>/.atlas/latest/`:

- `summary.json`
- `manifest.json`
- `failed.txt`
- `skipped.txt`
- `canceled.txt`
- `retry.atlas.json`

`retry.atlas.json` points at failed-only, checksum-failure-only,
skipped-unknown, canceled-only, save-manifest, load-manifest, export-failed, and
resume flows.

Atlas builds a complete private `latest/` generation before it publishes it, so
readers never see a mixed summary and manifest. Timestamped batch history uses
unique generation names. Atlas rejects symbolic-link `latest/` directories.

`total` is the full number of considered input lines, including skipped blank or
comment lines. `succeeded`, `failed`, and `skipped` always describe the final
batch outcome, and batch continues after individual item failures. `canceled`
is a reported subset of `skipped`; automation must not add the two values when
reconciling totals.

Batch concurrency is queue-level: it limits active URL items. Each item still
uses its own optimized plan, including
yt-dlp options for media and aria2 HTTP/HTTPS connections/splits for direct-file
downloads when aria2 is selected. The default comes from `batch_concurrency` in
config and is deliberately conservative. When every batch item resolves to a
direct-file or Metalink aria2 plan, atlas submits the whole batch to one local
aria2 RPC session and maps `--concurrency` to aria2's queue limit.

Adaptive batch mode probes direct files, scans allowed site/directory
candidates, and route-classifies media URLs without a media metadata probe. The
plan reports size counts, bucket counts, hosts,
queue concurrency, per-host concurrency, per-file segments, selected backend, and
safety notes. At runtime the batch scheduler enforces the adaptive queue and
per-host caps, while each item still carries its own native, aria2c, Wget2, site,
or yt-dlp media plan. Tiny/small direct files can run many URL slots with no
splitting; large ranged files use fewer URL slots with more per-item aria2
segments. Unknown-size items begin conservatively and are reclassified once a
backend progress event reports a total size.

For the mixed-engine executor, only rows that can start under the current
global, per-host, and operator-pause limits enter the executor. Paused or
host-capped rows remain pending, so they do not occupy worker threads needed by
another runnable host. The runtime API can attribute feedback to a resolved/CDN
host when a caller supplies a host resolver; the built-in CLI currently uses
the original item host. The all-aria2 shared RPC path queues every item into
aria2 immediately, enforces only its global adaptive queue limit, and does not
bind `BatchControl`, so the full-progress mutation keys are not advertised for
that path. Shared RPC batches change global queue and speed options only when
their effective values change, and reset that cache when the process stops.

If shared aria2 RPC startup fails, Atlas returns to ordinary per-item batch
execution. If RPC is lost after work starts, completed items stay complete,
unresolved items fail, and active GIDs are removed best-effort. Only TLS-chain
failures receive the verified-curl per-item retry; general mid-session RPC
failures do not automatically retry through the legacy aria2 subprocess.

In interactive full progress on the mixed-engine path, `x` and `X` can cancel queued rows and controlled
active work. Active mirror subprocesses are terminated; native files,
exact-index files, and media work stop cooperatively at progress or
postprocessor hook boundaries. Canceled rows are recorded distinctly and stay
eligible for resume or canceled-only retry.

Use exact-list batch downloads when you need every known file from an archive
page and recursive Wget2 traversal is confused by malformed HTML, broken links,
or mixed page/file anchors. The directory mirror remains the fastest path for
well-formed open indexes; the batch file path is the auditable path when the
input list is authoritative.

## Saved-session commands

Use saved-session commands after a batch, site mirror, or directory mirror has
written `.atlas` artifacts. Start with inspection, then choose the narrowest
recovery action.

### Inspect

`inspect-session` is read-only operator mode. It shows counts, scheduler policy,
item samples, failures, artifact paths, and redacted saved backend commands.

```bash
atlas inspect-session ~/Downloads/atlas
atlas inspect-session ~/Downloads/atlas --preview plan
atlas inspect-session ~/Downloads/atlas --preview errors --status failed
atlas inspect-session ~/Downloads/atlas --panel failed
atlas inspect-session ~/Downloads/atlas --filter checksum --limit 20
atlas inspect-session ~/Downloads/atlas --status failed --export-urls failed-filtered.txt
atlas inspect-session ~/Downloads/atlas --copy-command resume
atlas inspect-session ~/Downloads/atlas --open-output
atlas inspect-session ~/Downloads/atlas --json
```

Preview choices are `plan`, `backend`, `manifest`, `summary`, `retry`, `failed`,
`errors`, `logs`, and `config`. Panel choices are `overview`, `queue`, `active`,
`completed`, `canceled`, `failed`, `scheduler`, `logs`, and `summary`.

Filters do not change the saved session. `--export-urls PATH` exports every URL
in the filtered view, ignoring the display-only `--limit`. Exports refuse to
replace existing files or session artifacts unless `--force` is explicit.
`--open-output` uses the macOS `open` command and is currently macOS-only.

### Resume and retry

```bash
atlas resume
atlas resume ~/Downloads/atlas
atlas retry ~/Downloads/atlas --failed-only
atlas retry ~/Downloads/atlas --checksum-failures-only
atlas retry ~/Downloads/atlas --skipped-unknowns-only
atlas retry ~/Downloads/atlas --canceled-only
atlas retry ~/Downloads/atlas --dry-run --json
```

`resume` includes failed URLs, skipped unknown-route URLs, and canceled URLs,
including both queued cancellations and controllable active cancellations.
`retry` defaults to failed items and can narrow to one failure class. Both reuse
the normal batch execution path so routing, progress, and new artifacts stay
consistent.

### Export retryable URLs

```bash
atlas export-failed ~/Downloads/atlas --output failed.txt
atlas export-failed ~/Downloads/atlas --canceled-only --output canceled.txt
```

The `SESSION` argument can be `retry.atlas.json`, `manifest.json`,
`.atlas/latest`, or the original output directory. When omitted, Atlas uses the
configured output directory's latest session. Saved-session commands accept
`--session-output-dir` to resolve a different latest-session root.

> [!IMPORTANT]
> Atlas rejects symbolic-link session files and linked manifests outside the
> owning session area. Embedded output paths cannot redirect retries or Finder
> actions outside the owning `.atlas` output boundary.

## `atlas doctor`

Check local setup.

```bash
atlas doctor
atlas doctor --fix
atlas doctor --fix --yes
atlas doctor --fix --no-install
atlas doctor --network
atlas doctor --network --fix-certs
atlas doctor --json
```

Checks include:

- Python version
- atlas package version
- Python SSL, CA bundle, and HTTPS verification
- yt-dlp import/version
- mutagen artwork-embedding support
- ffmpeg
- ffprobe
- aria2c
- aria2 JSON-RPC support
- wget2 and useful wget2 feature flags
- wget
- config/data/cache/log/output directories
- browser cookie support
- optional yt-dlp impersonation support

Every mode performs one verified HTTPS GET to `https://www.python.org/` with a
three-second timeout for the HTTPS verification row. Default human mode creates
missing Atlas directories and performs temporary write probes. `--json` and
`--fix --no-install` use non-mutating path checks, but they still perform that
network probe and therefore are not offline modes.

`--network` filters the report to Python/SSL/CA/HTTPS checks and exits nonzero
when any network check fails. `--fix-certs` prints safe certificate repair
guidance without disabling TLS verification or silently changing system trust.

Exit status is nonzero when required dependencies are missing. Missing optional
backends are reported with install hints but do not fail the overall required
dependency check.

`--fix` builds the same full-runtime repair plan as `atlas setup --full`.
It prints the plan by default, runs install commands only with confirmation or
`--yes`, and keeps `--no-install` and JSON output non-mutating.

## `atlas config`

```bash
atlas config path
atlas config show
```

`config path` prints only the config file path.

`config show` prints the resolved settings in TOML-like form.

## Common cross-command options

Global UI options are accepted before the subcommand:

```bash
atlas --theme high-contrast --plain --no-unicode --no-animation COMMAND ...
```

| Option | Behavior |
| --- | --- |
| `--version` | Print the installed Atlas version and exit. |
| `--theme auto|dark|light|high-contrast` | Select the named Rich palette for human output. |
| `--plain` | Disable color and Unicode for simple terminals, logs, or screenshots. |
| `--no-unicode` | Keep color but use ASCII boxes, icons, and progress bars. |
| `--no-animation` | Keep color/Unicode but disable shimmer, moving pulse bars, spinners, and activity frames. |
| `--no-menu` | Show help instead of launching the interactive no-argument menu. |

Frequently shared download options are listed below. Exact availability depends
on the command family; use `atlas COMMAND --help` as the executable source of
truth.

```text
--output-dir PATH
--archive PATH
--no-archive
--cookies-from-browser safari|chrome|firefox|brave|edge
--cookies-file PATH
--dry-run
--json
--quiet
--progress auto|compact|full|json|none
--verbose
```

| Family | Typical shared controls |
| --- | --- |
| Media | Output, archive, browser cookies, dry run, JSON, progress, and network retry controls. |
| Direct file | Output, backend, checksum, headers, rate limits, TLS, dry run, JSON, and progress. |
| Site and directory | Output, backend, recursion scope, bounds, politeness, cookies, TLS, dry run, JSON, and progress. |
| Batch | Kind, concurrency, mirror permissions, adaptive policy, dry run, JSON, and progress. |
| Saved sessions | Session root, retry selector, output override, dry run, JSON, and progress where execution occurs. |

Progress modes:

| Mode | Behavior |
| --- | --- |
| `auto` | Rich progress on a TTY; no live progress mixed into JSON output. |
| `compact` | Restrained Atlas card, colored stacked semantic bars, and active table. |
| `full` | Adds scheduler diagnostics while keeping the same colored card/bar/table structure. |
| `json` | Emits newline-delimited progress events; adaptive events include queue, per-host, segment, bucket, backend, priority, and scheduler-decision fields when known. |
| `none` | Disables live progress. |

Interactive Rich progress can use the terminal alternate screen for a dashboard
feel. `--progress json`, `--progress none`, `--json`, `--plain`, and non-TTY
script paths stay inline and machine-safe.

Network controls:

```text
--retries 10
--fragment-retries 10
--rate-limit 5M
--sleep 1
--proxy URL
```

Authorized access and politeness controls:

| Need | Atlas controls |
| --- | --- |
| Own logged-in browser session | `--cookies-from-browser safari|chrome|firefox|brave|edge` |
| Exported cookies | `--cookies-file PATH` or site `--load-cookies PATH` |
| Normal request context | file/site `--user-agent`, `--referer`, `--header "Name: value"` |
| Courteous transfer speed | media/file `--rate-limit`; site `--limit-rate`, `--wait`, `--random-wait` |
| Authorized network route | media/file `--proxy URL`; site proxy options |
| yt-dlp browser profile compatibility | media `--impersonate chrome` with `curl_cffi` installed |

Atlas does not provide fake browser fingerprinting, credential/session theft,
browser automation for defeating bot challenges, or DRM circumvention.

Downloader controls:

```text
--download-engine auto|native|aria2
--aria2 / --no-aria2
--connections 16
--splits 16
--chunk-size 1M
--concurrent-fragments 4
--file-access-retries 3
--retry-sleep http:1
--throttled-rate 64K
--http-chunk-size 10M
--socket-timeout 12
--impersonate chrome
--extractor-args youtube:player_client=android
```

## Related

- [Quick start](quick-start.md) for the shortest first-success path.
- [Configuration](configuration.md) for persistent defaults.
- [Smart sessions](smart-sessions.md) for lifecycle and artifact concepts.
- [Troubleshooting](troubleshooting.md) for symptom-based recovery.
- [Documentation home](README.md) for the complete guide map.
