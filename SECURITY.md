# Security Policy

## Supported versions

Atlas is pre-release software. Until the first stable release, security fixes are
made only on the latest commit of the `main` branch. After tagged releases begin,
only the latest release line and `main` will receive security fixes unless a
release announcement says otherwise.

## Reporting a vulnerability

Do not disclose suspected vulnerabilities in public issues, pull requests,
discussions, logs, or social media.

Use GitHub's private vulnerability reporting flow:

1. Open the repository's **Security** tab.
2. Select **Advisories**, then **Report a vulnerability**.
3. Include affected versions, impact, reproduction steps, and a minimal proof of
   concept. Remove real credentials, cookies, private URLs, and user data.

If private vulnerability reporting is unavailable, open a public issue that
contains no security details and asks the maintainers to establish a private
contact channel. Do not include a proof of concept in that issue.

Maintainers will make a best effort to acknowledge a complete report within
seven days and provide an initial assessment within fourteen days. Timelines may
vary for complex reports or volunteer availability. Please allow a reasonable
remediation and release window before coordinated disclosure.

## Scope

Reports are especially useful when they concern:

- command or argument injection;
- credential, cookie, or private-URL disclosure;
- unsafe redirects, TLS behavior, or unintended network access;
- arbitrary file writes, path traversal, or unsafe archive handling;
- privilege-boundary or installer integrity failures; or
- vulnerable direct dependencies that affect an Atlas execution path.

Reports about third-party websites, downloader services, or unsupported local
modifications should be sent to the responsible project or operator. General
support requests belong in the issue tracker and follow `SUPPORT.md`.

## Safe-harbor intent

Good-faith research should avoid privacy violations, service disruption, data
destruction, social engineering, and access beyond what is necessary to
demonstrate the issue. The project will not pursue action against researchers
who follow this policy and applicable law.
