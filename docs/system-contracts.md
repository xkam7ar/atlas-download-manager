# System Contracts

This document is the implementation contract for Atlas. It is intentionally
more prescriptive than the architecture overview: if code changes move a
boundary, change a durable artifact, or alter a user-facing flow, update this
file in the same patch.

## Prime Directive

Every download is a smart session.

```text
input
  -> detect / scan / probe
  -> classify
  -> plan
  -> customize
  -> execute
  -> report progress
  -> summarize and save artifacts
```

The interactive menu is the primary human interface. Commands are the
scriptable/automation layer underneath it. Commands and menu branches can choose
different presets, but they should not invent separate products. `video`,
`audio`, `playlist`, `file`, `site`, `dir`, `get`, `batch`, retry/resume, and
saved-session inspection all share the same planning, progress, and artifact
vocabulary.

## Front Doors

Atlas has three user-facing entry surfaces:

| Surface | Contract |
| --- | --- |
| Interactive menu | Primary human UX. Prompt for minimum input, build the same typed options, show the same plan preview, and call the same execution helpers. |
| Explicit commands | Scriptable API. Convert flags into typed options, build a hub plan, preview or execute it. |
| `atlas get` / Smart download | Route by intent and URL evidence, then let `DownloadOptimizer` choose typed options. |

Menu action flow:

```text
menu action
  -> typed options / HubRequest
  -> EngineRouter
  -> DownloadOptimizer
  -> SmartDownloadSession
  -> plan preview
  -> customize
  -> execute
  -> progress
  -> summary / artifacts
```

The menu must expose every normal operator capability that exists as a command.
The current menu capability registry covers:

- smart auto-detect
- video
- audio
- direct file
- explicit playlist
- website mirror
- directory/open-index mirror
- batch creation
- resume session
- retry failed
- inspect session
- export failed URLs
- info
- formats
- advanced backend command, grouping `ytdlp`, `aria2`, `wget2`, and `wget`
- doctor
- setup/install tools
- update Atlas
- config
- shortcuts/help

Menu Parity Rule: every normal operator workflow must be reachable from the
interactive menu. A command may be script-only only when it is explicitly marked
as such in tests and documentation. Advanced raw backend flag coverage is
reachable through `Tools` -> `Advanced backend` instead of being part of the
normal download UX. The forced `atlas menu` launcher is a front-door
helper, not a separate operator capability.

No-argument `atlas` is the primary human launcher, not command help. In a real
interactive terminal it must render the polished launcher, with a setup gate
first when required media tools are missing or when first-run full-runtime checks
find missing tools. The setup gate may offer install, plan-only, limited-mode,
Doctor, and quit actions, but it must never run package-manager commands without
an explicit install/setup action and confirmation. `atlas --help`, `--no-menu`,
JSON, non-TTY output, and automation environments remain command-oriented.
Atlas-owned interactive screens should behave like redrawable terminal-app
surfaces rather than stacked questionnaire history. In real TTY mode Atlas may
use an alternate screen and clear/redraw between menu states so prompt echoes
do not pile up above the current state. Plain, no-color, no-unicode, and
non-interactive paths remain inline.

## Installation Contract

Atlas must provide a one-command guided installer for normal users on macOS,
Debian/Ubuntu, Fedora, and Arch-family Linux. Runtime packages use Homebrew,
apt, dnf, or pacman as appropriate; Arch uses Homebrew on Linux for `wget2`.

Target install paths:

```bash
brew install xkam7ar/tap/atlas
uv tool install "git+https://github.com/xkam7ar/atlas.git@COMMIT_ID"
atlas setup
```

Repository visibility establishes a public-source preview, not a release
channel. The unqualified PyPI and Homebrew `atlas` names belong to unrelated
projects. Until a collision-free release identity and immutable release exist,
the supported working path is a local checkout via `uv tool install . --force`;
`install.sh --no-install` is safe for plan review. A release installer must be
downloaded with its checksum manifest from the same immutable release,
verified, inspected, and invoked with `--release-ref`. Executing the copy on
`main` is never a supported install path.

The installer/setup layer must:

- detect OS, architecture, shell, package manager, and likely install method
- install or verify required media tools: `ffmpeg` and `ffprobe`
- install or verify full-runtime tools in full mode: `aria2c`, `wget2`, `wget`
- install Atlas with its declared Python dependencies, including `yt-dlp` and
  `mutagen` for audio artwork embedding
- create config, data, cache, log, and output directories
- run `atlas doctor` before declaring guided installer success
- never install system packages during Python import or package installation
- install Homebrew only after showing its bootstrap in the approved plan
- show every package-manager and bootstrap command before running it
- keep `--no-install` and JSON planning output non-mutating
- allow `install.sh --no-install` to report every missing prerequisite without
  attempting installation
- block guided uv installation before mutation unless `--release-ref` is the
  verified release's full 40-character commit ID
- block uv-tool updates without the same explicit release ref; never install or
  update Atlas from `main`, `master`, `HEAD`, or another mutable branch
- verify an existing `atlas` command supports `atlas setup` before treating it
  as a complete installation
- use the detected package-manager executable path in generated install commands
- require root or `sudo` for native Linux changes and print exact manual commands
  when elevation is unavailable
- keep `atlas setup`, `atlas doctor --fix`, and `atlas update` reachable from the menu

The guided installer must finish planning before its first mutation:

```text
detect host and missing executables
  -> build Atlas/runtime/bootstrap plan
  -> show the complete plan
  -> obtain one Atlas-level approval (`--yes` supplies it)
  -> run every approved bootstrap/package/Atlas step in order
  -> run `atlas setup --MODE` to initialize paths and configuration
  -> run `atlas setup --MODE --no-install` for final plan-only verification
  -> run `atlas doctor`
  -> report success only when required checks pass
```

OS authentication may still ask for a sudo password. A failed command stops the
sequence. A second run omits tools that are already present.

## Core Data Flow

```text
Typer command / menu action
  -> Pydantic options or HubRequest
  -> EngineRouter
  -> DownloadOptimizer
  -> SmartDownloadSession
  -> backend-specific plan
  -> adapter
  -> backend
  -> ProgressEvent
  -> reporter / JSON stream
  -> DownloadResult / BatchSummary
```

Important rule: `DownloadOptimizer` owns the optimized plan preview. User-facing
commands should not build raw backend argv or yt-dlp dictionaries directly.

Media rule: normal interactive media choices are catalog-backed. Atlas must
probe first, recommend available profiles, hide impossible choices, and require
explicit confirmation before ffmpeg conversion or re-encoding. Raw `--format`
selectors remain scriptable/advanced escape hatches.

Scan-state rule: URL scans must be one of `success`, `partial`, `failed`, or
`empty`. A transport, TLS, timeout, HTTP, or parse failure is never rendered as
an empty success. A successful fetch with no links is `empty` with `no_links`.
Menu actions must be derived from the scan state; discovered-file actions are
valid only when accepted downloadable file URLs exist.

Directory Explorer rule: directory-like URLs must map visible folders/files
before Atlas asks for recursive behavior. The fast root map is stage 1. Deep
scanning selected roots is stage 2. Selected-root deep scans must either build
an exact-list batch session or an explicit directory session; Atlas must not
jump straight to a recursive mirror when it already knows the exact selected
scope. The stage-1 browse screen must use one compact context card with `Seed`,
`Scope`, `Visible`, and `Estimated`; grouped `Folders`, `Files at this level`,
and optional `Warnings` sections; a preview-only root-file sample; and only
valid actions for the current state. The full visible root-file list belongs in
a searchable picker opened by `Only visible files at this level`, not in the
primary browse screen.

Exact-index rule: a complete signature-recognized CopyParty HTML or text index
may become a `native-exact-index` download. Atlas must keep the walk same-host,
apply depth and file filters, refuse partial/unsupported nested indexes, enforce
the 2,000-file hard cap plus user file/size/runtime bounds, and validate every
relative destination against traversal, symlink escape, and case-folded
collision before transfer. Conventional HTML indexes stay on explicit recursive
Wget2/Wget policy.

## Ownership Boundaries

| Layer | Owns | Must not own |
| --- | --- | --- |
| `cli.py` | Typer wiring, concise errors, command/menu action adapters, final command dispatch. | Backend internals or raw progress parsing. |
| `menu.py` | Searchable prompts, focused overlays, prompt-to-options mapping, menu-only generated queues. | Backend execution or command-specific subprocess logic. |
| `models.py` | Typed request, plan, event, and result contracts. | UI rendering or live I/O. |
| `hub.py` | Conservative URL-to-kind routing. | Download execution. |
| `optimizer.py` | Route-to-options optimization, probes/scans for explain/adaptive, preview construction. | Rich rendering or subprocess management. |
| `network.py` | Shared verified GET/HEAD fetches, TLS/CA handling, typed fetch failures, safe curl fallback for scan reads. | Download execution, planner policy, or UI rendering. |
| `directory_parser.py` | Directory-index parsing across supported HTML/listing shapes. | Terminal prompting, execution, or backend recursion. |
| `directory_scanner.py` | Typed scan status/error results for directory-style scans. | Prompt branching or backend invocation. |
| `directory_tree.py` | Visible folder-tree modeling for directory exploration. | Deep network recursion or execution. |
| `directory_explorer.py` | First-class directory action validity rules before download. | Download work, subprocess control, or planner mutation. |
| `directory_index.py` | Open-directory row parsing, visible folder/file metadata, and explorer-ready directory models. | Backend execution or terminal prompting. |
| `media_capabilities.py` | Probe-driven media catalogs, recommended profiles, exact format choices, and conversion warnings. | Download execution, Rich rendering, or silent transcoding. |
| `sessions.py` | `SmartDownloadSession` presets and artifact expectations. | Backend-specific stdout or UI state. |
| `planner.py` / `presets.py` | Media intent normalization and yt-dlp option translation. | Terminal output. |
| `adapters.py` | Stable adapter boundary for media, file, site, and directory execution. | Human UI decisions. |
| `backends.py` / `aria2_rpc.py` | Direct-file and mirror backend planning/execution, including verified curl retry after eligible TLS issuer-chain failures. | Rich layout, menu behavior, or silent certificate downgrades. |
| `batch.py` | Queue execution, item ordering, continue-after-failure aggregation, pause/cancel gates. | Per-backend transfer implementation. |
| `progress_events.py` | UI-free backend event normalization into `ProgressEvent`. | Rich layout, colors, or menu behavior. |
| `progress.py` | Live progress policy and human/JSON progress reporters. | Backend execution or planner decisions. |
| `views.py` / `theme.py` | Shared visual components, styles, glyphs, plain/NO_COLOR fallbacks. | Backend or planner state mutation. |
| `runner.py` | Safe subprocess execution and cancellation. | Shell-string execution or UI policy. |
| `setup.py` / `doctor.py` | Host detection, package plans, approved plan execution, and verification reports. | Unapproved package-manager mutation. |
| `config.py` / `paths.py` | Effective settings and `platformdirs`-based host paths. | Download planning or UI policy. |
| `redaction.py` / `private_files.py` | Shared secret removal and owner-only atomic artifact writes. | Session semantics or backend routing. |

## SmartDownloadSession Contract

`SmartDownloadSession` is the shared envelope for every optimized mode:

| Field | Contract |
| --- | --- |
| `source` | Original URL, file, or seed source. |
| `detected_kind` | Routed `HubKind`. |
| `intent` | User-level goal. |
| `session_type` | Stable preset label such as `single_video`, `direct_file`, or `batch_session`. |
| `manifest` | Known `WorkItem` rows before execution. |
| `plan` | Adaptive or preset `AdaptiveDownloadPlan`. |
| `customization` | User-facing option state. |
| `scheduler_policy` | JSON-stable scheduler summary. |
| `progress_reporter` | Expected human progress surface. |
| `final_summary` | Expected completion/artifact shape. |

The session is planner-owned. Backends receive typed options and callbacks; they
do not decide session semantics.

## Core Schema Field Groups

The field groups below are the stable vocabulary shared by manifests, dry-run
plans, JSON progress, and saved-session inspection. New fields can be added, but
renames and semantic changes are automation-affecting and must update this
contract.

`WorkItem` records should keep these groups clear:

- identity and routing: `url`, `host`, `final_url`, `final_host`,
  `redirect_target`, `kind`, `filename`, `same_host`, `external_host`,
  `url_fingerprint`, `mirror_fingerprint`
- HTTP/probe evidence: `content_type`, `content_length`,
  `content_disposition`, `content_disposition_filename`, `file_extension`,
  `accept_ranges`, `supports_ranges`, `etag`, `last_modified`,
  `checksum_metadata`, `probed`, `error`
- discovery and scan context: `discovered_links`, `discovered_work_items`,
  `sitemap_urls`, `robots_url`, `scan_type`, `scan_recommended_mode`,
  `scan_recommended_strategy`, `scan_counts`, `scan_estimated_bytes`,
  `scan_warnings`, `classification_notes`, `warning_flags`
- open-directory evidence: directory-index rows should be preserved when
  available as child `WorkItem` records with folder/file kind, parent-directory
  skip markers, visible size, modified time, and same-host/no-parent scope
  decisions
- Wget2 directory mirrors must include the directory baseline by default:
  `--recursive`, `--no-parent`, `--mirror`, `--continue`, `--timestamping`,
  `--no-if-modified-since`, `--directory-prefix=<output>`, and the configured
  directory user agent. The configured depth bound must be emitted after the
  mirror preset so `--mirror` cannot accidentally widen the default scope.
- scheduler inputs: `size_class`, `bucket`, `selected_backend`, `priority`,
  `recursion_depth`, `scheduler_decision`

`AdaptiveDownloadPlan` records should keep:

- policy and strategy: `enabled`, `politeness`, `backend`, `strategy`
- concurrency budgets: `global_min_concurrency`, `global_max_concurrency`,
  `queue_concurrency`, `per_host_concurrency`, `per_file_segments`,
  `per_file_segment_cap`, `max_active_files`, `max_total_connections`,
  `max_per_host_connections`, `max_active_postprocessors`,
  `max_disk_write_bytes_per_sec`, `speed_limit`
- summary evidence: `size_counts`, `bucket_counts`, `hosts`, `work_items`,
  `safety_notes`

`ProgressEvent` records should keep:

- identity and phase: `engine`, `status`, `phase`, `kind`, `filename`,
  `title`, `url`, `item_id`, `line_no`, `message`
- transfer and totals: `downloaded_bytes`, `total_bytes`, `estimated_bytes`,
  `fragment_index`, `fragment_count`, `files_done`, `files_total`, `percent`,
  `speed_bytes_per_sec`, `eta_seconds`
- adaptive scheduler state: `retry_count`, `active_connections`,
  `queue_concurrency`, `per_host_concurrency`, `per_file_segments`,
  `max_total_connections`, `max_per_host_connections`,
  `max_active_postprocessors`, `priority`, `recursion_depth`, `size_class`,
  `work_bucket`, `selected_backend`, `scheduler_decision`, `speed_limit`,
  `reclassified_from`
- backend-specific normalized detail: `backend_id`, `error_code`,
  `verified_bytes`, `verification_pending`, `piece_length`, `piece_count`,
  `bitfield`, `followed_by`, `following`, `belongs_to`, `backend_files`

## Adaptive Scheduler Contract

Adaptive planning is three-level for the mixed-engine executor:

- Queue concurrency controls how many URLs/items run at once.
- Host budgets cap how much work can hit one host at the same time.
- Per-item settings control one backend transfer, such as aria2 segments or
  Wget2 threads.

The hard invariant is:

```text
active_items * per_item_segments <= max_total_connections
host_active_items * per_item_segments <= max_per_host_connections
```

`--max-concurrency` remains a queue cap. Atlas must not silently reinterpret it
as a socket cap. The scheduler derives and records a separate
`max_total_connections` budget from the selected politeness profile and clamps
queue and segment choices so the plan cannot accidentally create hundreds of
sockets by combining high batch concurrency with high per-item splitting.
For `atlas batch --adaptive`, mixed-engine execution uses the adaptive plan's
queue value even when `--concurrency` is present. An all-aria2 shared RPC batch
instead maps explicit `--concurrency` to aria2's global queue.

`AdaptiveDownloadPlan` must preserve:

- queue concurrency
- per-host concurrency
- per-file segments
- per-file segment cap
- global min/max concurrency
- max active files
- total connection cap
- per-host connection cap
- postprocessor cap
- disk-write pressure cap
- speed limit
- backend preference
- size and bucket counts
- host counts
- work-item decisions
- safety notes

Runtime scheduling is evidence-driven. Backends feed normalized
`ProgressEvent` samples into the scheduler with speed, active connections,
retries, status, backend, host, bucket, and downloaded bytes. The scheduler keeps
host-level EWMA speed, active connection counts, retry/error evidence, current
host cap, and the latest explainable scheduler decision. It uses AIMD behavior:

- additive increases after stable low-error samples
- multiplicative decreases for 429/503/403, timeouts, retry spikes, speed
  collapse, disk pressure, CPU pressure, or postprocess pressure
- host-scoped backoff where possible so one unhealthy host does not unnecessarily
  punish unrelated hosts

Only runnable rows may enter the mixed-engine executor. Host-capped or
operator-paused work must remain pending rather than consuming worker threads.
When a verified probe
resolves an item to a different host, the runtime API may attribute feedback to
that final/CDN host if its caller supplies a host resolver; the built-in CLI
currently uses the original item host.

The all-aria2 shared RPC path queues every item immediately and enforces only
aria2's global adaptive queue. It binds no `BatchControl`, so per-host gating and
pause/cancel mutation keys do not apply. Shared sessions must avoid redundant
`changeGlobalOption` calls and clear their option cache when the process stops.
Startup failure returns to ordinary per-item execution. Mid-session loss keeps
completed results, fails unresolved items, and removes active GIDs best-effort;
only TLS-chain failures receive the verified-curl per-item retry.

Unknown-size items start cautiously. If progress later reports enough bytes to
classify an item, runtime code can clamp future starts and annotate progress
events with the reclassification. It should not pretend earlier unknown totals
had a percent.

## Fetch And Scan Contract

All scan/probe code must go through the shared fetch layer instead of ad hoc
`urlopen` calls.

`FetchClient` and scan services must preserve:

- TLS verification on by default
- CA-bundle-aware SSL setup
- typed fetch failures for TLS, timeout, connection, and HTTP errors
- safe, explicit fallback scanning through installed tools such as `curl` only
  when Python TLS verification fails and the fallback can still fetch safely
- no silent downgrade to `--no-check-certificate`

Doctor network diagnostics must inspect the same Python SSL and CA-bundle state
used by the fetch layer so scan failures and doctor output stay aligned. Every
Doctor mode performs one verified GET to `https://www.python.org/` with a
three-second timeout. Default human mode may initialize Atlas directories and
uses temporary write probes; JSON and `--fix --no-install` use non-mutating path
checks but are not offline modes.

Direct-file execution may use a separate verified curl retry from
`FileDownloadEngine` when Python, aria2c, or Wget2 fail on an issuer-chain error
but system curl can verify the same URL. That retry remains a download backend
concern, keeps certificate verification enabled, and must not be confused with
scan-fetch fallback or `--no-check-certificate`.

## Progress Contract

Backends emit `ProgressEvent`; reporters render it.

Human progress must:

- show phase state, not raw backend spam
- separate transfer from postprocessing
- avoid success until finalize/postprocessing is complete
- never fake a percent for unknown totals
- render tiny nonzero warning/error ratios as `<1%` with a visible sliver
- throttle live rendering to avoid terminal churn
- switch live batch tables to compact rows below 110 columns and stacked rows
  below 64 while retaining speed and ETA at very narrow widths
- budget rows against terminal height; keep the focused row first, then active,
  retry/backoff, failed, and recent rows, and report omitted state counts in a
  `+N hidden (...)` summary
- preserve plain/NO_COLOR/no-unicode fallbacks

Machine progress must:

- use JSON or NDJSON only
- never include Rich markup, ANSI control codes, tables, or human cards
- keep field names stable for automation

Final `--json` result mode has precedence over every `--progress` value and
emits exactly one JSON document. NDJSON is available only through
`--progress json` when final JSON mode is not active. Parser failures in either
machine mode must use the selected protocol, dry runs emit one terminal event,
and batch streams end with an aggregate `batch_summary` terminal event carrying
status and exit code.

Direct-file progress uses file-specific next phases such as verify/finalize.
Media progress derives its steps from the resolved plan and must not display
disabled merge, metadata, subtitle, thumbnail, or artwork stages. Mirror
progress uses discovery/mirror/verify/finalize language.

## Artifact Contract

Non-dry-run batch sessions write timestamped history plus stable latest files:

```text
<output>/.atlas/
  batch-summary-*.json
  batch-manifest-*.json
  batch-retry-*.txt
  latest/
    summary.json
    manifest.json
    failed.txt
    skipped.txt
    canceled.txt
    retry.atlas.json
```

Successful and failed non-dry-run standalone site and directory mirror attempts
write the stable `latest/` files. They do not create batch history files unless
they are running as batch items. A failed Wget2 exception can retain failed-row
samples for its displayed error, while the current saved recovery item is the
mirror seed URL.

Artifact rules:

- `summary.json` is the compact result view.
- `manifest.json` is the durable item/session view.
- `failed.txt`, `skipped.txt`, and `canceled.txt` exist even when empty.
- `canceled` is a subset of batch `skipped` accounting and must not be added to
  it when reconciling `total`.
- `retry.atlas.json` points to retry, resume, export, save, and load flows.
- Saved backend argv must be redacted before entering manifests, previews,
  copy actions, or JSON reports.
- Retry URL lists may retain the original signed URL required for recovery.
  They must be owner-only, atomic operational artifacts and must never be
  described as sanitized for sharing.
- Retry/resume/export commands load saved artifacts and then reuse the normal
  batch execution path.

## Cancellation And Operator Controls

Queue controls and active-work controls are separate but coordinated.

On the mixed-engine executor, `BatchControl` can pause global starts, pause one
host, pause one queued line, cancel queued work, and forward cancellation to the
`ProcessControl` registered for an active row. It does not freeze an
already-running transfer.

`ProcessControl` is the per-item cancellation signal. For Wget2/Wget and other
controlled subprocesses, `runner.py` terminates the child. Native direct files,
exact-index work, and yt-dlp media check the same signal at transfer progress,
between exact-index files, and at media progress/postprocessor hooks. These
in-process paths are cooperative: they must stop at the next checkpoint and map
the outcome to `DownloadStatus.canceled`, but they must not claim arbitrary
mid-instruction suspension.

Both queued and active-controlled cancellations are written to `canceled.txt`
and are eligible for `resume`, `retry --canceled-only`, and canceled-only export.

The mixed-engine full-progress batch UI owns key interpretation. The shared
aria2 RPC path does not advertise these mutation keys:

| Key | Contract |
| --- | --- |
| `g` | Pause or resume new queue starts. |
| `h` | Pause or resume focused host starts. |
| `s` | Pause or resume focused queued line. |
| `x` | Cancel focused queued or controllable active item. |
| `X` | Cancel queued work and every controlled active item. |
| `tab` | Cycle queue, active, completed, failed, scheduler, logs, summary panels. |
| `?` | Toggle help overlay. |

JSON, plain/script, compact non-interactive, and non-TTY modes must not start a
key reader.

## Safety Contract

Atlas supports normal authorized access and polite compatibility controls:

- user-authorized browser cookies
- cookie files
- user agents
- headers and referrers
- proxies
- waits, random waits, rate limits, retry/backoff controls
- yt-dlp supported impersonation profiles

Native direct-download credentials and sensitive request context must be
initial-origin only. Recursive mirror request bodies and secret-bearing custom
headers/referrers are rejected; site Basic authentication must not fall back to
GNU Wget. WARC/session/cookie material is staged privately and published only
through symlink-safe owner-only file operations.

Atlas must not implement:

- stolen session workflows
- fake browser fingerprinting to defeat protections
- browser automation to bypass bot challenges
- DRM circumvention
- access-control bypass

When docs mention cookies, proxies, headers, or impersonation, they must keep
that boundary clear.

## Documentation Update Matrix

When a system behavior changes, update the docs that own that contract:

| Change | Required docs |
| --- | --- |
| New command or flag | `commands.md`, `README.md` if common, `configuration.md` if config-backed. |
| New menu path | `commands.md`, `ui-ux.md`, this file, menu tests. |
| New route/session type | `architecture.md`, `download-planning.md`, `smart-sessions.md`, this file. |
| New artifact or saved-session field | `smart-sessions.md`, `commands.md`, this file. |
| Progress/UI change | `ui-ux.md`, `architecture.md` progress section, this file. |
| Backend or scheduler behavior | `download-planning.md`, `architecture.md`, `mirror-policy.md` or `media-edge-cases.md` when relevant. |
| Installer/setup/update behavior | `installation.md`, `commands.md`, `troubleshooting.md`, `development.md`, this file. |
| Supported host paths or package mappings | `configuration.md`, `installation.md`, `migration.md`, `commands.md`, this file. |
| Security/access policy | `responsible-use.md`, `media-edge-cases.md` or `mirror-policy.md`, this file. |
| Developer workflow | `development.md`. |

## Verification Contract

Before declaring architecture or system documentation current:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
sh -n install.sh
uv build
git diff --check
```

For docs-only changes, still run at least targeted tests for the touched
contracts when feasible. Examples:

```bash
uv run pytest tests/test_menu.py
uv run pytest tests/test_cli.py tests/test_optimizer.py -q
uv run pytest tests/test_progress.py tests/test_views.py -q
```

Use `rg` as the final drift check for renamed concepts, stale command names, or
old safety language.
