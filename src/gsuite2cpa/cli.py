"""Command-line interface for gsuite2cpa."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, ui
from .accounts import Account, email_from_auth_id, read_accounts, remove_account
from .cleanup import credential_label, run_delete
from .config import (
    CONFIG_FILE,
    DEFAULT_ACCOUNT_FILE,
    DEFAULT_DELAY,
    DEFAULT_OAUTH_TIMEOUT,
    DEFAULT_URL,
    ENV_KEY,
    ENV_URL,
    TIMING,
    first_env,
    global_config_path,
    load_settings,
    mask_secret,
    save_config,
)
from .cpa_api import CpaClient
from .errors import AuthError, GSuite2CPAError
from .google_auth import (
    LoginOptions,
    find_browser,
    kill_zombie_browsers,
    login,
)
from .quota import describe_quota, fetch_live_quota


def _common_parent() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--url",
        default=argparse.SUPPRESS,
        help=f"CLIProxyAPI base URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--key",
        default=argparse.SUPPRESS,
        help="Management key (remote-management.secret-key)",
    )
    parser.add_argument(
        "--proxy",
        default=argparse.SUPPRESS,
        help="HTTP(S)/SOCKS proxy for management API calls",
    )
    parser.add_argument(
        "--insecure",
        action=argparse.BooleanOptionalAction,
        default=argparse.SUPPRESS,
        help="Skip TLS verification (self-signed certificates only)",
    )
    parser.add_argument("--config", default=argparse.SUPPRESS, help="Path to the config file")
    parser.add_argument(
        "--no-color", action="store_true", default=argparse.SUPPRESS, help="Disable ANSI colors"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Show tracebacks for errors",
    )
    return parser


def _build_client(args: argparse.Namespace) -> tuple[CpaClient, dict[str, Any], Path]:
    settings, config_path = load_settings(
        url=getattr(args, "url", None),
        api_key=getattr(args, "key", None),
        proxy=getattr(args, "proxy", None),
        oauth_proxy=getattr(args, "oauth_proxy", None),
        insecure=getattr(args, "insecure", None),
        browser_path=getattr(args, "browser_path", None),
        browser_proxy=getattr(args, "browser_proxy", None),
        config_path=getattr(args, "config", None),
    )
    client = CpaClient(
        settings["url"],
        settings["api_key"],
        proxy=settings["proxy"],
        insecure=settings["insecure"],
    )
    return client, settings, config_path


def _print_connection(settings: dict[str, Any], config_path: Path) -> None:
    ui.kv("CLIProxyAPI", settings["url"])
    ui.kv("Management key", mask_secret(settings["api_key"]))
    ui.kv("Config", config_path)
    if settings["proxy"]:
        ui.kv("Proxy", settings["proxy"])


def _choose_account_file(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    default = Path.cwd() / DEFAULT_ACCOUNT_FILE
    legacy = Path.cwd() / "akun.txt"
    if not default.exists() and legacy.exists():
        return legacy
    return default


def cmd_init(args: argparse.Namespace) -> int:
    ui.banner()
    ui.header("Initialize gsuite2cpa")

    config_arg = getattr(args, "config", None)
    path = (
        global_config_path()
        if args.global_
        else Path(config_arg).expanduser()
        if config_arg
        else Path.cwd() / CONFIG_FILE
    )
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}

    url = getattr(args, "url", None) or first_env(ENV_URL)
    if not url:
        default_url = str(existing.get("url") or DEFAULT_URL)
        url = input(f"CLIProxyAPI URL [{default_url}]: ").strip() or default_url

    key = getattr(args, "key", None) or first_env(ENV_KEY)
    if not key:
        existing_masked = mask_secret(str(existing.get("api_key") or ""))
        prompt = f"Management key [{existing_masked}]: "
        if sys.stdin.isatty():
            import getpass

            key = getpass.getpass(prompt).strip()
        else:
            key = input(prompt).strip()
        if not key:
            key = str(existing.get("api_key") or "")
    if not key:
        ui.error("A management key is required.")
        ui.info("Set remote-management.secret-key in CLIProxyAPI's config.yaml first.")
        return 1

    proxy = getattr(args, "proxy", None)
    if proxy is None:
        proxy = existing.get("proxy")
    oauth_proxy = getattr(args, "oauth_proxy", None)
    if oauth_proxy is None:
        oauth_proxy = existing.get("oauth_proxy")
    insecure = getattr(args, "insecure", None)
    if insecure is None:
        insecure = bool(existing.get("insecure", False))
    browser_path = args.browser_path if args.browser_path is not None else existing.get("browser_path")

    data = dict(existing)
    data.update(
        {
            "url": url.rstrip("/"),
            "api_key": key,
            "proxy": proxy,
            "oauth_proxy": oauth_proxy,
            "insecure": bool(insecure),
            "browser_path": browser_path,
        }
    )
    saved = save_config(path, data)
    ui.success(f"Config saved to {saved}")

    if args.test:
        client = CpaClient(
            data["url"],
            data["api_key"],
            proxy=data.get("proxy"),
            insecure=bool(data.get("insecure")),
        )
        try:
            health = client.ping()
        except GSuite2CPAError as exc:
            ui.error(str(exc))
            return 1
        finally:
            client.close()
        ui.success(
            f"Connected — API {health['api_version']}, "
            f"{health['credentials']} credential(s)"
        )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ui.banner()
    ui.header("Doctor")
    client, settings, config_path = _build_client(args)
    _print_connection(settings, config_path)

    browser = find_browser(settings.get("browser_path"))
    if browser:
        ui.kv("Browser", browser)
    else:
        ui.kv("Browser", ui.yellow("not found — install Chrome/Chromium"))

    try:
        health = client.ping()
    except GSuite2CPAError as exc:
        ui.error(str(exc))
        client.close()
        return 1

    try:
        ui.kv("API version", health["api_version"])
        ui.kv("Credentials", health["credentials"])
        try:
            antigravity = client.antigravity_credentials()
            ui.kv("Antigravity", len(antigravity))
        except GSuite2CPAError as exc:
            ui.warn(f"Could not list credentials: {exc}")

        if health["api_version"] == "v0":
            ui.warn(
                "Legacy /v0 management API detected. Update CLIProxyAPI for v8 "
                "features such as credential verification."
            )
        ui.success("CLIProxyAPI is reachable.")
        return 0 if browser else 1
    finally:
        client.close()


def cmd_list(args: argparse.Namespace) -> int:
    client, settings, config_path = _build_client(args)
    try:
        credentials = client.antigravity_credentials()
        if args.json:
            print(json.dumps(credentials, indent=2, ensure_ascii=False, default=str))
            return 0

        ui.banner()
        ui.header("Antigravity credentials")
        _print_connection(settings, config_path)
        if not credentials:
            ui.info("No Antigravity credentials found.")
            return 0

        rows = []
        for credential in credentials:
            quota_text = ""
            if args.quota:
                snapshot = fetch_live_quota(client, credential, include_models=False)
                quota_text = describe_quota(snapshot)
            rows.append(
                (
                    credential_label(credential),
                    str(credential.get("status") or "unknown"),
                    "yes" if credential.get("disabled") else "no",
                    quota_text or (str(credential.get("project_id") or "") or "-"),
                    str(credential.get("auth_index") or "-"),
                )
            )

        headers = ("EMAIL", "STATUS", "DISABLED", "QUOTA / PROJECT", "AUTH_INDEX")
        widths = [len(value) for value in headers]
        for row in rows:
            for index, value in enumerate(row):
                widths[index] = max(widths[index], len(value))

        print()
        print("  " + "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
        print("  " + "  ".join("─" * width for width in widths))
        for row in rows:
            print("  " + "  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))
        print()
        ui.kv("Total", len(rows))
        return 0
    except GSuite2CPAError as exc:
        ui.error(str(exc))
        return 1
    finally:
        client.close()


def _login_options(args: argparse.Namespace, state: str) -> LoginOptions:
    timing_name = "fast" if args.fast else "normal"
    return LoginOptions(
        timing=TIMING[timing_name],
        timeout=args.login_timeout,
        browser_path=args.browser_path,
        proxy=args.browser_proxy,
        headless=args.headless,
        expected_state=state,
    )


def _same_email(expected: str, actual: str | None) -> bool:
    if not actual:
        return True
    return expected.strip().lower() == actual.strip().lower()


def _existing_emails(client: CpaClient) -> dict[str, str]:
    result: dict[str, str] = {}
    for credential in client.antigravity_credentials():
        email = credential.get("email")
        if not isinstance(email, str) or not email:
            email = email_from_auth_id(str(credential.get("name") or "")) or ""
        if email:
            result[email.lower()] = str(credential.get("name") or "")
    return result


def _attempt_account(
    client: CpaClient, account: Account, args: argparse.Namespace
) -> str:
    session = client.start_oauth(proxy_url=getattr(args, "oauth_proxy", None))
    ui.info(f"OAuth session started ({session['state'][:8]}…)")
    try:
        result = login(
            session["url"],
            account.email,
            account.password,
            totp_secret=account.totp_secret,
            options=_login_options(args, session["state"]),
        )
    except Exception:
        client.cancel_oauth(session["state"])
        raise

    ui.info("Authorization code captured; completing exchange…")
    try:
        client.submit_callback(session["state"], result.code)
    except Exception:
        client.cancel_oauth(session["state"])
        raise
    status = client.wait_for_oauth(session["state"], timeout=args.login_timeout)

    auth_id = str(status.get("auth_id") or "")
    actual_email = email_from_auth_id(auth_id)
    if args.strict_email and not _same_email(account.email, actual_email):
        raise AuthError(
            f"Google signed in as {actual_email}, expected {account.email}. "
            "The credential was saved by CLIProxyAPI; remove it manually or "
            "re-run with --no-strict-email."
        )

    if not auth_id:
        # Some older builds omit auth_id; verify the expected file exists.
        expected = f"antigravity-{account.email}.json"
        if client.get_credential(expected):
            auth_id = expected
    if not auth_id:
        raise AuthError(
            "CLIProxyAPI reported success but did not return a credential name. "
            "Run `gsuite2cpa list` to inspect the result."
        )

    if args.verify:
        result = client.verify_credential(auth_id, args.verify_model)
        if not result.get("verified", result.get("status") == "ok"):
            raise AuthError(
                "Credential was saved, but inference verification failed: "
                + str(result.get("error") or result.get("verification_error") or "unknown error")
            )
        ui.success("Inference verification passed.")

    return auth_id


def cmd_add(args: argparse.Namespace) -> int:
    ui.banner()
    client, settings, config_path = _build_client(args)
    account_file = _choose_account_file(args.file)

    try:
        client.detect_version()
    except GSuite2CPAError as exc:
        ui.error(str(exc))
        client.close()
        return 1

    try:
        accounts = read_accounts(account_file)
    except OSError as exc:
        ui.error(f"Cannot read {account_file}: {exc}")
        client.close()
        return 1
    if not accounts:
        ui.error(f"No valid accounts in {account_file}")
        ui.info("Format: email|password or email|password|TOTP_SECRET")
        client.close()
        return 1

    ui.header("Add accounts")
    _print_connection(settings, config_path)
    ui.kv("Account file", account_file)
    ui.kv("Accounts", len(accounts))
    ui.kv("Speed mode", "fast" if args.fast else "normal")
    ui.kv("Delay", f"{args.delay:g}s")
    ui.kv("Verify", "yes" if args.verify else "no")
    if settings.get("oauth_proxy"):
        ui.kv("OAuth proxy", settings["oauth_proxy"])

    if args.dry_run:
        print()
        for account in accounts:
            ui.info(account.email)
        ui.info("Dry run complete; nothing was added.")
        client.close()
        return 0

    existing: dict[str, str] = {}
    if args.skip_existing:
        try:
            existing = _existing_emails(client)
        except GSuite2CPAError as exc:
            ui.error(str(exc))
            client.close()
            return 1

    kill_zombie_browsers()
    started = time.time()
    success = 0
    failed = 0
    skipped = 0
    failures: list[tuple[str, str]] = []

    try:
        for index, account in enumerate(accounts, start=1):
            print()
            ui.header(f"[{index}/{len(accounts)}] {account.email}")

            if account.email.lower() in existing:
                skipped += 1
                ui.warn(
                    f"Already present as {existing[account.email.lower()]} — skipped "
                    f"(line kept in {account_file.name})"
                )
                continue

            last_error: str | None = None
            for attempt in range(1, args.retries + 2):
                if attempt > 1:
                    ui.info(f"Retry {attempt - 1}/{args.retries}")
                try:
                    auth_id = _attempt_account(client, account, args)
                    ui.success(f"Added {account.email} → {auth_id}")
                    success += 1
                    if not args.keep_succeeded:
                        try:
                            remove_account(account_file, account.raw)
                        except OSError as exc:
                            ui.warn(
                                f"Added, but could not update {account_file}: {exc}"
                            )
                    break
                except KeyboardInterrupt:
                    raise
                except GSuite2CPAError as exc:
                    last_error = str(exc)
                    ui.error(last_error)
                except Exception as exc:  # noqa: BLE001 - per-account isolation
                    last_error = str(exc)
                    ui.error(last_error)
            else:
                failed += 1
                failures.append((account.email, last_error or "unknown error"))
                ui.warn(f"Kept {account.email} in {account_file.name} for a retry")

            if index < len(accounts) and args.delay > 0:
                time.sleep(args.delay)
    except KeyboardInterrupt:
        ui.warn("Interrupted by user.")
        failed += 1

    elapsed = ui.human_duration(time.time() - started)
    ui.header("Done")
    ui.kv("Success", success)
    ui.kv("Failed", failed)
    ui.kv("Skipped", skipped)
    ui.kv("Duration", elapsed)
    if failures:
        print()
        ui.warn("Failed accounts (still in the account file):")
        for email, reason in failures:
            print(f"    {ui.red('✗')} {email}: {reason}")
    client.close()
    return 1 if failed else 0


def cmd_delete(args: argparse.Namespace) -> int:
    client, settings, config_path = _build_client(args)
    try:
        client.detect_version()
    except GSuite2CPAError as exc:
        ui.error(str(exc))
        client.close()
        return 1

    ui.banner()
    _print_connection(settings, config_path)
    try:
        return run_delete(
            client,
            dry_run=args.dry_run,
            yes=args.yes,
            use_quota=args.quota,
            include_models=not args.fast_quota,
            include_disabled=args.include_disabled,
            include_error=args.include_error,
            include_unavailable=args.include_unavailable,
            verify=args.verify,
            verify_model=args.verify_model,
            disable_instead=args.disable,
            reset_kept=args.reset,
            backup_dir=args.backup_dir,
        )
    finally:
        client.close()


def build_parser() -> argparse.ArgumentParser:
    common = _common_parent()
    parser = argparse.ArgumentParser(
        prog="gsuite2cpa",
        description=(
            "Bulk-add Google Workspace accounts to CLIProxyAPI's Antigravity "
            "provider and remove exhausted credentials."
        ),
        epilog=(
            "Examples:\n"
            "  gsuite2cpa init --url http://127.0.0.1:8317 --key KEY\n"
            "  gsuite2cpa add --file accounts.txt --fast\n"
            "  gsuite2cpa delete --dry-run\n"
            "  gsuite2cpa list --quota\n"
            "  gsuite2cpa doctor\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Save CLIProxyAPI URL and management key", parents=[common])
    init.add_argument("--browser-path", help="Chrome/Chromium executable")
    init.add_argument(
        "--oauth-proxy",
        help="Proxy CLIProxyAPI uses for the Google token/project exchange (v8+)",
    )
    init.add_argument("--global", dest="global_", action="store_true", help="Write the per-user config")
    init.add_argument("--test", action=argparse.BooleanOptionalAction, default=True)

    add = sub.add_parser("add", help="Log in accounts and add them to CLIProxyAPI", parents=[common])
    add.add_argument("--file", help=f"Account file (default: {DEFAULT_ACCOUNT_FILE})")
    add.add_argument("--fast", action="store_true", help="Shorter waits between Google pages")
    add.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=f"Delay between accounts in seconds (default: {DEFAULT_DELAY:g})",
    )
    add.add_argument("--skip-existing", action="store_true", help="Skip emails already in CLIProxyAPI")
    add.add_argument(
        "--keep-succeeded",
        action="store_true",
        help="Do not remove successful accounts from the account file",
    )
    add.add_argument("--retries", type=int, default=0, help="Retries per account (default: 0)")
    add.add_argument("--verify", action="store_true", help="Run a 1-token inference check after login")
    add.add_argument("--verify-model", help="Model used for --verify")
    add.add_argument("--headless", action="store_true", help="Run Chrome headless (not recommended)")
    add.add_argument("--browser-path", help="Chrome/Chromium executable")
    add.add_argument("--browser-proxy", help="Proxy for the browser (defaults to --proxy)")
    add.add_argument(
        "--oauth-proxy",
        help="Proxy used by CLIProxyAPI for the Google token/project exchange (v8+)",
    )
    add.add_argument(
        "--login-timeout",
        type=float,
        default=DEFAULT_OAUTH_TIMEOUT,
        help=f"OAuth timeout in seconds (default: {DEFAULT_OAUTH_TIMEOUT:g})",
    )
    add.add_argument(
        "--strict-email",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fail when Google signs in a different email (default: on)",
    )
    add.add_argument("--dry-run", action="store_true", help="List accounts without logging in")

    delete = sub.add_parser("delete", help="Remove exhausted/broken credentials", parents=[common])
    delete.add_argument("--dry-run", action="store_true", help="Scan only, do not delete")
    delete.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    delete.add_argument(
        "--quota",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fetch live quota before deciding (default: on)",
    )
    delete.add_argument("--fast-quota", action="store_true", help="Skip per-model quota calls")
    delete.add_argument("--include-disabled", action="store_true", help="Also remove disabled credentials")
    delete.add_argument("--include-error", action="store_true", help="Remove any credential in error state")
    delete.add_argument(
        "--include-unavailable",
        action="store_true",
        help="Remove credentials with an active cooldown",
    )
    delete.add_argument("--verify", action="store_true", help="Verify suspect credentials with a 1-token call")
    delete.add_argument("--verify-model", help="Model used for --verify")
    delete.add_argument(
        "--disable",
        action="store_true",
        help="Disable credentials instead of deleting them",
    )
    delete.add_argument("--reset", action="store_true", help="Reset cooldown on kept credentials")
    delete.add_argument("--backup-dir", help="Download credentials to this directory before deleting")

    listing = sub.add_parser("list", help="List Antigravity credentials", parents=[common])
    listing.add_argument("--json", action="store_true", help="Print raw JSON")
    listing.add_argument("--quota", action="store_true", help="Fetch live quota for each credential")

    sub.add_parser("doctor", help="Check connectivity and browser availability", parents=[common])

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui.configure(no_color=bool(getattr(args, "no_color", False)))
    args.command = getattr(args, "command", None)

    commands = {
        "init": cmd_init,
        "add": cmd_add,
        "delete": cmd_delete,
        "list": cmd_list,
        "doctor": cmd_doctor,
    }

    try:
        handler = commands.get(args.command or "")
        if handler is None:
            parser.print_help()
            raise SystemExit(1)
        code = handler(args)
    except KeyboardInterrupt:
        print()
        ui.warn("Interrupted.")
        raise SystemExit(130) from None
    except GSuite2CPAError as exc:
        ui.error(str(exc))
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        raise SystemExit(1) from None
    except Exception as exc:  # noqa: BLE001 - top-level guard
        ui.error(f"Unexpected error: {exc}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        raise SystemExit(1) from None
    raise SystemExit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
