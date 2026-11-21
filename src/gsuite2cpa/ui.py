"""Tiny terminal UI helpers (no third-party dependency)."""

from __future__ import annotations

import os
import sys

_COLOR = True

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"


def configure(*, no_color: bool = False) -> None:
    global _COLOR
    _COLOR = not no_color and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _paint(text: str, color: str) -> str:
    if not _COLOR:
        return text
    return f"{color}{text}{RESET}"


def bold(text: str) -> str:
    return _paint(text, BOLD)


def dim(text: str) -> str:
    return _paint(text, DIM)


def red(text: str) -> str:
    return _paint(text, RED)


def green(text: str) -> str:
    return _paint(text, GREEN)


def yellow(text: str) -> str:
    return _paint(text, YELLOW)


def blue(text: str) -> str:
    return _paint(text, BLUE)


def cyan(text: str) -> str:
    return _paint(text, CYAN)


def banner() -> None:
    print(
        cyan(
            r"""
   ___ _________  __  _____   _____
  / _ `/ __/ __ \/ / / / _ \ / ___/
 / , _/ /_/ /_/ / /_/ / // // /__
/_/|_|\__/\____/\____/____(_)___/
"""
        )
    )
    print(dim("  Google Workspace → CLIProxyAPI (Antigravity)\n"))


def rule(char: str = "─", width: int = 62) -> None:
    print(dim(char * width))


def header(text: str) -> None:
    print()
    print(bold(cyan(text)))
    rule()


def kv(key: str, value: object) -> None:
    print(f"  {dim(key.ljust(14))} {value}")


def success(text: str) -> None:
    print(f"  {green('✓')} {text}")


def warn(text: str) -> None:
    print(f"  {yellow('!')} {text}")


def error(text: str) -> None:
    print(f"  {red('✗')} {text}", file=sys.stderr)


def info(text: str) -> None:
    print(f"  {blue('·')} {text}")


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds}s"


def format_quota_short(snapshot: object | None) -> str:
    if snapshot is None:
        return "not checked"
    if getattr(snapshot, "error", None):
        return f"unavailable ({snapshot.error})"  # type: ignore[union-attr]
    bucket = snapshot.worst_remaining()  # type: ignore[attr-defined]
    if bucket is None:
        return "no data"
    return f"{bucket.remaining_percent:.0f}% ({bucket.key})"
