from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gsuite2cpa.quota import (
    QuotaBucket,
    QuotaSnapshot,
    parse_models,
    parse_reset_at,
    parse_summary,
    quota_reasons,
)

FUTURE = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def test_parse_summary_maps_families_and_windows():
    data = {
        "groups": [
            {
                "displayName": "Gemini 4 Argon",
                "buckets": [
                    {"window": "weekly", "remainingFraction": 0.5, "resetTime": FUTURE},
                    {"window": "5h", "remainingFraction": 0.25, "resetTime": FUTURE},
                ],
            },
            {
                "displayName": "Claude & GPT",
                "buckets": [
                    {"window": "weekly", "remainingFraction": 0, "resetTime": FUTURE},
                ],
            },
        ]
    }
    buckets = parse_summary(data)
    assert set(buckets) == {
        "gemini_argon_weekly",
        "gemini_argon_session",
        "claude_gpt_weekly",
    }
    assert buckets["gemini_argon_weekly"].remaining_percent == 50
    assert buckets["claude_gpt_weekly"].is_exhausted() is True


def test_parse_summary_disabled_session_becomes_exhausted():
    buckets = parse_summary(
        {
            "groups": [
                {
                    "displayName": "Gemini",
                    "buckets": [
                        {"window": "weekly", "remainingFraction": 1},
                        {"window": "5h", "remainingFraction": 1, "disabled": True},
                    ],
                }
            ]
        }
    )
    assert buckets["gemini_session"].remaining_fraction == 0
    assert buckets["gemini_session"].disabled is True


def test_parse_summary_disabled_weekly_is_skipped():
    buckets = parse_summary(
        {
            "groups": [
                {
                    "displayName": "Gemini",
                    "buckets": [
                        {"window": "weekly", "remainingFraction": 0, "disabled": True},
                    ],
                }
            ]
        }
    )
    assert buckets == {}


def test_parse_models_skips_internal_and_missing_quota():
    models = parse_models(
        {
            "models": {
                "gemini-good": {"displayName": "Good", "quotaInfo": {"remainingFraction": 0.2}},
                "gemini-internal": {"isInternal": True, "quotaInfo": {"remainingFraction": 1}},
                "gemini-none": {"displayName": "No quota"},
            }
        }
    )
    assert list(models) == ["gemini-good"]


def test_exhaustion_requires_future_reset():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert QuotaBucket("x", "x", "weekly", 0, future).is_exhausted() is True
    assert QuotaBucket("x", "x", "weekly", 0, past).is_exhausted() is False
    assert QuotaBucket("x", "x", "weekly", 0, None).is_exhausted() is True


def test_all_weekly_exhausted_requires_every_family():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    snapshot = QuotaSnapshot(
        buckets={
            "gemini_weekly": QuotaBucket("gemini_weekly", "Gemini", "weekly", 0, future),
            "claude_gpt_weekly": QuotaBucket(
                "claude_gpt_weekly", "Claude", "weekly", 0.4, future
            ),
        }
    )
    assert snapshot.all_weekly_exhausted() is False
    snapshot.buckets["claude_gpt_weekly"] = QuotaBucket(
        "claude_gpt_weekly", "Claude", "weekly", 0, future
    )
    assert snapshot.all_weekly_exhausted() is True


def test_quota_reasons_falls_back_to_models_without_weekly():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    snapshot = QuotaSnapshot(
        models={
            "a": QuotaBucket("a", "A", "model", 0, future),
            "b": QuotaBucket("b", "B", "model", 0, future),
        }
    )
    assert quota_reasons(snapshot) == ["all model quota exhausted"]


def test_parse_reset_at_formats():
    assert parse_reset_at("2026-11-21T12:00:00Z") is not None
    assert parse_reset_at(1_700_000_000) is not None
    assert parse_reset_at(1_700_000_000_000) is not None
    assert parse_reset_at("") is None
    assert parse_reset_at("not-a-date") is None
