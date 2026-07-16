# Dependency compliance audit

Run this audit whenever runtime dependencies change and before publishing a
package or installer. The SBOM is derived from the checked-in lockfile. The
license report is collected from a clean environment containing only Atlas's
locked runtime dependencies plus a pinned reporting tool.

From the repository root:

```bash
mkdir -p build/compliance

uv lock --check
uv export \
  --locked \
  --no-dev \
  --no-emit-project \
  --format cyclonedx1.5 \
  --output-file build/compliance/atlas-runtime.cdx.json

audit_env="$(mktemp -d)"
trap 'rm -rf "$audit_env"' EXIT
uv venv "$audit_env"
UV_PROJECT_ENVIRONMENT="$audit_env" uv sync --locked --no-dev
uv pip install --python "$audit_env/bin/python" 'pip-licenses==5.5.5'
"$audit_env/bin/pip-licenses" \
  --format=json \
  --with-urls \
  --with-license-file \
  --no-license-path \
  --ignore-packages pip-licenses prettytable \
  --output-file=build/compliance/atlas-runtime-licenses.json
```

The CycloneDX export requires uv 0.11.8 or newer. `uv lock --check` makes the
command fail rather than silently resolving a different dependency graph. The
temporary environment prevents globally installed or development-only packages
from contaminating the license inventory.

Review the generated files; do not assume a scanner's license string resolves
every obligation. In particular, compare the report with
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md), inspect any `UNKNOWN` or
ambiguous result, preserve upstream notices, and obtain qualified advice for a
distribution model when needed.
