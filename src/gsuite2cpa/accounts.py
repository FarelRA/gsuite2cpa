"""Account-file parsing and atomic line removal.

Format (one account per line)::

    email|password
    email|password|TOTP_SECRET

Blank lines and lines starting with ``#`` are ignored. TOTP is optional; it is
only used when Google asks for a one-time code.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Account:
    email: str
    password: str
    totp_secret: str | None
    raw: str
    line_number: int


def read_accounts(path: str | Path, *, dedupe: bool = True) -> list[Account]:
    """Read accounts from ``path``.

    Invalid lines are skipped; the caller can report the resulting count.
    """
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return []

    accounts: list[Account] = []
    seen: set[str] = set()
    with file_path.open("r", encoding="utf-8") as handle:
        for number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 2:
                continue
            email, password = parts[0], parts[1]
            if not email or not password or not _EMAIL_RE.match(email):
                continue
            totp = parts[2] if len(parts) >= 3 and parts[2] else None
            key = email.lower()
            if dedupe and key in seen:
                continue
            seen.add(key)
            accounts.append(
                Account(
                    email=email,
                    password=password,
                    totp_secret=totp,
                    raw=line,
                    line_number=number,
                )
            )
    return accounts


def remove_account(path: str | Path, raw_line: str) -> bool:
    """Remove the first line matching ``raw_line``.

    The rewrite is atomic and preserves the file mode. Returns ``True`` when a
    line was removed.
    """
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return False

    original = file_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    removed = False
    kept: list[str] = []
    for line in lines:
        if not removed and line.strip() == raw_line:
            removed = True
            continue
        kept.append(line)
    if not removed:
        return False

    mode = stat.S_IMODE(file_path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{file_path.name}.", dir=str(file_path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(kept)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, file_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return True


def email_from_auth_id(auth_id: str | None) -> str | None:
    """Extract the account email from a CLIProxyAPI Antigravity credential ID.

    CLIProxyAPI stores Antigravity credentials as
    ``antigravity-<email>.json``. Unknown shapes return ``None``.
    """
    if not auth_id:
        return None
    name = auth_id.strip()
    if name.startswith("antigravity-"):
        name = name[len("antigravity-") :]
    if name.lower().endswith(".json"):
        name = name[:-5]
    if not name or "@" not in name:
        return None
    return name
