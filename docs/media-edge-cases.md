# Media Edge Cases

Atlas treats every media run as a smart session: detect intent, build a typed
plan, execute with phase-aware progress, and summarize the result. This page
documents the edge cases that should stay deliberate and predictable.

## Playlist Detection

Explicit playlist URLs are deliberate playlist sessions. In the interactive
menu, pasting an explicit playlist URL into Video or Audio prompts for playlist
video vs playlist audio before planning. In non-interactive CLI commands, use
one of:

```bash
atlas playlist PLAYLIST_URL --type video
atlas playlist PLAYLIST_URL --type audio
atlas video PLAYLIST_URL --playlist
atlas audio PLAYLIST_URL --playlist
```

Normal watch URLs with `list=` or `start_radio=` remain single-item downloads,
even when `--playlist` is present. This prevents an ordinary watch URL from
turning into an accidental playlist batch.

YouTube channel and tab URLs are collection URLs. They are rejected unless both
playlist intent and a finite selection are explicit:

```bash
atlas video "https://www.youtube.com/@example/videos" --playlist --playlist-items 1
atlas audio "https://www.youtube.com/@example/videos" --playlist --playlist-end 5
```

Atlas uses the same bound for metadata probing and downloading. Human previews
show the output template for a bounded collection rather than inventing a final
filename from collection-level metadata.

Playlist sessions set yt-dlp `ignoreerrors = "only_download"` so removed,
private, or unavailable download entries can be skipped without hiding
post-processing failures.

## Authorized Content

Atlas supports user-authorized access through normal yt-dlp cookie mechanisms:

```bash
atlas video URL --cookies-from-browser safari
atlas audio URL --cookies-file cookies.txt
```

Age-restricted, login-gated, members-only, and private content either works with
authorized cookies or fails with a clean explanation. Atlas does not bypass DRM,
paywalls, membership checks, bot challenges, or access controls.

## Live And Upcoming Media

Use these policy flags to control live and scheduled items:

```bash
atlas video URL --reject-live
atlas video URL --reject-upcoming
atlas video URL --live-from-start
```

`--reject-live` and `--reject-upcoming` become yt-dlp match filters. If a live
or scheduled item still fails at extraction time, Atlas reports a specific
livestream or premiere message instead of a raw backend traceback.

## Long Media And Chapters

Extremely long videos use the same resumable yt-dlp plan as ordinary media:
partial continuation, fragment retries, file-access retries, socket timeout,
retry sleeps, throttled-rate detection, and optional HTTP chunk sizing.

Chapter workflows:

```bash
atlas video URL --chapters
atlas video URL --split-chapters
atlas audio URL --split-chapters
```

`--split-chapters` is a post-processing step, so the download is not complete
until split/finalize events finish.

## Sidecar-Only Modes

Atlas exposes sidecar-only modes without requiring raw yt-dlp flags:

```bash
atlas video URL --subtitle-only --sub-lang en
atlas video URL --thumbnail-only
atlas audio URL --info-only
atlas video URL --skip-download --info-json --thumbnail
```

`--subtitle-only`, `--thumbnail-only`, and `--info-only` imply
`--skip-download` and disable impossible embed postprocessors. `--skip-download`
is the lower-level mode for advanced sidecar combinations.

## Phase-Aware UI

Media progress is not one generic bar. Atlas separates:

- Download
- Merge
- Extract
- Embed metadata
- Thumbnail
- Finalize

An audio extraction error after a successful transfer remains an Extract-phase
error. A media run is successful only after transfer and post-processing phases
finish.

## Postprocessing Backpressure

Playlist and batch media sessions treat ffmpeg work as its own scheduler budget.
Atlas can keep media downloads active while limiting merge/extract/embed work so
CPU and disk pressure do not build without being visible. Progress and JSON
events should show the active download lane separately from postprocessing
states such as merge, extract, metadata, thumbnail, and finalize.
