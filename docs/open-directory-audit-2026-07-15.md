# Open-directory live audit — 2026-07-15

## Scope and method

- Source pool: recent posts from
  [r/opendirectories](https://www.reddit.com/r/opendirectories/). Reddit returned HTTP 403
  to automated listing requests, so the candidate-post snapshot came from the Arctic Shift
  Reddit archive API and each selected target was then checked live.
- Sample: 50 distinct top-level URLs. Obvious advertisements and explicit-content targets were
  excluded.
- Safety bound: one top-level fetch per check, with redirects and compression enabled. The audit
  did not recurse into child folders and did not download linked files.
- Cross-check: Atlas results were compared with an independent HTTP fetch of the rendered source
  response, including status, redirect target, directory/file rows, visible sizes, and page type.
- Initial result: 38 passed and 12 exposed defects or classification edge cases.
- Final result after the fixes in this change: all 50 behave as expected under Atlas's same-origin,
  no-parent safety policy. Unavailable targets remain explicit failures rather than false empty
  successes.

## Results

| # | Target | Independent top-level result | Final Atlas result |
|---:|---|---|---|
| 1 | `sound.offlinemode.org/` | Apache index, 11 folders | Pass — exact 11 folders |
| 2 | `vault.unicornriot.ninja/patriotfrontleaks/` | 7 folders, 2 files; gzip; split IEC sizes | Fixed — exact rows and sizes; sort controls excluded |
| 3 | `cdn.ay1.net/pub/` | 5 folders, 3 files, parent | Pass — real entries exact; parent skipped |
| 4 | `files.profullstack.com/.../wallpapers/` | Custom browser, 2 child folders | Pass — 2 usable folders; chrome skipped |
| 5 | `pubarc.vafe.pro/` | CopyParty index, 6 folders and README | Pass — CopyParty controls excluded |
| 6 | `dl.musicgeek.ir/` | Index, 14 folders | Pass — exact 14 folders |
| 7 | `14.177.236.243:8008/W_Shares/` | Connection timeout | Pass — explicit failed scan, not empty success |
| 8 | `195.201.59.219/public/` | TLS certificate/hostname mismatch | Pass — explicit TLS failure |
| 9 | `affiliatecp.espn.com/` | Custom browser, query-driven `audio` folder | Fixed — `audio/` detected as a folder |
| 10 | `files.mistwx.com/` | Custom index, 3 folders | Pass — exact 3 usable folders |
| 11 | `yujawang.nicerweb.com/download/` | Rich catalog with out-of-scope VTT files | Fixed — VTT rows remain files and remain safely skipped |
| 12 | `ftpmirror.your.org/.../sonniss2020/` | 1 folder, 16 files; KiB/GiB sizes | Fixed — exact rows/sizes; sort controls excluded |
| 13 | `softpro.ee/...Top 100 Masterpieces...` | Redirect to index, 10 folders and 1 file | Pass — redirect and entries exact |
| 14 | `195.178.93.92:48082/.../love-songs/` | File-only Apache index, 80 MP4 files | Fixed — detected as directory-style index |
| 15 | `84.54.191.178:5555/Music/` | Index, 4 folders | Pass — exact folders |
| 16 | `ftp.gwdg.de/` | Mirror landing/catalog page, not a raw autoindex | Fixed — classified as an HTML landing page, not a directory index |
| 17 | `ftp5.gwdg.de/pub/` | Index, 62 folders and 4 files | Pass — all usable entries found |
| 18 | `perso.eleaar.fr/serveur/` | Index, 9 folders | Pass — exact folders |
| 19 | `minerva-archive.org/browse/` | Custom listing, 21 folders plus navigation | Pass — exact 21 usable folders; navigation safely excluded |
| 20 | `nx-retrodata.ghostland.at/content/` | Redirect to ordinary shop page | Pass — not treated as an open directory |
| 21 | `treasure.fractumseraph.net/movies/` | Redirect to ordinary navigation page | Pass — not treated as an open directory |
| 22 | `zenthara.art/downloads` | Index, 1 folder and 1 file | Pass — exact entries |
| 23 | `tatu4u.net/videos/Remixes/` | File-only Apache media index, 732 files | Fixed — directory index and recursive-mirror mode detected |
| 24 | `sellingyourscreenplay.com/.../scripts/` | Apache index, 964 files | Pass — exact files |
| 25 | `madebycooper.co.uk/images/` | Index, 4 folders and `index.html` | Pass — exact entries |
| 26 | `cdu.pt/2019/i.ytimg.com/` | Index, 1 folder | Pass — exact folder |
| 27 | `inteos.com/img/` | Index, 17 folders and 10 files | Pass — exact entries |
| 28 | `ftp.labdoo.org/` | Index, 2 folders | Pass — exact folders |
| 29 | `bimsa.net/video/` | Index, 8 folders and 27 files | Pass — exact entries |
| 30 | `sdis03.com/data/` | Gzip index, 7 folders and 1 file | Pass — exact entries |
| 31 | `x.org/videos/` | HTTP 404 | Pass — explicit HTTP failure |
| 32 | `bunker-teksped.com/web/` | Index, 1 folder | Pass — exact folder |
| 33 | `scienceandfilm.org/uploads/` | Index, 7 folders | Pass — exact folders |
| 34 | `infotopia.info/videos/` | File-only index, 108 files | Fixed — directory index and recursive-mirror mode detected |
| 35 | `valleeduthouet.fr/video/` | Gzip file-only index, 5 files | Fixed — detected as directory-style index |
| 36 | `contiman.free.fr/` | Index, 21 folders and 1 file | Pass — exact entries; invalid UTF-8 safely replaced |
| 37 | `calcofi.org/downloads/` | Index, 68 folders and 28 files | Pass — exact entries |
| 38 | `mediamusic-journal.com/video/` | Gzip file-only index, 140 files | Fixed — detected as directory-style index |
| 39 | `file.wincodetek.com/` | Index, 6 folders and 7 files | Pass — exact entries |
| 40 | `avibitton.com/video/` | Gzip file-only index, 8 files | Fixed — detected as directory-style index |
| 41 | `avibitton.com/wp-content/uploads/` | Index, 14 folders | Pass — exact folders |
| 42 | `maisonpop.fr/videos/` | Index, 25 folders and 16 files | Pass — exact entries |
| 43 | `brianb.freeshell.org/` | Ordinary personal landing page | Pass — remains an HTML page |
| 44 | `imagej.net/images/` | Index, 2 folders and 180 files | Pass — exact entries |
| 45 | `greensgroomer.com/video/` | File-only index, 37 files | Pass — exact files and index type |
| 46 | `devcogneuro.com/videos/` | Index, 1 folder and 362 files | Pass — exact entries |
| 47 | `untitled-magazine.com/video/` | Index, 2 folders and 38 files | Pass — exact entries |
| 48 | `romachapter.com/video/` | Index, 1 folder and 8 files | Pass — exact entries |
| 49 | `efendilaw.com/video/` | Index, 1 folder and 51 files | Pass — exact entries |
| 50 | `robots.stanford.edu/movies/` | File-only index, 50 files | Pass — exact files and index type |

## Defects corrected

1. Bounded gzip/deflate decoding for servers that return compressed bodies to Python's HTTP
   client.
2. Apache/nginx `?C=...&O=...` sort-control filtering, including alternate column labels and
   arrow-only controls.
3. Split IEC size parsing such as `2.8 KiB`, `4.7 MiB`, and `3.4 GiB`.
4. Query-driven folders such as `index.php?dir=audio`, including preservation of the visible
   folder name.
5. Out-of-scope file links remain typed as files while the no-parent policy skips them.
6. Explicit autoindex detection from `Index of`, `Directory listing for`, and standard index-table
   markers, including file-only and media-only indexes.
7. Navigation-heavy mirror landing pages no longer become directory indexes merely because they
   contain several file or folder-like links.
8. File-only directory indexes can enter the directory explorer and receive recursive-directory
   recommendations instead of media-page recommendations.
9. Year-first textual dates such as `2022-Jan-20 08:52` no longer parse as a different
   day/month/year.
