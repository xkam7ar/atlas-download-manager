# Atlas documentation

Use this page to choose the shortest path to an answer. User guides come first;
implementation contracts and design specifications are grouped separately.

[Project README](../README.md) · [Quick start](quick-start.md) ·
[Command reference](commands.md) · [Troubleshooting](troubleshooting.md)

## Choose your path

| If you want to… | Go here |
| --- | --- |
| Install, verify, and complete a first download | [Quick start](quick-start.md) |
| Install runtime tools or repair setup | [Installation](installation.md) |
| Find the right command or option | [Command reference](commands.md) |
| Configure paths, defaults, or display behavior | [Configuration](configuration.md) |
| Understand an end-to-end install or download lifecycle | [Architecture](architecture.md#high-level-flow) |
| Recover a failed or canceled run | [Troubleshooting](troubleshooting.md#recover-a-failed-or-canceled-session) |
| Understand how Atlas chooses a backend | [Download planning](download-planning.md) |
| Build or review Atlas itself | [Development guide](development.md) |

## Use Atlas

Start here for installation, daily workflows, automation, and support.

| Guide | What it owns |
| --- | --- |
| [Quick start](quick-start.md) | First verification, menu use, dry run, first result, and recovery. |
| [Installation](installation.md) | Installer behavior, setup modes, runtime footprints, repair, and update paths. |
| [Command reference](commands.md) | User-facing commands, examples, options, output, and exit behavior. |
| [Configuration](configuration.md) | Config paths, keys, environment variables, output organization, and archive behavior. |
| [Troubleshooting](troubleshooting.md) | Symptom-to-fix paths for setup, tools, network, media, mirrors, and sessions. |
| [Responsible use](responsible-use.md) | Access, privacy, cookies, network courtesy, and unsupported use. |
| [Upgrade and migration](migration.md) | Moving an existing checkout, configuration, or shell workflow to current Atlas names and paths. |

## Understand Atlas

These guides explain the product model without requiring source-code knowledge.

| Guide | What it explains |
| --- | --- |
| [Smart sessions](smart-sessions.md) | The shared detect, plan, customize, execute, summarize, and recover lifecycle. |
| [Download planning](download-planning.md) | Routing, media capability choices, adaptive planning, preflight, and progress events. |
| [Media edge cases](media-edge-cases.md) | Playlists, authorized media, live content, chapters, sidecars, and post-processing. |
| [Mirror policy](mirror-policy.md) | Recursive scope, bounds, filters, HTML handling, and politeness. |

```text
intent  ->  plan  ->  session  ->  engine  ->  progress  ->  result
```

Recursive mirrors are always explicit. Advanced backend controls remain
available, but the normal path starts with intent and a reviewed plan.

## Build Atlas

These documents are contributor-facing. They describe the implementation,
normative contracts, visual system, and engineering decisions.

| Document | Role |
| --- | --- |
| [Architecture](architecture.md) | Descriptive system shape, modules, boundaries, and extension points. |
| [System contracts](system-contracts.md) | Normative ownership, schemas, progress, artifacts, safety, and verification invariants. |
| [UI and UX guidelines](ui-ux.md) | Human-facing visual language, interaction patterns, accessibility, and screen states. |
| [Development guide](development.md) | Local setup, tests, change workflow, documentation ownership, and releases. |
| [Downloader research](download-research.md) | Historical backend research and the decisions derived from it. |
| [Repository audit (2026-07-15)](repository-audit-2026-07-15.md) | Thirty-pass audit scope, fixes, verification evidence, and remaining release blockers. |
| [Open-directory field audit (2026-07-15)](open-directory-audit-2026-07-15.md) | Fifty-directory detection and browser cross-check evidence. |

> [!NOTE]
> Architecture explains how the system is shaped. System contracts define what
> must remain true. UI and UX guidelines define the human experience. When they
> disagree with implementation, treat that as documentation or code drift—not
> as permission to choose whichever description is convenient.

## Documentation ownership

To prevent the same concept from acquiring several competing definitions:

| Topic | Canonical owner |
| --- | --- |
| Commands and flags | [Command reference](commands.md) |
| Config syntax and defaults | [Configuration](configuration.md) |
| User-facing session concepts | [Smart sessions](smart-sessions.md) |
| Planner decisions and algorithms | [Download planning](download-planning.md) |
| Normative schemas and invariants | [System contracts](system-contracts.md) |
| System shape and module boundaries | [Architecture](architecture.md) |
| Interaction and visual rules | [UI and UX guidelines](ui-ux.md) |
| Access and safety policy | [Responsible use](responsible-use.md) |
| Symptoms and repairs | [Troubleshooting](troubleshooting.md) |
| Install, download, cancellation, and recovery process invariants | [System contracts](system-contracts.md) |

## Need help now?

```bash
atlas --version
atlas doctor
atlas doctor --network
```

- Command missing or setup incomplete: [Installation](installation.md)
- Download or post-processing failed: [Troubleshooting](troubleshooting.md)
- Previous batch interrupted: [Saved-session recovery](quick-start.md#recover-an-interrupted-session)
- Unsure whether a workflow is appropriate: [Responsible use](responsible-use.md)
