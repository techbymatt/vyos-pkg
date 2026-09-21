"""Validated source catalog shared by planning and output enforcement."""

from __future__ import annotations

import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("package_catalog.json")
GROUPS = ("build", "build-extra")
SOURCE_PATTERN = r"[a-z0-9][a-z0-9_+.-]*"
DEPENDENCY_PATTERN = r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?(?:=[A-Za-z0-9.+:~_-]+)?"


def validate_name(value: object, *, dependency: bool = False) -> str:
    """Return value if it matches the source or dependency pattern; else raise."""
    pattern = DEPENDENCY_PATTERN if dependency else SOURCE_PATTERN
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(
            f"invalid {'dependency' if dependency else 'source'}: {value!r}"
        )
    return value


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    """Object pairs hook for json that rejects duplicate keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate catalog field: {key}")
        result[key] = value
    return result


def validate_catalog(value: object) -> dict[str, list[dict]]:
    """Validate parsed catalog JSON; return normalized build/build-extra entries."""
    if not isinstance(value, dict) or set(value) != set(GROUPS):
        raise ValueError("catalog must contain build and build-extra")
    result = {}
    seen = set()
    for group in GROUPS:
        if not isinstance(value[group], list):
            raise ValueError(f"{group} must be an array")
        result[group] = []
        for entry in value[group]:
            if (
                not isinstance(entry, dict)
                or "name" not in entry
                or set(entry) - {"name", "architecture", "deps"}
            ):
                raise ValueError("catalog entry requires name and only known fields")
            name = validate_name(entry["name"])
            if name in seen:
                raise ValueError(f"duplicate catalog source: {name}")
            seen.add(name)
            architecture = entry.get("architecture", "dual")
            if architecture not in ("dual", "independent_only", "amd64_only"):
                raise ValueError(f"invalid architecture policy for {name}")
            deps = entry.get("deps", [])
            if not isinstance(deps, list) or (group == "build" and deps):
                raise ValueError(f"invalid dependencies for {group}/{name}")
            deps = [validate_name(dep, dependency=True) for dep in deps]
            if len(set(deps)) != len(deps):
                raise ValueError(f"duplicate dependency for {name}")
            result[group].append(
                {"name": name, "architecture": architecture, "deps": deps}
            )
    return result


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, list[dict]]:
    """Read and validate the catalog JSON at path."""
    return validate_catalog(
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    )


def architecture_policy(group: str, package: str, catalog: dict | None = None) -> str:
    """Return the source's architecture policy, defaulting to dual if unlisted."""
    if group not in GROUPS:
        raise ValueError(f"invalid package group: {group}")
    validate_name(package)
    if catalog is None:
        catalog = load_catalog()
    return next(
        (entry["architecture"] for entry in catalog[group] if entry["name"] == package),
        "dual",
    )


def architectures(group: str, package: str, catalog: dict | None = None) -> list[str]:
    """Map policy to targets: dual yields amd64 plus arm64, otherwise amd64 only."""
    if architecture_policy(group, package, catalog) == "dual":
        return ["amd64", "arm64"]
    return ["amd64"]
