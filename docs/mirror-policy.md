# Mirror Policy

Recursive website and directory mirrors are explicit in Atlas. They should feel
like bounded archival sessions, not open-ended crawls.

The command line and interactive menu share the same policy model. In Batch
download, `Paste URL and scan` builds a scan manifest first, recommends a
site or directory strategy, then exposes the same scope, bounds, filters, HTML,
and politeness controls before execution.

Directory-like URLs should enter the Directory Explorer first. Atlas performs a
fast visible root map, lets the user choose everything, one folder, multiple
folders, visible files, or a deep scan, then mirrors only the selected scope.
When the selected roots already resolve to exact file URLs, Atlas prefers a
generated exact-list batch so adaptive queue control, per-host caps, retry
artifacts, and duplicate-name handling stay precise.

## Scope

Atlas exposes friendly scope controls on top of recursive Wget2/Wget plans and
native exact-index plans:

```bash
atlas site URL --same-host-only
atlas site URL --same-domain-www
atlas site URL --include-subdomains
atlas dir URL --same-host-only
```

Scope presets compile to the lower-level host and domain policy:

| Preset | Effect |
| --- | --- |
| `--same-host-only` | Keep recursion on the exact seed host; disables host spanning. |
| `--same-domain-www` | Allow the seed domain and its `www` variant with domain-bounded host spanning. |
| `--include-subdomains` | Allow host spanning within the seed domain boundary. |

The presets are mutually exclusive. Advanced users can still use raw
`--span-hosts`, `--domains`, and `--exclude-domains`.

## Bounds

Atlas supports four practical mirror bounds:

```bash
atlas site URL --depth 2
atlas site URL --max-total-size 5G
atlas site URL --max-runtime 1800
atlas dir URL --max-files 500
```

`--depth` maps to Wget2/Wget recursion depth and bounds exact-index folder
walking. `--max-total-size` is a friendly alias for Wget2 quota; on an exact
index Atlas requires all selected sizes to be known and verifies the sum before
transfer. `--max-runtime` is enforced around a recursive mirror subprocess and
across exact-index discovery/transfer. Interactive operator cancellation uses
`ProcessControl`: it terminates recursive children and cooperatively stops native
exact-index work at file/progress boundaries without dumping backend shutdown
noise into the UI.

`--max-files` is exact for complete, signature-recognized CopyParty index lists.
Atlas rejects it for conventional recursive Wget/Wget2 mirrors because those
backends do not expose a reliable file-count kill switch. Use byte/runtime
limits for conventional recursion and `--adaptive --explain` only as an
informational preflight.

## Filters And HTML Policy

Core filters:

```bash
atlas dir URL --accept zip,7z,pdf,mp4
atlas dir URL --reject html,tmp
atlas site URL --include-directories /docs,/assets
atlas site URL --exclude-directories /private
atlas site URL --accept-regex '.*\.html$'
atlas site URL --reject-regex logout
```

HTML/offline-copy controls:

```bash
atlas site URL --convert-links --adjust-extension --page-requisites
atlas site URL --no-convert-links --no-assets
atlas dir URL --reject html,htm
```

Website mirrors default to keeping HTML and page requisites. Directory mirrors
default to preserving file-tree structure without page requisites or link
conversion. Wget2 directory mirrors also default to the explicit open-directory
baseline: recursive mirror mode, no-parent, resume, timestamping,
no-if-modified-since, the selected output directory as `--directory-prefix=<output>`,
and a browser-style user agent. Atlas appends the configured `--level=<depth>`
after the mirror preset so the default remains bounded.

## Politeness

Use waits and retry policy for predictable recursive behavior:

```bash
atlas site URL --wait 0.5 --random-wait --timeout 60 --tries 5
atlas dir URL --wait 0.5 --continue
```

Adaptive mirrors add scan-based safety notes and queue/per-host planning.
Wget2/Wget remain the recursive workers; complete signature-recognized
CopyParty text/HTML indexes use the bounded `native-exact-index` worker. That
exact worker currently downloads one native file at a time; adaptive queue and
per-host fields describe discovery/session planning, and recursive `--wait`
does not pace the exact native loop. `atlas dir` does not expose
`--random-wait`.
