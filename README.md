# gsuite2cpa

Bulk-add Google Workspace (GSuite) accounts to **[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)**'s **Antigravity** provider — and remove accounts whose quota is exhausted or whose refresh token is dead.

This is a focused rewrite of [gsuite2router](https://github.com/mhiqrambg/gsuite2router) for CLIProxyAPI. It no longer knows anything about 9Router: there is no dashboard password, no provider CRUD, and no database. It uses CLIProxyAPI's maintained **v8 management API** for everything except the one thing Google refuses to automate: the password login, which runs in a real Chrome window.

```
┌──────────────┐   management API (v8)   ┌──────────────────┐
│ gsuite2cpa   │ ──────────────────────► │ CLIProxyAPI      │
│              │  auth-url / callback    │ management API   │
│              │  credentials / verify   │                  │
└──────┬───────┘                         └────────┬─────────┘
       │ Chrome + DrissionPage                    │
       ▼                                          ▼
┌──────────────┐   Google OAuth redirect   ┌──────────────────┐
│ Google login │ ────────────────────────► │ Antigravity auth │
└──────────────┘   localhost:51121/callback └──────────────────┘
```

## Why this design

- **CLIProxyAPI owns the OAuth exchange.** The tool starts a login session, captures the authorization code from the browser, and posts it back. CLIProxyAPI exchanges the code, fetches the project ID, writes the credential file, and hot-loads it. No client secrets or token-writing logic is duplicated here.
- **Works with local and remote CLIProxyAPI.** The browser does not need to reach CLIProxyAPI's callback listener; the tool submits the code over the management API. The `http://localhost:51121/oauth-callback` page may show “connection refused” — that is expected. The code is already in the address bar.
- **Live quota through the server.** Quota checks use `/v8/management/requests/api-call` with `$TOKEN$`, so CLIProxyAPI refreshes the account token itself. The tool never reads or prints access tokens.
- **Safe deletion.** Only terminal failures and fully exhausted quotas are removed by default. You always get a dry run, a confirmation prompt, and optional credential backups.

## Requirements

- Python **3.10+**
- Google Chrome, Chromium, Brave, or Edge installed on the machine running the tool
- A running CLIProxyAPI **v8+** instance (legacy v0 is supported for add/list/delete)
- CLIProxyAPI management API enabled:

```yaml
# CLIProxyAPI config.yaml
management:
  # A key is required for ALL management requests, even on localhost.
  secret-key: "change-me-to-a-long-random-string"

  # Set true only when the tool runs on another machine.
  allow-remote: false
```

Restart CLIProxyAPI after changing the config.

## Installation

```bash
git clone https://github.com/mhiqrambg/gsuite2cpa.git
cd gsuite2cpa

python3 -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

pip install -e .
```

Or install it as an isolated CLI with pipx:

```bash
pipx install .
```

## Quick start

### 1. Configure the connection

```bash
gsuite2cpa init --url http://127.0.0.1:8317 --key 'your-management-key'
```

`init` writes `.gs2cpa.json` in the current directory (mode `0600`). Use `--global` to write `~/.config/gsuite2cpa/config.json` instead, or `--config PATH` for an explicit location.

Add `--no-test` to skip the connectivity check.

### 2. Create an account file

`accounts.txt` (or `akun.txt`, which is picked up automatically):

```text
# email|password
user1@yourdomain.com|password123
user2@yourdomain.com|password456

# Optional third field: TOTP secret for accounts with 2FA
user3@yourdomain.com|password789|JBSWY3DPEHPK3PXP
```

- One account per line.
- `#` starts a comment; blank lines are ignored.
- Passwords containing `|` are not supported (the first two `|` delimit fields).
- Duplicate emails are processed once.

### 3. Add the accounts

```bash
gsuite2cpa add
```

For each account the tool:

1. starts an Antigravity OAuth session through CLIProxyAPI;
2. opens Chrome with a **fresh temporary profile**;
3. signs in to Google, accepts Workspace TOS/consent pages if needed;
4. captures the `code` from the redirect URL;
5. submits it to CLIProxyAPI and waits for the credential to be saved;
6. checks that the signed-in email matches the account file;
7. removes the line from the account file.

Successful lines leave the file. Failed lines stay for the next run.

### 4. Remove exhausted accounts

```bash
# Scan and show the verdict without changing anything
gsuite2cpa delete --dry-run

# Remove terminal failures and accounts with all quota exhausted
gsuite2cpa delete

# Non-interactive (CI/cron) and keep a backup first
gsuite2cpa delete --yes --backup-dir backups
```

## Commands

### `gsuite2cpa init`

Save the CLIProxyAPI URL, management key, proxy, and browser path.

| Flag | Description |
| --- | --- |
| `--url URL` | CLIProxyAPI base URL (default `http://127.0.0.1:8317`) |
| `--key KEY` | Management key (`management.secret-key`) |
| `--proxy URL` | Proxy for API calls |
| `--insecure` | Skip TLS verification (self-signed only) |
| `--browser-path PATH` | Chrome/Chromium executable |
| `--oauth-proxy URL` | Proxy for CLIProxyAPI's Google token/project exchange |
| `--global` | Write to the per-user config |
| `--config PATH` | Explicit config path |
| `--no-test` | Do not test the connection |

### `gsuite2cpa add`

| Flag | Description |
| --- | --- |
| `--file PATH` | Account file (default `accounts.txt`, then `akun.txt`) |
| `--fast` | Shorter waits between Google pages |
| `--delay SECONDS` | Delay between accounts (default `3`) |
| `--skip-existing` | Do not re-add emails already in CLIProxyAPI |
| `--keep-succeeded` | Keep successful lines in the account file |
| `--retries N` | Retry each account N times (default `0`) |
| `--verify` | Run a minimal 1-token inference after login |
| `--verify-model MODEL` | Model for `--verify` |
| `--headless` | Headless Chrome (Google often blocks this) |
| `--browser-path PATH` | Chrome/Chromium executable |
| `--browser-proxy URL` | Proxy for Chrome (defaults to `--proxy`) |
| `--oauth-proxy URL` | Proxy CLIProxyAPI uses for the Google token/project exchange (v8+) |
| `--login-timeout SECONDS` | OAuth timeout (default `300`) |
| `--no-strict-email` | Do not fail when Google signs in a different email |
| `--dry-run` | List accounts without logging in |

### `gsuite2cpa delete`

Default policy removes a credential only when it is **terminal** or **globally exhausted**:

- `requires_sign_in` / recovery stage `sign_in_required`;
- OAuth error containing `invalid_grant`, revoked/refresh-token failures;
- `quota_blocked` reported by CLIProxyAPI;
- live quota shows **all weekly** buckets exhausted, or (when no weekly summary exists) **all model** buckets exhausted.

Transient 429s, a 5-hour session cooldown while the weekly quota remains, and generic error states are **kept** unless you opt in.

| Flag | Description |
| --- | --- |
| `--dry-run` | Scan only; never prompt or change anything |
| `--yes` | Skip the confirmation prompt (required in non-interactive shells) |
| `--quota / --no-quota` | Fetch live quota before deciding (default on) |
| `--fast-quota` | Skip per-model `fetchAvailableModels` calls |
| `--include-disabled` | Also remove disabled credentials |
| `--include-error` | Remove any credential in `error` state |
| `--include-unavailable` | Remove credentials with an active cooldown |
| `--verify` | Confirm suspects with a 1-token inference check |
| `--verify-model MODEL` | Model for `--verify` |
| `--disable` | Disable credentials instead of deleting them |
| `--reset` | Reset the cooldown of kept credentials |
| `--backup-dir DIR` | Download each credential to `DIR` before removing it |

`--backup-dir` writes the credential JSON files with owner-only permissions. If a backup fails, nothing is deleted.

### `gsuite2cpa list`

```bash
gsuite2cpa list            # email, status, project, auth_index
gsuite2cpa list --quota    # add live quota per credential
gsuite2cpa list --json     # machine-readable
```

### `gsuite2cpa doctor`

Checks the management key, API version, credential count, Antigravity count, and Chrome availability.

## Configuration

Values resolve in this order:

1. command-line flag
2. environment variable
3. config file
4. built-in default

| Setting | Environment variables |
| --- | --- |
| URL | `GS2CPA_URL`, `CLIPROXYAPI_URL`, `CPA_URL` |
| Key | `GS2CPA_KEY`, `CLIPROXYAPI_MANAGEMENT_KEY`, `CLIPROXYAPI_KEY`, `CPA_MANAGEMENT_KEY`, `CPA_KEY` |
| Proxy (management API) | `GS2CPA_PROXY`, `CLIPROXYAPI_PROXY`, `CPA_PROXY` |
| Proxy (OAuth exchange) | `GS2CPA_OAUTH_PROXY`, `CLIPROXYAPI_OAUTH_PROXY` |
| Insecure TLS | `GS2CPA_INSECURE`, `CLIPROXYAPI_INSECURE` |
| Browser path | `GS2CPA_BROWSER`, `CHROME_PATH` |
| Browser proxy | `GS2CPA_BROWSER_PROXY` |

Example `.gs2cpa.json`:

```json
{
  "url": "http://127.0.0.1:8317",
  "api_key": "change-me",
  "proxy": null,
  "oauth_proxy": null,
  "insecure": false,
  "browser_path": null
}
```

The file is created with mode `0600` on Unix. Do not commit it; it is in `.gitignore`.

## How the OAuth flow maps to CLIProxyAPI

| Step | Endpoint |
| --- | --- |
| Start login | `GET /v8/management/oauth/auth-url?provider=antigravity` |
| Submit captured code | `POST /v8/management/oauth/callback` |
| Poll result | `GET /v8/management/oauth/status?state=…` |
| List credentials | `GET /v8/management/credentials` |
| Verify | `POST /v8/management/credentials/verify` |
| Delete | `DELETE /v8/management/credentials?name=…` |
| Live quota | `POST /v8/management/requests/api-call` |

No `is_webui` flag is sent, so CLIProxyAPI does not start its own callback forwarder. The tool captures the code from the browser and submits it, which works whether CLIProxyAPI is local or remote.

Legacy CLIProxyAPI builds are detected automatically. Core operations fall back to `/v0/management` (`antigravity-auth-url`, `oauth-callback`, `get-auth-status`, `auth-files`, `api-call`). v8-only features such as `--verify` print a clear message.

## Security notes

- The management key is stored only in the config file (mode `0600`) or environment.
- Passwords and TOTP secrets are read from the account file and never logged.
- Each login uses a fresh temporary Chrome profile under the OS temp directory; profiles are deleted after each attempt.
- The tool does not read, print, or persist tokens. Quota calls use CLIProxyAPI's `$TOKEN$` substitution.
- TLS verification is on by default. `--insecure` is only for a self-signed management endpoint.
- Credential backups are written with mode `0600` and contain refresh tokens; treat the backup directory like a password store.

## Troubleshooting

**“Cannot connect to CLIProxyAPI”**
Check `--url`, that CLIProxyAPI is running, and that `management.secret-key` is set. Management routes return `404` when the key is empty.

**“HTTP 401/403 … check that --key matches”**
The key is wrong, or CLIProxyAPI is remote and `management.allow-remote` is `false`.

**The browser shows “This site can’t be reached” on `localhost:51121`**
Expected. The OAuth code is in the address bar; the tool reads it and submits it to CLIProxyAPI. Do not close Chrome before the tool finishes.

**“Google asked for a two-factor code”**
Add the TOTP secret as the third field: `email|password|TOTP_SECRET`. Hardware keys, push approvals, device confirmation, and reCAPTCHA cannot be automated; sign in once manually or use an account without those challenges.

**Workspace “Welcome to your new account” / Terms of Service**
Handled automatically. The tool clicks “I understand” once and waits for Google to provision the account.

**“Google signed in as X, expected Y”**
The browser session landed on the wrong Google account. The credential is already saved; remove it with `gsuite2cpa delete` or re-run with `--no-strict-email` if the alias is intentional.

**“OAuth state mismatch”**
A stale browser tab or a mixed-up run. Retry the account; each attempt starts a new session.

**`delete` says “Refusing … in a non-interactive shell”**
Pass `--yes` (after a `--dry-run` review).

**Headless Chrome fails or loops**
Google aggressively blocks headless browsers. Use the default visible window, or run the tool on a desktop/VM with a display (Xvfb works).

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

ruff check .
pytest
```

Tests run without a browser or a live CLIProxyAPI; HTTP calls are mocked.

## Project layout

```text
src/gsuite2cpa/
├── cli.py          # argparse commands
├── config.py       # env/config precedence, timing, browser constants
├── cpa_api.py      # v8 management client with v0 fallback
├── google_auth.py  # Chrome/DrissionPage OAuth automation
├── accounts.py     # account-file parsing and atomic rewrite
├── quota.py        # Antigravity live quota parsing
├── cleanup.py      # delete/disable policy
├── ui.py           # small terminal UI
└── errors.py
```

## Migrating from gsuite2router

- Replace the 9Router URL/password with the CLIProxyAPI URL/management key.
- `akun.txt` still works; `accounts.txt` is the new default.
- `--redirect-uri` is gone: CLIProxyAPI fixes the redirect to `http://localhost:51121/oauth-callback`.
- `delete` now speaks quota directly through CLIProxyAPI and has a safer default policy.
- Add `--verify` if you want a positive inference check after login.

## License

MIT
