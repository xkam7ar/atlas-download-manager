# Responsible use

[Documentation home](README.md) · [Quick start](quick-start.md) ·
[Configuration](configuration.md) · [Troubleshooting](troubleshooting.md)

`atlas` is a smart download hub around normal downloader backends. It does not
bypass DRM, paywalls, access controls, platform restrictions, or authorization
checks.

## Supported use

Use `atlas` only for media, files, and websites you are allowed to access and
download.

Examples:

- Your own uploaded media.
- Public media where downloading is permitted.
- Media you can access with your own account and where download use is allowed.
- Internal or archival workflows where you have permission.

## Unsupported use

Do not use `atlas` for:

- DRM circumvention.
- Paywall bypass.
- Credential theft.
- Access-control bypass.
- Downloading media you are not permitted to access.
- Automated abuse of platforms.
- Unbounded website crawling or mirroring without permission.

## Cookies

Cookies are supported only through normal yt-dlp mechanisms:

```text
--cookies-from-browser safari|chrome|firefox|brave|edge
--cookies-file PATH
```

Cookies should represent your own authorized browser session. `atlas` does not
steal credentials or attempt to break access restrictions.

## Privacy and local data

Treat URLs, headers, cookies, logs, session manifests, retry files, and command
history as potentially sensitive. Signed URLs often contain access tokens, and
custom headers can contain credentials.

- Prefer browser-cookie selection over copying cookie values into commands.
- Avoid putting secrets directly in URLs or shell history.
- Review session exports and logs before sharing them.
- Keep Atlas configuration, session state, and downloaded files private with
  normal filesystem permissions.
- Delete session artifacts when their recovery value no longer outweighs their
  sensitivity.
- Use `--json` output only with consumers that protect the resulting data.

Atlas cannot control copies retained by your shell, terminal recorder, CI logs,
download destination, backups, or remote servers. Redact those surfaces as part
of the workflow, not only at the final sharing step.

## Access and politeness controls

Atlas supports normal downloader controls that help with authorized sessions,
hotlink protection, and courteous transfer behavior:

| Method | Atlas surface | Intended use |
| --- | --- | --- |
| Browser cookies | `--cookies-from-browser safari|chrome|firefox|brave|edge` or `--cookies-file PATH` | Use your own logged-in browser session when the site permits downloading. |
| User agent | `--user-agent TEXT` for file/site commands; yt-dlp defaults for media | Identify as an expected client without forging authorization. |
| Referrer and headers | `--referer URL`, `--header "Name: value"` | Supply normal request context for authorized file/site downloads. |
| Delays and throttling | `--sleep`, `--rate-limit`, `--limit-rate`, `--wait`, `--random-wait` | Reduce server pressure and avoid retry storms. |
| Proxy | `--proxy URL` for media/file commands; site proxy controls for mirrors | Route traffic through an authorized network path. |
| yt-dlp impersonation | `--impersonate chrome` when `curl_cffi` is installed | Use yt-dlp-supported browser profiles for compatibility. |

These controls are not a license to evade a service's rules. Keep requests
bounded, respect robots/terms where applicable, and prefer archive/resume modes
over repeated downloads.

Atlas does not implement browser automation to defeat bot challenges, fake
browser fingerprinting from installed browsers, stolen-cookie/session workflows,
credential extraction, or DRM circumvention. If content requires those tactics,
Atlas should fail or require a normal authorized access path instead.

## Playlist safety

Playlist downloads can be large. `atlas` is deliberately conservative:

- Single-video mode is default.
- Watch URLs with playlist/radio query parameters are treated as single videos.
- `atlas playlist` accepts only explicit playlist URLs.
- Playlist ranges are available for deliberate limits.

Use:

```bash
atlas playlist PLAYLIST_URL --playlist-items 1-10
```

or:

```bash
atlas playlist PLAYLIST_URL --playlist-start 1 --playlist-end 25
```

## Network courtesy

Use retries, sleeps, and rate limits responsibly:

```bash
atlas video URL --sleep 1 --rate-limit 5M
```

For website mirrors, keep depth low, leave host spanning off unless you really
need it, and use the default wait between requests:

```bash
atlas site URL --depth 1
atlas site URL --adaptive --explain
```

For open directory mirrors, use `atlas dir` only when you intentionally want a
public file tree. Keep `--no-parent`, host spanning off, and depth bounded:

```bash
atlas dir URL --depth 2 --accept zip,pdf
atlas dir URL --adaptive --per-host-concurrency 2
```

Avoid unnecessary repeated downloads. Keep the download archive enabled unless
you have a specific reason to disable it.

For direct files, prefer checksums when the publisher provides them and avoid
raising connection counts on servers that do not invite parallel downloads.
For large batches, use `--adaptive` and a conservative
`--per-host-concurrency` so Atlas can keep same-host pressure bounded while
still using segmented downloads where byte ranges are supported. Review
`--adaptive --explain --json` first when a batch mixes tiny files, large ranged
files, media URLs, and recursive mirrors; the manifest shows each item's bucket,
priority, backend, and scheduler decision before any transfer starts.

## Related

- [Quick start](quick-start.md)
- [Configuration](configuration.md)
- [Troubleshooting](troubleshooting.md)
- [System contracts](system-contracts.md)
