#!/usr/bin/env python3
"""Fail closed when RP-120 release identity or dependency locks diverge."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
NANOBOT_VERSION = "0.1.5.post2"
NANOBOT_BASELINE = "950dddec499fbbe0353e997158c99808f0bb41e1"


def _toml(relative: str) -> dict[str, Any]:
    return tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))


def _error(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def _locked_pins(path: Path, errors: list[str]) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    starts: list[tuple[int, str, str]] = []
    pin_re = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\;]+)")
    for index, line in enumerate(lines):
        match = pin_re.match(line)
        if match:
            starts.append((index, match.group(1).lower().replace("_", "-"), match.group(2)))
    for position, (start, name, _version) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end])
        _error(errors, "--hash=sha256:" in block, f"familia lock pin {name} has no SHA-256")
    return {name: version for _line, name, version in starts}


def _memx_requirement_pins() -> dict[str, str]:
    result: dict[str, str] = {}
    pattern = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)")
    for line in (ROOT / "memx/requirements.txt").read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            result[match.group(1).lower().replace("_", "-")] = match.group(2)
    return result


def check(backend_version: str, release_tag: str) -> list[str]:
    errors: list[str] = []
    expected_tag = f"image-v{backend_version}"
    _error(errors, backend_version == "0.4.1", "backend version must be exactly 0.4.1")
    _error(errors, release_tag == expected_tag, f"release tag must be {expected_tag}")

    familia = _toml("familia/pyproject.toml")
    nanobot = _toml("nanobot/pyproject.toml")
    memx = _toml("memx/pyproject.toml")
    memx_sdk = _toml("memx/sdk/pyproject.toml")
    identity = json.loads((ROOT / "release/release-identity.json").read_text(encoding="utf-8"))
    init_text = (ROOT / "familia/src/familia/__init__.py").read_text(encoding="utf-8")
    init_match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)

    _error(errors, familia["project"]["version"] == backend_version, "familia pyproject version diverges")
    _error(errors, init_match is not None and init_match.group(1) == backend_version, "familia runtime version diverges")
    _error(errors, identity.get("backend_version") == backend_version, "release identity backend version diverges")
    _error(errors, identity.get("release_tag") == release_tag, "release identity tag diverges")
    _error(errors, identity.get("release_scope") == "backend-only", "release scope is not backend-only")
    included = identity.get("included_components", [])
    excluded = identity.get("excluded_components", [])
    _error(errors, not any("admin" in str(item).lower() for item in included), "admin is mixed into backend components")
    _error(errors, excluded == ["ignored local admin/"], "admin exclusion is not explicit and exact")
    vendored = identity.get("vendored_nanobot", {})
    _error(errors, nanobot["project"]["version"] == NANOBOT_VERSION, "vendored nanobot metadata changed")
    _error(errors, vendored.get("version") == NANOBOT_VERSION, "vendored nanobot identity changed")
    _error(errors, vendored.get("baseline_commit") == NANOBOT_BASELINE, "vendored nanobot baseline changed")

    lock_errors: list[str] = []
    pins = _locked_pins(ROOT / "familia/requirements.lock", lock_errors)
    errors.extend(lock_errors)
    pypdf = pins.get("pypdf")
    _error(errors, pypdf is not None and 5 <= int(pypdf.split(".", 1)[0]) < 6, "pypdf lock violates >=5,<6")
    header = "\n".join((ROOT / "familia/requirements.lock").read_text(encoding="utf-8").splitlines()[:4])
    _error(errors, "nanobot/pyproject.toml familia/pyproject.toml" in header, "familia lock provenance inputs missing")

    poetry = _toml("memx/poetry.lock")
    poetry_pins = {
        item["name"].lower().replace("_", "-"): item["version"]
        for item in poetry.get("package", [])
    }
    requirement_pins = _memx_requirement_pins()
    direct_names = {
        re.match(r"^([A-Za-z0-9_.-]+)", requirement).group(1).lower().replace("_", "-")
        for requirement in memx["project"]["dependencies"]
    }
    for name in sorted(direct_names):
        _error(errors, name in requirement_pins, f"memX requirements misses direct dependency {name}")
        if name in requirement_pins:
            _error(
                errors,
                requirement_pins[name] == poetry_pins.get(name),
                f"memX requirement {name} diverges from poetry.lock",
            )
    _error(errors, (ROOT / "bin/regen-memx-lock.sh").is_file(), "memX lock generator missing")
    _error(errors, memx["project"]["version"] == "0.1.0", "memX component version changed unexpectedly")
    _error(errors, memx_sdk["project"]["version"] == "0.1.1", "memX SDK component version changed unexpectedly")

    bridge_package = json.loads((ROOT / "nanobot/bridge/package.json").read_text(encoding="utf-8"))
    bridge_lock_path = ROOT / "nanobot/bridge/package-lock.json"
    _error(errors, bridge_lock_path.is_file(), "bridge package-lock.json missing")
    if bridge_lock_path.is_file():
        bridge_lock = json.loads(bridge_lock_path.read_text(encoding="utf-8"))
        root_package = bridge_lock.get("packages", {}).get("", {})
        _error(errors, bridge_lock.get("lockfileVersion", 0) >= 3, "bridge lockfileVersion is not npm-ci compatible")
        _error(errors, bridge_lock.get("name") == bridge_package["name"], "bridge lock name diverges")
        _error(errors, bridge_lock.get("version") == bridge_package["version"], "bridge lock version diverges")
        _error(errors, root_package.get("dependencies") == bridge_package.get("dependencies"), "bridge runtime deps diverge")
        _error(errors, root_package.get("devDependencies") == bridge_package.get("devDependencies"), "bridge dev deps diverge")

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    memx_compose = (ROOT / "docker-compose.memx.yml").read_text(encoding="utf-8")
    source_pack = (ROOT / "bin/build-source-pack.sh").read_text(encoding="utf-8")
    _error(errors, "npm ci" in dockerfile and "npm install" not in dockerfile, "Docker bridge build is not npm ci locked")
    _error(errors, f"${{FAMILIA_TAG:-{backend_version}}}" in compose, "Compose Familia image tag diverges")
    _error(errors, f"${{MEMX_TAG:-{backend_version}}}" in memx_compose, "Compose memX image tag diverges")
    _error(
        errors,
        "ghcr.io/<owner>/familia-assistant" in compose,
        "Compose Familia image source is not repository-neutral",
    )
    _error(
        errors,
        "ghcr.io/<owner>/memx" in memx_compose,
        "Compose memX image source is not repository-neutral",
    )
    _error(errors, "release/release-identity.json" in source_pack, "source pack omits release identity")
    _error(errors, "SOURCE_RELEASE_TAG" in source_pack, "source pack omits release tag identity")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-version", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    errors = check(args.backend_version, args.tag)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(
        f"PASS: backend={args.backend_version} tag={args.tag} "
        f"nanobot={NANOBOT_VERSION}@{NANOBOT_BASELINE} scope=backend-only"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
