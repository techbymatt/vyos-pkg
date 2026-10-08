#!/usr/bin/env python3
"""Track publication inputs and the exact source inputs of every package.

Repository revisions cover workflow/configuration (including Go configuration).
Schema v2 also records patched recipe identities and resolved external Git refs;
schema v1 remains readable for legacy-cache migration. Live APT is not locked.
Canonical JSON is UTF-8, sorted object keys, compact separators, a trailing newline,
with packages sorted by (group, package, arch). Dependency text is preserved.
The signing key input must be the PUBLIC key file; its exact bytes are hashed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import NotRequired, TypedDict

try:
    from . import source_identity as package_sources
    from .workflow_utils import atomic_write, canonical_json
except ImportError:
    import source_identity as package_sources
    from workflow_utils import atomic_write, canonical_json


class Package(TypedDict):
    """One per-architecture package record within a manifest."""

    group: str
    package: str
    arch: str
    commit: str
    deps: str
    source: NotRequired[dict]
    source_digest: NotRequired[str]


class Manifest(TypedDict):
    """Record of publish inputs deployed for one manifest version."""

    schema_version: int
    repository_commit: str
    patch_commit: str
    image: str
    signing_key_sha256: str
    packages: list[Package]


REVISION = r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})"
HEX256 = r"[0-9a-fA-F]{64}"
PACKAGE_NAME = r"[a-z0-9][a-z0-9_+.-]*"


def require_pattern(value: object, pattern: str, field: str) -> str:
    """Return value if it fully matches pattern, else raise ValueError naming field."""
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid {field}")
    return value


def package_identity(package: Package) -> tuple[str, str, str]:
    """Return the (group, package, arch) tuple identifying a package."""
    return package["group"], package["package"], package["arch"]


def validate_manifest(value: object) -> Manifest:
    """Validate exact v1/v2 shapes/types and return a sorted manifest copy."""
    fields = {
        "schema_version",
        "repository_commit",
        "patch_commit",
        "image",
        "signing_key_sha256",
        "packages",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("manifest must contain exactly the publication fields")
    schema = value["schema_version"]
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError("unsupported schema_version")
    repository_commit = require_pattern(
        value["repository_commit"], REVISION, "repository_commit"
    )
    patch_commit = require_pattern(value["patch_commit"], REVISION, "patch_commit")
    image = require_pattern(value["image"], r"[^\s@]+@sha256:" + HEX256, "image")
    signing_key_sha256 = require_pattern(
        value["signing_key_sha256"], HEX256, "signing_key_sha256"
    )
    entries = value["packages"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("packages must be a non-empty array")
    packages: list[Package] = []
    seen: set[tuple[str, str, str]] = set()
    package_fields = {"group", "package", "arch", "commit", "deps"}
    if schema == 2:
        package_fields |= {"source", "source_digest"}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != package_fields:
            raise ValueError("package must contain exactly the schema's package fields")
        group = require_pattern(entry["group"], r"build(?:-extra)?", "package group")
        arch = require_pattern(entry["arch"], r"amd64|arm64", "package arch")
        name = require_pattern(entry["package"], PACKAGE_NAME, "package name")
        commit = require_pattern(entry["commit"], REVISION, "package commit")
        deps = entry["deps"]
        if not isinstance(deps, str):
            raise ValueError("package deps must be a string")
        if any(ord(char) < 32 or ord(char) == 127 for char in deps):
            raise ValueError("deps must be single-line text without control characters")
        package = Package(
            group=group, package=name, arch=arch, commit=commit, deps=deps
        )
        if schema == 2:
            try:
                source = package_sources.validate_source(entry["source"])
            except TypeError as error:
                raise ValueError("invalid package source") from error
            digest = require_pattern(entry["source_digest"], HEX256, "source digest")
            if digest != package_sources.fingerprint(source):
                raise ValueError("source digest does not match its inputs")
            if (source["recipe_tree"] is None) != (group == "build-extra"):
                raise ValueError("source recipe tree does not match the build group")
            if group == "build-extra" and (
                source["repositories"][0]["name"] != name
                or source["repositories"][0]["commit"] != commit
            ):
                raise ValueError("standalone source does not match its checkout")
            package["source"] = source
            package["source_digest"] = digest
        identity = package_identity(package)
        if identity in seen:
            raise ValueError(f"duplicate package: {identity}")
        seen.add(identity)
        packages.append(package)
    return Manifest(
        schema_version=schema,
        repository_commit=repository_commit,
        patch_commit=patch_commit,
        image=image,
        signing_key_sha256=signing_key_sha256,
        packages=sorted(packages, key=package_identity),
    )


def create_manifest(
    packages: list[Package],
    repository_commit: str,
    patch_commit: str,
    image: str,
    signing_key: Path,
) -> Manifest:
    """Create publication inputs from source records, independent of cache state."""
    return validate_manifest(
        Manifest(
            schema_version=2,
            repository_commit=repository_commit,
            patch_commit=patch_commit,
            image=image,
            signing_key_sha256=hashlib.sha256(signing_key.read_bytes()).hexdigest(),
            packages=packages,
        )
    )


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """JSON object_pairs_hook rejecting duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def load_manifest(path: Path) -> Manifest:
    """Load and strictly validate a manifest, including duplicate JSON keys."""
    return validate_manifest(
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    )


def canonical_bytes(manifest: Manifest) -> bytes:
    """Serialize a validated manifest to canonical UTF-8 JSON bytes."""
    return canonical_json(validate_manifest(manifest)) + b"\n"


def main(argv: list[str] | None = None) -> int:
    """CLI status: 0 success/equal, 1 different, 2 malformed or unreadable."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    for flag in ("packages", "signing-key", "output"):
        create.add_argument("--" + flag, type=Path, required=True)
    for flag in ("repository-commit", "patch-commit", "image"):
        create.add_argument("--" + flag, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--current", type=Path, required=True)
    compare.add_argument("--published", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            manifest = create_manifest(
                json.loads(
                    args.packages.read_text(encoding="utf-8"),
                    object_pairs_hook=unique_object,
                ),
                args.repository_commit,
                args.patch_commit,
                args.image,
                args.signing_key,
            )
            atomic_write(args.output, canonical_bytes(manifest))
        elif args.command == "compare":
            current = load_manifest(args.current)
            published = load_manifest(args.published)
            return int(canonical_bytes(current) != canonical_bytes(published))
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(f"publish_manifest: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
