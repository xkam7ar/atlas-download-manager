# Open-directory field audit — 2026-07-15

## Scope and privacy boundary

- Candidate pool: recent public community reports of open directories. The audit used a
  frozen, private candidate snapshot because automated listing access was unavailable.
- Sample: 50 distinct top-level targets. Obvious advertisements and explicit-content targets
  were excluded.
- Safety bound: one top-level fetch per check, with redirects and compression enabled. The
  audit did not recurse into child folders and did not download linked files.
- Cross-check: Atlas results were compared with an independent HTTP fetch of the rendered
  response, including status, redirect behavior, directory/file rows, visible sizes, and page
  type.
- Publication boundary: **Raw target identities are intentionally omitted**. This public
  artifact records aggregate evidence and reproducible synthetic cases without advertising
  third-party endpoints or preserving a list that will become stale.

## Aggregate outcome

| Stage | Expected behavior | Defect or edge case | Total |
| --- | ---: | ---: | ---: |
| Initial field run | 38 | 12 | 50 |
| After the fixes | 50 | 0 | 50 |

The final result means that all sampled responses behaved as expected under Atlas's
same-origin, no-parent safety policy. Unavailable targets remained explicit failures rather
than false empty successes.

The sample exercised these response families:

- Apache/nginx-style indexes, including file-only and media-only listings;
- custom directory browsers, query-driven folders, and CopyParty-style indexes;
- gzip/deflate responses and split IEC size labels;
- redirecting indexes, ordinary landing pages, and navigation-heavy catalogs;
- connection failures, HTTP failures, and TLS hostname failures.

## Defects corrected

| Area | Observed edge case | Regression contract |
| --- | --- | --- |
| Response decoding | Compressed directory bodies were not decoded consistently. | Gzip and deflate bodies are decoded within the configured byte bound. |
| Sort controls | Apache/nginx sort links appeared as content rows. | Sort controls, alternate labels, and arrow-only controls are excluded. |
| Size parsing | Labels such as `2.8 KiB`, `4.7 MiB`, and `3.4 GiB` were split. | Split IEC values are parsed as one size. |
| Query folders | Query-driven folder links lost their visible names. | Folder type and visible name are preserved. |
| File typing | Out-of-scope file links could lose their file classification. | Entries remain files even when the no-parent policy skips them. |
| Index detection | File-only and media-only indexes could look like ordinary pages. | Index signatures and standard tables identify directory-style responses. |
| Landing pages | Navigation-heavy catalogs could resemble directory indexes. | Generic navigation alone is insufficient evidence of an index. |
| Recommendations | File-only indexes could receive media-page recommendations. | Confirmed indexes enter directory exploration and recursive planning. |
| Dates | Year-first textual dates could be interpreted in the wrong order. | Year-first dates retain the source day, month, and year. |

## Safe reproduction

The public test corpus uses controlled fixtures and reserved synthetic hosts only. Representative
inputs can be modeled as:

- `https://apache.files.example.invalid/releases/` for a table-style index;
- `https://query.files.example.invalid/index.php?dir=audio` for a query-driven folder;
- `https://media.files.example.invalid/videos/` for a file-only media index;
- `https://landing.files.example.invalid/catalog/` for a navigation-heavy non-index page.

Fixtures should capture only the minimum HTML, headers, redirects, and encoding needed to
reproduce a behavior. They must not contain credentials, signed URLs, cookies, personal data,
or copied third-party content.

## Future field audits

Contributors repeating this work should use systems they control or targets they are authorized
to assess. Public reports should retain the sample size, safety bounds, aggregate outcome,
response families, and regression mapping while omitting live target identities. Any private
working list should be access-controlled, short-lived, and excluded from the repository.
