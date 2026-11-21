"""Antigravity live-quota inspection.

Quota is fetched through CLIProxyAPI's authenticated ``api-call`` endpoint,
which refreshes the account's access token before calling Google. The tool
never handles raw access tokens.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .cpa_api import CpaClient, auth_index

SUMMARY_URL = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
)
MODELS_URL = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
)
ANTIGRAVITY_VERSION = "2.33.0"
ANTIGRAVITY_UA = (
    f"antigravity/hub/{ANTIGRAVITY_VERSION} (aidev_client; linux/amd64)"
)

_GROUP_FAMILIES = (
    ("argon", "gemini_argon"),
    ("gemini", "gemini"),
    ("claude", "claude_gpt"),
    ("gpt", "claude_gpt"),
)


@dataclass(frozen=True)
class QuotaBucket:
    key: str
    display_name: str
    window: str  # "weekly", "session" or "model"
    remaining_fraction: float
    reset_at: datetime | None = None
    disabled: bool = False

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, self.remaining_fraction * 100.0))

    def is_exhausted(self, now: datetime | None = None) -> bool:
        if self.remaining_fraction > 0:
            return False
        if self.reset_at is None:
            return True
        current = now or datetime.now(timezone.utc)
        return self.reset_at > current

    def reset_text(self) -> str:
        if self.reset_at is None:
            return "unknown reset"
        return self.reset_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class QuotaSnapshot:
    buckets: dict[str, QuotaBucket] = field(default_factory=dict)
    models: dict[str, QuotaBucket] = field(default_factory=dict)
    error: str | None = None
    summary_status: int | None = None
    models_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.buckets or self.models)

    def all_weekly_exhausted(self, now: datetime | None = None) -> bool:
        weekly = [b for b in self.buckets.values() if b.window == "weekly"]
        return bool(weekly) and all(b.is_exhausted(now) for b in weekly)

    def all_models_exhausted(self, now: datetime | None = None) -> bool:
        models = [b for b in self.models.values() if b.window == "model"]
        return bool(models) and all(b.is_exhausted(now) for b in models)

    def worst_remaining(self) -> QuotaBucket | None:
        candidates = [b for b in self.buckets.values() if b.window in {"weekly", "session"}]
        if not candidates:
            candidates = list(self.models.values())
        if not candidates:
            return None
        return min(candidates, key=lambda bucket: bucket.remaining_fraction)


def parse_reset_at(value: Any) -> datetime | None:
    """Parse Google's resetTime field (ISO string, epoch seconds or millis)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = float(value)
        if stamp <= 0:
            return None
        if stamp > 1e12:
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.isdigit():
                return parse_reset_at(int(text))
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _fraction(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return max(0.0, min(1.0, number))


def _family_for(display_name: str) -> str | None:
    lowered = display_name.lower()
    for needle, family in _GROUP_FAMILIES:
        if needle in lowered:
            return family
    return None


def parse_summary(data: dict[str, Any] | None) -> dict[str, QuotaBucket]:
    """Parse ``retrieveUserQuotaSummary`` into stable family buckets."""
    if not isinstance(data, dict):
        return {}
    groups = data.get("groups")
    if not isinstance(groups, list):
        summary = data.get("quotaSummary")
        groups = summary.get("groups") if isinstance(summary, dict) else None
    if not isinstance(groups, list):
        return {}

    result: dict[str, QuotaBucket] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        display_name = str(group.get("displayName") or group.get("display_name") or "")
        family = _family_for(display_name)
        if not family:
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            window = str(bucket.get("window") or "").lower()
            text = " ".join(
                str(bucket.get(key) or "") for key in ("bucketId", "displayName")
            ).lower()
            if window == "weekly" or "weekly" in text:
                kind = "weekly"
            elif window in {"5h", "daily"} or "5h" in text or "five hour" in text or "daily" in text:
                kind = "session"
            else:
                continue
            disabled = bucket.get("disabled") is True
            if disabled and kind == "weekly":
                continue
            fraction = 0.0 if disabled else _fraction(bucket.get("remainingFraction"))
            if fraction is None:
                continue
            key = f"{family}_{kind}"
            if key in result:
                continue
            result[key] = QuotaBucket(
                key=key,
                display_name=str(bucket.get("displayName") or key),
                window=kind,
                remaining_fraction=fraction,
                reset_at=parse_reset_at(bucket.get("resetTime")),
                disabled=disabled,
            )
    return result


def parse_models(data: dict[str, Any] | None) -> dict[str, QuotaBucket]:
    """Parse ``fetchAvailableModels`` into per-model quota buckets."""
    if not isinstance(data, dict):
        return {}
    models = data.get("models")
    if not isinstance(models, dict):
        return {}
    result: dict[str, QuotaBucket] = {}
    for model_id, info in models.items():
        if not isinstance(info, dict) or info.get("isInternal") is True:
            continue
        quota_info = info.get("quotaInfo")
        if not isinstance(quota_info, dict):
            continue
        fraction = _fraction(quota_info.get("remainingFraction"))
        if fraction is None:
            continue
        result[str(model_id)] = QuotaBucket(
            key=str(model_id),
            display_name=str(info.get("displayName") or model_id),
            window="model",
            remaining_fraction=fraction,
            reset_at=parse_reset_at(quota_info.get("resetTime")),
        )
    return result


def _quota_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": ANTIGRAVITY_UA,
        "X-Client-Name": "antigravity",
        "X-Client-Version": ANTIGRAVITY_VERSION,
    }


def fetch_live_quota(
    client: CpaClient, credential: dict[str, Any], *, include_models: bool = True
) -> QuotaSnapshot:
    """Fetch live quota for one Antigravity credential."""
    snapshot = QuotaSnapshot()
    index = auth_index(credential)
    if not index:
        snapshot.error = "credential has no auth_index"
        return snapshot

    project_id = credential.get("project_id")
    project = str(project_id).strip() if project_id else ""
    call_body = json.dumps({"project": project} if project else {})

    try:
        raw = client.api_call(
            auth_index=index,
            method="POST",
            url=SUMMARY_URL,
            headers=_quota_headers(),
            data=call_body,
        )
        snapshot.summary_status = int(raw.get("status_code") or 0)
        if snapshot.summary_status != 200:
            snapshot.error = f"quota summary HTTP {snapshot.summary_status}"
            return snapshot
        try:
            payload = json.loads(raw.get("body") or "{}")
        except ValueError:
            snapshot.error = "quota summary returned invalid JSON"
            return snapshot
        snapshot.buckets = parse_summary(payload)
    except Exception as exc:  # noqa: BLE001 - reported to the user, never fatal
        snapshot.error = str(exc)
        return snapshot

    if include_models:
        try:
            raw_models = client.api_call(
                auth_index=index,
                method="POST",
                url=MODELS_URL,
                headers=_quota_headers(),
                data=call_body,
            )
            snapshot.models_status = int(raw_models.get("status_code") or 0)
            if snapshot.models_status == 200:
                try:
                    payload = json.loads(raw_models.get("body") or "{}")
                except ValueError:
                    payload = None
                snapshot.models = parse_models(payload)
        except Exception:  # noqa: BLE001 - per-model quota is best effort
            pass

    if not snapshot.buckets and not snapshot.models:
        snapshot.error = snapshot.error or "quota response contained no buckets"
    return snapshot


def describe_quota(snapshot: QuotaSnapshot) -> str:
    """One-line human-readable quota description."""
    if snapshot.error:
        return f"quota unavailable ({snapshot.error})"
    parts: list[str] = []
    for bucket in snapshot.buckets.values():
        label = bucket.key.replace("_", " ")
        state = "exhausted" if bucket.is_exhausted() else f"{bucket.remaining_percent:.0f}%"
        reset = f", resets {bucket.reset_text()}" if bucket.reset_at else ""
        parts.append(f"{label}: {state}{reset}")
    if not parts and snapshot.models:
        exhausted = sum(1 for bucket in snapshot.models.values() if bucket.is_exhausted())
        parts.append(f"{exhausted}/{len(snapshot.models)} models exhausted")
    return "; ".join(parts) if parts else "no quota data"


def quota_reasons(snapshot: QuotaSnapshot) -> list[str]:
    """Return deletion reasons derived from a live quota snapshot."""
    reasons: list[str] = []
    if snapshot.all_weekly_exhausted():
        reasons.append("all weekly quota exhausted")
    elif (
        not [b for b in snapshot.buckets.values() if b.window == "weekly"]
        and snapshot.all_models_exhausted()
    ):
        reasons.append("all model quota exhausted")
    return reasons
