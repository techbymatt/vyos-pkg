#!/usr/bin/env python3
"""Plan Test and Publish jobs; stdout contains only GITHUB_OUTPUT lines.

Pure matrix functions are separate from revision, cache and deployed-manifest
lookups. The local input manifest is a candidate for deployment, never a marker
of successful publication. Only the deployed manifest can suppress publication.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

try:
    from . import (
        legacy_package_cache,
        package_sources,
    )
    from . import (
        package_catalog as catalog,
    )
    from . import (
        publish_manifest as manifest,
    )
except ImportError:
    import legacy_package_cache
    import package_catalog as catalog
    import package_sources
    import publish_manifest as manifest

RESTORE_BATCH_SIZE = 8


def runner_label(arch: str) -> str:
    """GitHub Actions runner label for a package architecture."""
    return "ubuntu-26.04" if arch == "amd64" else "ubuntu-26.04-arm"


def parse_input(raw: str, *, dependency: bool = False) -> list[str]:
    """Normalize raw comma/whitespace inputs and preserve first occurrence order."""
    return list(
        dict.fromkeys(
            catalog.validate_name(token, dependency=dependency)
            for token in re.split(r"[,\s]+", raw.strip())
            if token
        )
    )


def plan_test(
    packages: str, extra_packages: str, deps: str, sources: dict | None = None
) -> dict:
    """Build the Test job matrices and verification architectures from inputs."""
    sources = catalog.load_catalog() if sources is None else sources
    dependencies = " ".join(parse_input(deps, dependency=True))
    selected = {
        "build": parse_input(packages),
        "build-extra": parse_input(extra_packages),
    }
    if set(selected["build"]) & set(selected["build-extra"]):
        raise ValueError("a Test source cannot appear in both build groups")
    result = {}
    arches = set()
    for group, names in selected.items():
        entries = []
        for name in names:
            for arch in catalog.architectures(group, name, sources):
                entry = {
                    "package": name,
                    "arch": arch,
                    "runner_label": runner_label(arch),
                }
                if group == "build-extra":
                    entry["deps"] = dependencies
                entries.append(entry)
                arches.add(arch)
        result[group + "-matrix"] = {"include": entries}
    result["verify-arches"] = sorted(arches)
    result["verify-plan"] = sorted(
        f"deb-{entry['package']}-{entry['arch']}"
        for group in ("build", "build-extra")
        for entry in result[group + "-matrix"]["include"]
    )
    return result


def source_records(sources: dict, revisions: dict[tuple[str, str], dict]) -> list[dict]:
    """Expand resolved source fingerprints into per-architecture records."""
    records = []
    for group in catalog.GROUPS:
        for source in sources[group]:
            name = source["name"]
            resolved = revisions[group, name]
            commit = manifest.require_pattern(
                resolved["commit"], manifest.REVISION, f"{group}/{name} revision"
            )
            inputs = package_sources.validate_source(resolved["source"])
            digest = package_sources.fingerprint(inputs)
            if resolved["source_digest"] != digest:
                raise ValueError(
                    f"{group}/{name} source fingerprint does not match inputs"
                )
            for arch in catalog.architectures(group, name, sources):
                records.append(
                    {
                        "group": group,
                        "package": name,
                        "arch": arch,
                        "commit": commit,
                        "deps": " ".join(source["deps"]),
                        "source": inputs,
                        "source_digest": digest,
                    }
                )
    return records


def visible_cache_keys(caches: list[dict], ref: str, default_branch: str) -> list[str]:
    """Newest first across both refs visible to the workflow cache restore."""
    visible = [c for c in caches if c["ref"] in (ref, f"refs/heads/{default_branch}")]
    return [
        c["key"] for c in sorted(visible, key=lambda c: c["created_at"], reverse=True)
    ]


def cache_prefix(record: dict) -> str:
    """Identify one source/architecture's outputs independently of build tooling."""
    return package_sources.cache_prefix(record)


def plan_publish(
    records: list[dict],
    cached_keys: list[str],
    run: str,
    *,
    changed: bool = True,
    force_rebuild: bool = False,
    legacy_matches: dict[tuple[str, str, str], str] | None = None,
) -> dict:
    """Route records into build and restore matrices, batching cache hits per runner."""
    result = {
        name: {"include": []}
        for name in ("build-matrix", "build-extra-matrix", "restore-matrix")
    }
    if not changed and not force_rebuild:
        result["verify-plan"] = []
        return result
    hits: dict[str, list[dict]] = {}
    for record in records:
        prefix = cache_prefix(record)
        key = (
            None
            if force_rebuild
            else next((key for key in cached_keys if key.startswith(prefix)), None)
        )
        if key is None and not force_rebuild:
            key = (legacy_matches or {}).get(manifest.package_identity(record))
        entry = dict(
            record,
            runner_label=runner_label(record["arch"]),
            cache_key=key or prefix + run,
        )
        if key and key.startswith("cache-v2-"):
            entry["save_cache_key"] = prefix + run
        if not entry["deps"]:
            del entry["deps"]
        if key:
            hits.setdefault(entry["runner_label"], []).append(entry)
        else:
            result[record["group"] + "-matrix"]["include"].append(entry)
    for runner, entries in sorted(hits.items()):
        for start in range(0, len(entries), RESTORE_BATCH_SIZE):
            result["restore-matrix"]["include"].append(
                {
                    "runner_label": runner,
                    "entries": entries[start : start + RESTORE_BATCH_SIZE],
                }
            )
    result["verify-plan"] = sorted(
        [
            f"deb-{entry['package']}-{entry['arch']}"
            for matrix in ("build-matrix", "build-extra-matrix")
            for entry in result[matrix]["include"]
        ]
        + [
            f"deb-{entry['package']}-{entry['arch']}"
            for batch in result["restore-matrix"]["include"]
            for entry in batch["entries"]
        ]
    )
    return result


def publication_changed(
    current: dict, published: object, force_rebuild: bool = False
) -> bool:
    """Whether the current manifest differs from the deployed one, or refresh forced."""
    if force_rebuild or published is None:
        return True
    try:
        return manifest.canonical_bytes(current) != manifest.canonical_bytes(published)
    except (ValueError, RecursionError):
        return True


def command_output(arguments: list[str], cwd: Path | None = None) -> str:
    """Run a command and return its stripped stdout."""
    return subprocess.check_output(arguments, cwd=cwd, text=True).strip()


def git(root: Path, *arguments: str) -> str:
    """Run a git command in a repository and return its stripped stdout."""
    return command_output(["git", *arguments], cwd=root)


def fetch_caches(repository: str, ref: str) -> list[str]:
    """List cache keys visible to the workflow restore, newest first, via gh api."""
    default_branch = command_output(
        ["gh", "api", f"repos/{repository}", "--jq", ".default_branch"]
    )
    pages = json.loads(
        command_output(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repository}/actions/caches?per_page=100",
            ]
        )
    )
    return visible_cache_keys(
        [cache for page in pages for cache in page["actions_caches"]],
        ref,
        default_branch,
    )


def fetch_published(repository: str, run: str) -> object:
    """Load the deployed manifest from Pages, or None when unavailable or invalid."""
    try:
        url = command_output(
            ["gh", "api", f"repos/{repository}/pages", "--jq", ".html_url"]
        )
        if not url.startswith("https://"):
            raise ValueError("Pages URL is not HTTPS")
        text = command_output(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--retry",
                "2",
                "--connect-timeout",
                "10",
                "--max-time",
                "60",
                "-H",
                "Cache-Control: no-cache",
                f"{url.rstrip('/')}/input-manifest.json?run={quote(run, safe='')}",
            ]
        )
        return manifest.validate_manifest(
            json.loads(text, object_pairs_hook=manifest.unique_object)
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        ValueError,
        RecursionError,
    ) as error:
        print(
            f"Published manifest unavailable or invalid; publishing: {error}",
            file=sys.stderr,
        )
        return None


def run_publish(args: argparse.Namespace) -> dict:
    """Plan publish inputs and matrices, writing input-manifest.json."""
    sources = catalog.load_catalog(args.workflow_root / "scripts/package_catalog.json")
    patch_commit = git(args.patch_root, "rev-parse", "HEAD")
    reader = package_sources.RecipeReader(args.patch_root)
    resolver = package_sources.Resolver()
    legacy_matches = {}
    legacy_notes = {}
    try:
        records = source_records(
            sources,
            package_sources.resolve_sources(reader, resolver, sources, patch_commit),
        )
        current = manifest.create_manifest(
            records,
            args.repository_commit,
            patch_commit,
            args.image,
            args.workflow_root / "vyos-pkg.asc",
        )
        (args.workflow_root / "input-manifest.json").write_bytes(
            manifest.canonical_bytes(current)
        )
        keys = fetch_caches(args.repository, args.ref)
        published = (
            None if args.force_rebuild else fetch_published(args.repository, args.run)
        )
        changed = publication_changed(current, published, args.force_rebuild)
        if changed and not args.force_rebuild:
            verifier = legacy_package_cache.LegacyVerifier(
                args.repository, reader, resolver
            )
            legacy_matches = verifier.matches(records, keys)
            legacy_notes = verifier.reasons
    finally:
        resolver.close()
    result = {"patch-commit": patch_commit, "changed": changed}
    result.update(
        plan_publish(
            records,
            keys,
            args.run,
            changed=changed,
            force_rebuild=args.force_rebuild,
            legacy_matches=legacy_matches,
        )
    )
    print(
        f"Publication inputs changed (or refresh forced): {str(changed).lower()}",
        file=sys.stderr,
    )
    for name in ("build-matrix", "build-extra-matrix", "restore-matrix"):
        print(f"{name}: {len(result[name]['include'])} jobs", file=sys.stderr)
    previous = {
        manifest.package_identity(record): record
        for record in (published or {}).get("packages", [])
    }
    for name in ("build-matrix", "build-extra-matrix"):
        for record in result[name]["include"]:
            old = previous.get(manifest.package_identity(record), {})
            note = legacy_notes.get(manifest.package_identity(record))
            reason = (
                "forced rebuild"
                if args.force_rebuild
                else "source inputs changed"
                if old.get("source_digest") not in (None, record["source_digest"])
                else f"legacy inputs unverified: {note}"
                if note
                else "matching source cache unavailable"
            )
            print(
                f"{record['package']}/{record['arch']}: build ({reason})",
                file=sys.stderr,
            )
    for batch in result["restore-matrix"]["include"]:
        for record in batch["entries"]:
            reason = (
                "verified legacy inputs; migrating cache"
                if "save_cache_key" in record
                else "matching source inputs"
            )
            print(
                f"{record['package']}/{record['arch']}: restore ({reason})",
                file=sys.stderr,
            )
    return result


def output_lines(result: dict) -> Iterator[str]:
    """Encode results as GITHUB_OUTPUT lines, rejecting embedded line breaks."""
    for name, value in result.items():
        encoded = (
            value
            if isinstance(value, str)
            else json.dumps(value, separators=(",", ":"))
        )
        if "\n" in encoded or "\r" in encoded:
            raise ValueError(f"output {name!r} would span multiple lines")
        yield f"{name}={encoded}"


def main(argv: list[str] | None = None) -> int:
    """CLI status: 0 success, 1 failure; stdout holds only GITHUB_OUTPUT lines."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("test")
    for name in ("packages", "extra-packages", "deps"):
        test.add_argument("--" + name, default="")
    publish = commands.add_parser("publish")
    for name in ("patch-root", "workflow-root"):
        publish.add_argument("--" + name, type=Path, required=True)
    for name in ("image", "repository-commit", "repository", "ref", "run"):
        publish.add_argument("--" + name, required=True)
    publish.add_argument("--force-rebuild", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = (
            plan_test(args.packages, args.extra_packages, args.deps)
            if args.command == "test"
            else run_publish(args)
        )
        for line in output_lines(result):
            print(line)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        print(f"plan_builds: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
