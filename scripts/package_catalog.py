"""Package definitions shared by planning, build setup and output enforcement.

The JSON catalog is the only package membership/configuration source. Local
build settings never become source-cache inputs; force a rebuild to apply them
to previously cached binaries.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

try:
    from .workflow_utils import atomic_write
except ImportError:
    from workflow_utils import atomic_write

CATALOG_PATH = Path(__file__).with_name("package_catalog.json")
GROUPS = ("build", "build-extra")
ARCHITECTURES = ("amd64", "arm64")
ARCHITECTURE_POLICIES = ("dual", "independent_only", "amd64_only")
DEFAULT_TIMEOUTS = {"build": 150, "build-extra": 30}
SOURCE_PATTERN = r"[a-z0-9][a-z0-9_+.-]*"
DEPENDENCY_PATTERN = r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?(?:=[A-Za-z0-9.+:~_-]+)?"


@dataclass(frozen=True)
class Source:
    """One immutable source definition, not a binary Debian package."""

    group: str
    name: str
    timeout_minutes: int
    architecture: str = "dual"
    deps: tuple[str, ...] = ()
    go: bool = False
    priority: int = 0

    @property
    def architectures(self) -> tuple[str, ...]:
        """amd64 owns independent outputs; only dual sources also build arm64."""
        return ARCHITECTURES if self.architecture == "dual" else ("amd64",)

    def allowed_outputs(self, arch: str) -> set[str]:
        """Allowed control architectures for a native producer."""
        if arch not in self.architectures:
            raise ValueError(f"{self.group}/{self.name}: no {arch} build is planned")
        if self.architecture == "independent_only":
            return {"all"}
        return {"amd64", "all"} if arch == "amd64" else {"arm64"}

    def build_settings(self) -> dict:
        """Execution-only matrix settings, excluded from manifests/cache keys."""
        settings = {"timeout_minutes": self.timeout_minutes}
        if self.group == "build":
            settings["go"] = self.go
        return settings


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
                or set(entry)
                - {"name", "architecture", "deps", "go", "priority", "timeout_minutes"}
            ):
                raise ValueError("catalog entry requires name and only known fields")
            name = validate_name(entry["name"])
            if name in seen:
                raise ValueError(f"duplicate catalog source: {name}")
            seen.add(name)
            architecture = entry.get("architecture", "dual")
            if architecture not in ARCHITECTURE_POLICIES:
                raise ValueError(f"invalid architecture policy for {name}")
            deps = entry.get("deps", [])
            if not isinstance(deps, list) or (group == "build" and deps):
                raise ValueError(f"invalid dependencies for {group}/{name}")
            deps = [validate_name(dep, dependency=True) for dep in deps]
            if len(set(deps)) != len(deps):
                raise ValueError(f"duplicate dependency for {name}")
            normalized = {"name": name, "architecture": architecture, "deps": deps}
            if "go" in entry:
                if group != "build" or type(entry["go"]) is not bool:
                    raise ValueError(f"go must be a boolean recipe setting for {name}")
                normalized["go"] = entry["go"]
            for field, lower, upper in (
                ("priority", 0, 100),
                ("timeout_minutes", 1, 360),
            ):
                if field in entry:
                    number = entry[field]
                    if type(number) is not int or not lower <= number <= upper:
                        raise ValueError(f"invalid {field} for {name}: {number!r}")
                    normalized[field] = number
            result[group].append(normalized)
    return result


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, list[dict]]:
    """Read and validate the catalog JSON at path."""
    return validate_catalog(
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    )


def source_definition(group: str, entry: dict) -> Source:
    """Turn a validated JSON entry into the runtime package definition."""
    return Source(
        group=group,
        name=entry["name"],
        architecture=entry.get("architecture", "dual"),
        deps=tuple(entry.get("deps", [])),
        go=entry.get("go", False),
        priority=entry.get("priority", 0),
        timeout_minutes=entry.get("timeout_minutes", DEFAULT_TIMEOUTS[group]),
    )


def iter_sources(catalog: dict) -> Iterator[Source]:
    """Expand a validated catalog once, in its declared source order."""
    for group in GROUPS:
        for entry in catalog[group]:
            yield source_definition(group, entry)


def find_source(group: str, package: str, catalog: dict | None = None) -> Source:
    """Look up a source, with backwards-compatible defaults for manual Test."""
    if group not in GROUPS:
        raise ValueError(f"invalid package group: {group}")
    validate_name(package)
    if catalog is None:
        catalog = load_catalog()
    entry = next(
        (entry for entry in catalog[group] if entry["name"] == package),
        # Unknown recipes previously received Go unconditionally. Preserve that
        # environment for Test until their requirements are audited/catalogued.
        {"name": package, "go": group == "build"},
    )
    return source_definition(group, entry)


def architecture_policy(group: str, package: str, catalog: dict | None = None) -> str:
    """Return the source's architecture policy, defaulting to dual if unlisted."""
    return find_source(group, package, catalog).architecture


def architectures(group: str, package: str, catalog: dict | None = None) -> list[str]:
    """Map policy to targets: dual yields amd64 plus arm64, otherwise amd64 only."""
    return list(find_source(group, package, catalog).architectures)


def add_source(path: Path, group: str, entry: dict) -> None:
    """Append a new source only after validating the complete resulting catalog."""
    if group not in GROUPS:
        raise ValueError(f"invalid package group: {group}")
    original = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=unique_object
    )
    validate_catalog(original)
    original[group].append(entry)
    validate_catalog(original)
    atomic_write(path, (json.dumps(original, indent=2) + "\n").encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    """Validate/list package definitions, or safely scaffold a new catalog entry."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    add = commands.add_parser("add")
    add.add_argument("--group", choices=GROUPS, required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--architecture", choices=ARCHITECTURE_POLICIES, default="dual")
    add.add_argument("--dep", action="append", default=[])
    add.add_argument("--go", action="store_true")
    add.add_argument("--priority", type=int, default=0)
    add.add_argument("--timeout-minutes", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            entry = {"name": args.name}
            for field, value, default in (
                ("architecture", args.architecture, "dual"),
                ("deps", args.dep, []),
                ("go", args.go, False),
                ("priority", args.priority, 0),
                ("timeout_minutes", args.timeout_minutes, None),
            ):
                if value != default:
                    entry[field] = value
            add_source(args.catalog, args.group, entry)
            print(
                f"Added {args.group}/{args.name}; audit its outputs and pin catalog tests."
            )
        else:
            sources = list(iter_sources(load_catalog(args.catalog)))
            for source in sources:
                print(
                    f"{source.group}/{source.name}: {','.join(source.architectures)}; "
                    f"go={str(source.go).lower()}; timeout={source.timeout_minutes}m; "
                    f"priority={source.priority}; deps={' '.join(source.deps) or '-'}"
                )
            print(
                f"{len(sources)} sources, {sum(len(s.architectures) for s in sources)} native builds"
            )
    except (OSError, ValueError) as error:
        print(f"package_catalog: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
