# Repository Audit — 2026-07-15

## Outcome

Atlas received a repository-wide, documentation-led audit using 30 independent
subagent passes plus a coordinating integration pass. The review covered source,
tests, CLI/menu behavior, downloader backends, network and filesystem trust
boundaries, concurrency, configuration, artifacts, packaging, CI, and release
documentation.

The implementation and local package are materially stronger and all local
quality gates pass. The project is **not public-release ready**: its documented
GitHub repository, raw installer, release tag, and Homebrew tap are unavailable,
and the PyPI/Homebrew name `atlas` is already owned by unrelated projects. Those
issues require an explicit distribution-identity decision and external release
work; they cannot be safely inferred or patched in this checkout.

The initial suite collected 813 tests. The final suite collects 876 tests and
passes with all Python warnings promoted to errors, including the two bounded
local Wget2 integration tests.

## Audit plan executed

1. Read the documentation and extract user-facing and system contracts.
2. Map architecture, entry points, data flow, engines, artifacts, and trust
   boundaries using the codebase graph before targeted source reads.
3. Establish baseline tests, lint, typing, package-build, and installer evidence.
4. Audit network, credential, redirect, proxy, cookie, path, symlink, and
   subprocess boundaries.
5. Audit direct-file, media, site, directory, aria2, Wget2, and Wget behavior.
6. Audit planner/model/configuration parity across CLI, menu, batch, and saved
   sessions.
7. Audit concurrency, adaptive scheduling, cancellation, cleanup, and terminal
   state reporting.
8. Audit UI safety, accessibility, JSON/NDJSON purity, exit codes, and automation
   behavior.
9. Audit installer, update detection, package metadata, CI, platform claims, and
   release artifacts.
10. Triage findings, patch high-confidence defects, add regression coverage, run
    adversarial/integration gates, and perform a final independent review.

## Material improvements

### Network and credential safety

- Native redirects now validate every hop, not only the final URL.
- Authorization, cookies, referrers, and secret-bearing headers are initial-hop
  only and are not forwarded across origins.
- Native Basic authentication and Netscape cookie-jar loading are implemented
  without relaxing redirect boundaries.
- Explicit proxies override ambient `NO_PROXY` behavior; aria2 RPC control
  traffic ignores ambient proxies so RPC secrets and submitted URLs stay local.
- Recursive mirror request bodies and secret-bearing custom headers/referrers
  are rejected because Wget may replay them across redirects.
- GNU Wget is rejected for site Basic authentication; Wget2 is required for the
  tested authenticated path.
- Scanner body decompression is bounded, case-insensitive response metadata is
  normalized, and redirect/scope checks reject unsafe URL forms.

### Filesystem and artifact safety

- Native downloads stage content and publish atomically only after completion
  and checksum verification; failed downloads do not expose a partial final
  file.
- Output paths reject traversal, unsafe absolute paths, symlink escapes, and
  case-folded collisions.
- Session, retry, aria2 save-session, cookie, WARC, and mirror artifacts use
  private directories/files and symlink-safe atomic publication.
- WARC files are produced in a private runtime directory and normalized before
  owner-only publication; suffix duplication and symlink overwrite are blocked.
- Human plans and saved backend commands redact recognized secrets. Recovery URL
  lists may retain an original signed URL by necessity and are now documented as
  private operational secrets rather than sanitized reports.

### Directory traversal and downloader correctness

- Signature-recognized supported directory indexes use a bounded exact-list
  executor with same-origin traversal and strict relative-path validation.
- `--max-files` is enforced only where Atlas has a complete exact list.
  Conventional Wget/Wget2 recursion now rejects the option instead of presenting
  an unenforceable guarantee; byte and runtime bounds remain available.
- Exact downloads enforce cancellation and runtime checks during discovery and
  transfer, with consistent success/failure/skipped artifacts.
- Wget2 zero-exit runs fail when real requested resources failed, while an
  expected missing `/robots.txt` policy file is not treated as a failed mirror.
- Wget2 stats, failure samples, cookie export, and WARC outputs have focused
  regression tests. The separate 50-directory field audit is recorded in
  [open-directory-audit-2026-07-15.md](open-directory-audit-2026-07-15.md).

### CLI, machine protocols, and terminal safety

- Parser-time failures honor `--json` and `--progress json`; application exit
  codes remain nonzero instead of being swallowed by the parser wrapper.
- Final JSON is one document. NDJSON dry runs emit one terminal event, and batch
  streams end with an aggregate `batch_summary` event containing counts, status,
  and exit code.
- Machine modes suppress Rich cards, progress UI, ANSI sequences, and explanatory
  prose on both success and failure paths.
- Untrusted titles, filenames, directory labels, backend messages, and media
  metadata are stripped of ANSI CSI/OSC, C0/C1, and bidirectional control
  characters before terminal rendering.
- `--no-unicode` now selects ASCII spinner and progress-bar frames. False-like
  `CI=0`, `CI=false`, and `CI=no` values no longer incorrectly disable the menu.

### Concurrency and lifecycle

- Subprocesses are placed in controllable groups, canceled/timeout processes are
  terminated and reaped, and stdout/stderr pipes are drained and closed.
- Batch cancellation and worker cleanup preserve result accounting, release
  concurrency slots, and distinguish failure from operator cancellation.
- Adaptive queue/per-host controls react to retry and pressure signals while
  remaining bounded by configured limits.
- CI now promotes resource, unraisable, and unhandled-thread warnings to errors;
  the full local suite passes the broader `-W error` gate.

### Configuration, packaging, and documentation

- TOML aliases match documented names and strict URL/template validation rejects
  malformed or unsafe input earlier.
- Tests isolate HOME/XDG/Atlas configuration from the operator's real profile.
- uv-tool install detection resolves symlink launchers; Homebrew detection no
  longer mistakes the unrelated core formula for the intended tap formula.
- `install.sh --no-install` avoids Homebrew metadata probes and remains
  non-mutating in the tested path.
- CI now checks formatting, strict lifecycle warnings, lock consistency in the
  package job, and wheel/sdist metadata with Twine.
- Homebrew style issues were reduced from ten to the three placeholder-checksum
  findings that require a real tagged release.
- Documentation no longer advertises unavailable remote installation as usable,
  removes invalid directory flags, and records the actual bounds, credential,
  WARC, machine-output, Windows-support, and release contracts.

## Verification evidence

| Gate | Result |
| --- | --- |
| `uv run pytest -q -W error` | Pass — 876 tests |
| Bounded local Wget2 integration tests | Pass |
| `uv run ruff check src tests` | Pass |
| `uv run ruff format --check src tests` | Pass |
| `uv run mypy src` | Pass — 41 source files |
| `uv lock --check` | Pass |
| `git diff --check` | Pass |
| `sh -n install.sh` | Pass |
| `./install.sh --no-install --no-menu --yes` | Pass; plan only |
| `ruby -c packaging/homebrew/atlas.rb` | Pass |
| `uv build` | Pass — wheel and sdist |
| `twine check` | Pass — wheel and sdist |
| Clean CPython 3.13 wheel install, help/version/dry-run smoke | Pass |
| `brew style packaging/homebrew/atlas.rb` | Expected release blocker — three placeholder SHA findings |

Additional specialist packaging passes verified clean-wheel CLI smoke behavior
under Python 3.12 and 3.14 and reproducible consecutive local builds.

## Remaining ranked issues

### P0 — distribution identity and public sources

- `https://github.com/xkam7ar/atlas`, its raw installer and v0.1.0 archive, and
  the documented tap are unavailable as of this audit.
- PyPI `atlas` is an unrelated AI-agent package.
- Homebrew core `atlas` is the unrelated AtlasGo database tool and installs the
  same executable name.

Required decision: select a collision-free distribution/formula identity and
confirm whether the CLI remains `atlas`. Public repository visibility is now
separate from release activation: the local installer refuses uv installation
without the verified release's full commit ID, and `atlas update`
applies the same rule. Formula URLs still require an atomic release change.

### P0 — Homebrew formula cannot be published yet

The formula still has a placeholder SHA, a nonexistent tag URL, and no generated
Python resource blocks. A real immutable release archive, SHA-256, resources,
tap repository, and conflict strategy are mandatory before publication.

### P1 — uninstall and rollback contract is incomplete

There is no Atlas uninstall command or complete failed-install rollback guide.
Current mutation is previewed and bounded, but guided installation can add
package-manager tools that are not automatically removed. Define ownership and
document safe cleanup before calling installation fully reversible.

### P1 — release/dependency policy

Git installs are now blocked unless the operator supplies the verified release's
full commit ID. A release must still publish checksums and define whether
release dependencies are locked, constrained, or tested at both minimum and
newest supported versions.

### P2 — release hygiene and platform matrix

- Build releases only from a clean checkout or add an explicit release check;
  Hatch includes otherwise-eligible untracked documentation/tests in an sdist.
- Windows is not tested and is now documented as best-effort, not supported.
- Real-wire coverage is strongest for native HTTP and Wget2. Add scheduled local
  origin/proxy integration jobs for aria2, GNU Wget, curl fallback, redirects,
  cancellation, resume, and credential stripping.

## Confidence and limits

Confidence is high for the exercised Python, CLI, native HTTP, Wget2, artifact,
and package-build paths because fixes have focused regression coverage and the
full warning-strict suite passes. Confidence is moderate for optional backend
combinations that remain primarily mock-tested. No external repository, tap,
release tag, package registration, or system package was created or modified by
this audit.
