# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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
