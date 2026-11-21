from __future__ import annotations

import contextlib

from gsuite2cpa import __version__
from gsuite2cpa.cli import build_parser


def test_parser_builds_and_commands_parse():
    parser = build_parser()
    assert parser.parse_args(["list"]).command == "list"
    assert parser.parse_args(["delete", "--dry-run"]).dry_run is True
    assert parser.parse_args(["add", "--fast", "--delay", "1"]).fast is True
    assert parser.parse_args(["init", "--no-test"]).test is False
    assert parser.parse_args(["verify", "--email", "a@example.com"]).command == "verify"
    assert parser.parse_args(["refresh", "--all"]).all is True
    assert parser.parse_args(["enable", "--name", "antigravity-a.json"]).command == "enable"


def test_version_flag(capsys):
    parser = build_parser()
    with contextlib.suppress(SystemExit):
        parser.parse_args(["--version"])
    assert __version__ in capsys.readouterr().out
