#!/usr/bin/env python3
# dosplex-test: tier=lint needs=python-yaml
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Validate Dosplex configuration invariants.

compatibility/game-compatibility.yaml lists the settings that games rely on as
named configuration invariants. This test checks them against the repository's
own text, so that removing a required flag, environment setting or low-latency
input behavior by accident fails before deployment. It shows that a line is
still there. It does not show that any game works: scenarios, results and
acceptance are in compatibility/scenarios.yaml, which
tests/compatibility-schema-test.py validates.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - fallback path for minimal systems
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "compatibility" / "game-compatibility.yaml"


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_matrix() -> dict[str, Any]:
    if yaml is not None:
        return yaml.safe_load(MATRIX.read_text(encoding="utf-8"))

    # Minimal parser fallback for this file's subset is intentionally not
    # implemented. PyYAML is already available on the development host; failing
    # loudly is better than silently skipping regression contracts.
    fail("PyYAML is required to parse compatibility/game-compatibility.yaml")


def shell_env_pattern(name: str, value: str) -> re.Pattern[str]:
    return re.compile(
        rf"(^|\n)\s*export\s+{re.escape(name)}={re.escape(value)!s}(\s|$)|"
        rf"(^|\n)\s*export\s+{re.escape(name)}=\"{re.escape(value)}\"",
        re.MULTILINE,
    )


def shell_unset_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(^|\n)\s*unset\s+{re.escape(name)}(\s|$)", re.MULTILINE)


def assert_requirement(contract_name: str, requirement: dict[str, Any]) -> None:
    path = ROOT / requirement["_file"]
    text = path.read_text(encoding="utf-8")
    kind = requirement["kind"]
    reason = requirement.get("reason", "no reason recorded")

    if kind == "env":
        name = requirement["name"]
        value = str(requirement["value"])
        if not shell_env_pattern(name, value).search(text):
            fail(f"{contract_name}: {path}: expected export {name}={value!r} ({reason})")
        return

    if kind == "unset":
        name = requirement["name"]
        if not shell_unset_pattern(name).search(text):
            fail(f"{contract_name}: {path}: expected unset {name} ({reason})")
        return

    if kind == "text":
        needle = requirement["contains"]
        if needle not in text:
            fail(f"{contract_name}: {path}: missing text {needle!r} ({reason})")
        return

    if kind == "absent":
        needle = requirement["contains"]
        if needle in text:
            fail(f"{contract_name}: {path}: forbidden text {needle!r} ({reason})")
        return

    if kind == "command-contains":
        command = requirement["command"]
        needle = requirement["contains"]
        command_lines = [line for line in text.splitlines() if command in line]
        if not any(needle in line for line in command_lines):
            fail(f"{contract_name}: {path}: command {command!r} missing {needle!r} ({reason})")
        return

    if kind == "key-value":
        key = requirement["key"]
        value = str(requirement["value"])
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*{re.escape(value)}\s*$", re.MULTILINE)
        if not pattern.search(text):
            fail(f"{contract_name}: {path}: expected {key}={value} ({reason})")
        return

    fail(f"{contract_name}: unknown requirement kind {kind!r}")


def main() -> int:
    data = load_matrix()
    invariants = data.get("configuration_invariants", {})
    if not invariants:
        fail("matrix has no configuration_invariants")

    checked = 0
    for name, invariant in invariants.items():
        rel_file = invariant.get("file")
        if not rel_file:
            fail(f"{name}: invariant has no file")
        if not (ROOT / rel_file).exists():
            fail(f"{name}: file does not exist: {rel_file}")
        for requirement in invariant.get("required", []):
            requirement = dict(requirement)
            requirement["_file"] = rel_file
            assert_requirement(name, requirement)
            checked += 1

    print(f"PASS: configuration invariants checked ({checked} requirements in {len(invariants)} invariants)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
