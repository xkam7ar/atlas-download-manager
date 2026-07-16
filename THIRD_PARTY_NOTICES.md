# Third-party notices

Atlas is distributed under the MIT license. It depends on separately licensed
open-source packages. Those packages remain subject to their own licenses; the
Atlas license does not replace or modify them.

The table below records the licenses declared by Atlas's direct runtime
dependencies as resolved in `uv.lock` on July 16, 2026. It is a convenience
summary, not a substitute for the dependency license texts or for reviewing the
complete transitive dependency set before redistribution.

| Dependency | Resolved version | Declared license | Upstream |
| --- | ---: | --- | --- |
| certifi | 2026.5.20 | MPL-2.0 | [python-certifi](https://github.com/certifi/python-certifi) |
| mutagen | 1.48.1 | GPL-2.0-or-later | [mutagen](https://github.com/quodlibet/mutagen) |
| platformdirs | 4.10.0 | MIT | [platformdirs](https://github.com/tox-dev/platformdirs) |
| pydantic | 2.13.4 | MIT | [pydantic](https://github.com/pydantic/pydantic) |
| pydantic-settings | 2.14.2 | MIT | [pydantic-settings](https://github.com/pydantic/pydantic-settings) |
| questionary | 2.1.1 | MIT | [questionary](https://github.com/tmbo/questionary) |
| rich | 15.0.0 | MIT | [rich](https://github.com/Textualize/rich) |
| typer | 0.26.7 | MIT | [typer](https://github.com/fastapi/typer) |
| yt-dlp | 2026.7.4 | Unlicense | [yt-dlp](https://github.com/yt-dlp/yt-dlp) |

## Dependencies that need distribution attention

- **Mutagen is declared `GPL-2.0-or-later`.** Anyone redistributing Mutagen,
  including as part of a packaged or bundled Atlas distribution, must review
  and satisfy Mutagen's license terms. Keep its license notices with the
  distribution and make the corresponding source available when the license
  requires it. A release owner should review the planned distribution model
  before publishing an installer, binary bundle, or package.
- **certifi is declared `MPL-2.0`.** A redistribution that includes certifi's
  covered files must preserve its notices and satisfy the MPL's source and
  modification requirements for those covered files.

These notes describe upstream declarations and release checks; they are not
legal advice and do not make a conclusion about a particular distribution.

## Recreate the dependency reports

The lock-derived CycloneDX SBOM and an installed-package license report can be
generated with the pinned procedure in
[`docs/dependency-compliance.md`](docs/dependency-compliance.md). Review both
artifacts whenever `pyproject.toml` or `uv.lock` changes and before every
packaged release.
