"""Classify and remove exhausted/broken Antigravity credentials."""

from __future__ import annotations

import os
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ui
from .accounts import email_from_auth_id
from .cpa_api import CpaClient, auth_index
from .errors import ApiError, GSuite2CPAError, UnsupportedFeatureError
from .quota import QuotaSnapshot, describe_quota, fetch_live_quota, quota_reasons

_TERMINAL_MARKERS = (
    "invalid_grant",
    "invalid grant",
    "invalid_refresh_token",
    "invalid refresh token",
    "token expired",
    "unauthorized",
    "revoked",
    "sign in",
    "sign-in",
    "reauth",
)

_SIGN_IN_STAGES = {"sign_in_required", "expired_sign_in_required"}


@dataclass
class CredentialReport:
    credential: dict[str, Any]
    reasons: list[str] = field(default_factory=list)
    quota: QuotaSnapshot | None = None
    verified: bool | None = None

    @property
    def name(self) -> str:
        return str(self.credential.get("name") or self.credential.get("id") or "?")

    @property
    def email(self) -> str:
        email = self.credential.get("email")
        if isinstance(email, str) and email:
            return email
        return email_from_auth_id(self.name) or self.name

    @property
    def should_remove(self) -> bool:
        return bool(self.reasons)


def credential_label(credential: dict[str, Any]) -> str:
    email = credential.get("email")
    if isinstance(email, str) and email:
        return email
    return email_from_auth_id(str(credential.get("name") or "")) or str(
        credential.get("name") or credential.get("id") or "?"
    )


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason and reason not in reasons:
        reasons.append(reason)


def classify_credential(
    credential: dict[str, Any],
    *,
    quota: QuotaSnapshot | None = None,
    include_disabled: bool = False,
    include_error: bool = False,
    include_unavailable: bool = False,
    verify_ok: bool | None = None,
) -> CredentialReport:
    """Decide whether a credential is a safe deletion candidate.

    Default policy removes only credentials that are terminal (re-auth needed)
    or whose *entire* quota is exhausted. Transient cooldowns are kept unless
    the operator explicitly opts in.
    """
    report = CredentialReport(credential=credential, quota=quota, verified=verify_ok)
    reasons = report.reasons

    status = str(credential.get("status") or "").strip().lower()
    message = str(credential.get("status_message") or "").strip()
    lowered = message.lower()
    recovery_stage = str(credential.get("recovery_stage") or "").strip().lower()

    if credential.get("disabled") is True or status == "disabled":
        if include_disabled:
            _add_reason(reasons, "disabled")
        return report

    if credential.get("requires_sign_in") is True or recovery_stage in _SIGN_IN_STAGES:
        _add_reason(reasons, "sign-in required")

    if credential.get("quota_blocked") is True:
        _add_reason(reasons, "quota blocked")

    if status == "error":
        if any(marker in lowered for marker in _TERMINAL_MARKERS):
            _add_reason(reasons, f"terminal error: {message or 'error'}")
        elif include_error:
            _add_reason(reasons, f"status error: {message or 'error'}")

    if quota is not None and verify_ok is not True:
        for reason in quota_reasons(quota):
            _add_reason(reasons, reason)

    if include_unavailable and credential.get("unavailable") is True:
        _add_reason(reasons, "unavailable/cooldown")

    if verify_ok is False:
        _add_reason(reasons, "inference verification failed")

    return report


def _confirm(count: int, action: str) -> bool:
    if not sys.stdin.isatty():
        raise GSuite2CPAError(
            f"Refusing to {action} {count} credential(s) in a non-interactive shell. "
            "Re-run with --yes to confirm."
        )
    answer = input(f"\n{action.capitalize()} {count} credential(s)? Type 'yes' to continue: ")
    return answer.strip().lower() in {"yes", "y"}


def _write_backup(client: CpaClient, reports: list[CredentialReport], root: Path) -> dict[str, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    directory = root / f"gs2cpa-{stamp}"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    saved: dict[str, Path] = {}
    for report in reports:
        try:
            payload = client.download_credential(report.name)
        except Exception as exc:  # noqa: BLE001 - backup failure aborts deletion
            raise GSuite2CPAError(
                f"Backup failed for {report.email}: {exc}. Nothing was deleted."
            ) from exc
        target = directory / Path(report.name).name
        if target.suffix.lower() != ".json":
            target = target.with_suffix(".json")
        target.write_bytes(payload)
        if os.name != "nt":
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        saved[report.name] = target
    return saved


def run_delete(
    client: CpaClient,
    *,
    dry_run: bool = False,
    yes: bool = False,
    use_quota: bool = True,
    include_models: bool = True,
    include_disabled: bool = False,
    include_error: bool = False,
    include_unavailable: bool = False,
    verify: bool = False,
    verify_model: str | None = None,
    disable_instead: bool = False,
    reset_kept: bool = False,
    backup_dir: str | None = None,
) -> int:
    """Scan and remove/disable Antigravity credentials. Returns an exit code."""
    started = time.time()
    ui.header("Scanning Antigravity credentials")

    credentials = client.antigravity_credentials()
    if not credentials:
        ui.info("No Antigravity credentials found.")
        return 0

    ui.kv("Credentials", len(credentials))
    if dry_run:
        ui.kv("Mode", ui.yellow("dry run — nothing will be changed"))
    elif disable_instead:
        ui.kv("Mode", ui.yellow("disable instead of delete"))
    else:
        ui.kv("Mode", ui.red("delete"))

    reports: list[CredentialReport] = []
    for index, credential in enumerate(credentials, start=1):
        name = str(credential.get("name") or credential.get("id") or "?")
        label = credential_label(credential)
        print()
        ui.info(f"[{index}/{len(credentials)}] {ui.bold(label)}  {ui.dim(name)}")

        quota: QuotaSnapshot | None = None
        if use_quota:
            quota = fetch_live_quota(client, credential, include_models=include_models)
            ui.kv("Quota", describe_quota(quota))

        verified: bool | None = None
        if verify:
            try:
                result = client.verify_credential(name, verify_model)
                verified = bool(result.get("verified", result.get("status") == "ok"))
                ui.kv("Verify", ui.green("ok") if verified else ui.red("failed"))
            except UnsupportedFeatureError:
                ui.warn("This CLIProxyAPI build cannot verify credentials; skipping.")
                verify = False
            except ApiError as exc:
                payload = exc.payload if isinstance(exc.payload, dict) else {}
                if exc.status == 404:
                    ui.warn("This CLIProxyAPI build cannot verify credentials; skipping.")
                    verify = False
                elif "verified" in payload or exc.status in (400, 429):
                    verified = False
                    ui.kv("Verify", ui.red("failed"))
                else:
                    ui.warn(f"Verification could not run ({exc}); not treated as a failure.")

        report = classify_credential(
            credential,
            quota=quota,
            include_disabled=include_disabled,
            include_error=include_error,
            include_unavailable=include_unavailable,
            verify_ok=verified,
        )
        reports.append(report)

        status = str(credential.get("status") or "unknown")
        ui.kv("Status", status)
        if report.should_remove:
            ui.kv("Verdict", ui.red("remove — " + ", ".join(report.reasons)))
        else:
            ui.kv("Verdict", ui.green("keep"))

    candidates = [report for report in reports if report.should_remove]
    kept = [report for report in reports if not report.should_remove]

    ui.header("Summary")
    ui.kv("Keep", len(kept))
    ui.kv("Remove", len(candidates))
    if candidates:
        for report in candidates:
            print(f"    {ui.red('✗')} {report.email}  {ui.dim(', '.join(report.reasons))}")

    if not candidates:
        ui.success("Nothing to remove.")
        return 0

    if dry_run:
        ui.info("Dry run complete.")
        return 0

    action = "disable" if disable_instead else "delete"
    if not yes and not _confirm(len(candidates), action):
        ui.info("Cancelled.")
        return 0

    backups: dict[str, Path] = {}
    if backup_dir:
        ui.info(f"Backing up credentials to {backup_dir} ...")
        backups = _write_backup(client, candidates, Path(backup_dir).expanduser())
        ui.success(f"Backed up {len(backups)} credential(s).")

    removed = 0
    failed = 0
    for report in candidates:
        try:
            if disable_instead:
                client.disable_credential(report.name, True)
            else:
                client.delete_credential(report.name)
            removed += 1
            ui.success(f"{action}d {report.email}")
        except Exception as exc:  # noqa: BLE001 - continue with the rest
            failed += 1
            ui.error(f"{action} failed for {report.email}: {exc}")

    reset = 0
    if reset_kept:
        for report in kept:
            cred = report.credential
            index = auth_index(cred)
            if not index:
                continue
            if not (cred.get("unavailable") or cred.get("next_retry_after")):
                continue
            try:
                client.reset_quota(index)
                reset += 1
                ui.success(f"Reset cooldown for {report.email}")
            except Exception as exc:  # noqa: BLE001
                ui.warn(f"Could not reset {report.email}: {exc}")

    elapsed = ui.human_duration(time.time() - started)
    ui.header("Done")
    ui.kv("Removed", removed)
    ui.kv("Failed", failed)
    ui.kv("Kept", len(kept))
    if reset:
        ui.kv("Cooldowns reset", reset)
    if backups:
        ui.kv("Backup", next(iter(backups.values())).parent)
    ui.kv("Duration", elapsed)
    return 1 if failed else 0
