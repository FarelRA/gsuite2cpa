# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.2] - 2026-11-21

### Fixed

- When CLIProxyAPI omits `auth_id` from the OAuth status response, look up the
  saved credential by email case-insensitively. Some builds lowercase the
  filename, which previously caused a successful login to be reported as a
  failure and retried unnecessarily.

## [1.0.1] - 2026-11-21

### Added

- `verify`, `refresh`, and `enable` subcommands with `--email` / `--name`
  filters for targeted operations.
- `CpaClient.refresh_all()` and broader v8 credential controls.

### Fixed

- DrissionPage 4.1.x compatibility: CSS attribute selectors now use an explicit
  `css:` prefix and unquoted values, so the password/TOTP fields are found.
- JavaScript helpers now pass lists as JSON strings; DrissionPage rejects raw
  Python lists as script arguments.
- Submit buttons are clicked on the actual `<button>` element instead of a
  wrapper `<div>`, with an Enter-key fallback.
- Hidden duplicate inputs (such as a stale `#identifierId`) are ignored when
  looking for a visible field.
- A Google image/audio CAPTCHA is detected immediately and reported with an
  actionable proxy hint instead of looping until the timeout.

## [1.0.0] - 2026-11-21

### Added

- Initial CLIProxyAPI-only release, rewritten from gsuite2router.
- CLIProxyAPI v8 management API support with automatic v0 fallback.
- Antigravity OAuth flow that captures the code in Chrome and submits it to
  CLIProxyAPI, so the tool works with local and remote servers.
- Bulk account import from `accounts.txt` / `akun.txt`.
- Optional `email|password|TOTP_SECRET` account format.
- Fresh temporary Chrome profile per account with zombie cleanup.
- Workspace Terms of Service, consent, account chooser, unknown-error, and
  unverified-app page handling.
- Live quota inspection through CLIProxyAPI's authenticated `api-call` endpoint.
- Safe delete policy: terminal failures and fully exhausted quotas by default.
- `delete` dry run, confirmation prompt, disable mode, cooldown reset, and
  credential backups.
- `list` (table/JSON/quota), `doctor`, and `init` commands.
- Atomic config writes with mode `0600`; TLS verification on by default.
- Unit tests with mocked HTTP and CI for Python 3.10, 3.12, and 3.14.
