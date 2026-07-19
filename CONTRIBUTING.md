# Contributing to Atlas

Thank you for helping improve Atlas. All constructive contributions are
welcome, including bug fixes, patches, performance improvements, upgrades,
new features, documentation, tests, accessibility work, design feedback, and
maintenance updates.

Participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md). For usage questions and security
reporting routes, see [SUPPORT.md](SUPPORT.md) and [SECURITY.md](SECURITY.md).

## Before you start

1. Check existing issues and pull requests to avoid duplicate work.
2. For a substantial change, open an issue first to discuss the approach.
3. Keep contributions focused, well tested, and consistent with the project's
   existing style.

## Start small

New contributors are welcome. Browse
[`good first issue`](https://github.com/xkam7ar/atlas-download-manager/labels/good%20first%20issue)
or [`help wanted`](https://github.com/xkam7ar/atlas-download-manager/labels/help%20wanted),
or improve a focused test, accessibility detail, troubleshooting path, or
backend-specific example. If no suitable issue is open, use the contribution
routes above before investing in a larger change.

## Development setup

```bash
uv sync --group dev
uv run atlas --help
```

The full local-development and quality instructions are in the
[development guide](docs/development.md).

## Submitting a pull request

1. Fork the repository and create a branch for your change.
2. Make the change and add or update tests where appropriate.
3. Run the relevant checks:

   ```bash
   uv run pytest
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src
   sh -n install.sh
   uv build
   git diff --check
   ```

4. Open a pull request that clearly explains the problem, the solution, and
   any user-visible changes.

By submitting a contribution, you agree that it may be distributed under the
repository's MIT license.
