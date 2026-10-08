#!/usr/bin/env python3
"""Plan Test and Publish jobs; stdout contains only GITHUB_OUTPUT lines.

Pure matrix functions are separate from revision, cache and deployed-manifest
lookups. The local input manifest is a candidate for deployment, never a marker
of successful publication. Only the deployed manifest can suppress publication.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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
    from .build_matrix import (
        RESTORE_BATCH_SIZE,
        parse_input,
        plan_publish,
        plan_test,
        runner_label,
        source_records,
    )
    from .package_cache import cache_prefix, visible_cache_keys
    from .workflow_utils import atomic_write, command_output, output_lines
except ImportError:
    import legacy_package_cache
    import package_catalog as catalog
    import package_sources
    import publish_manifest as manifest
    from build_matrix import (
        RESTORE_BATCH_SIZE,
        parse_input,
        plan_publish,
        plan_test,
        runner_label,
        source_records,
    )
    from package_cache import cache_prefix, visible_cache_keys
    from workflow_utils import atomic_write, command_output, output_lines

__all__ = [
    "RESTORE_BATCH_SIZE",
    "cache_prefix",
    "output_lines",
    "parse_input",
    "plan_publish",
    "plan_test",
    "runner_label",
    "source_records",
    "visible_cache_keys",
]


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


def git(root: Path, *arguments: str) -> str:
    """Run a git command in a repository and return its stripped stdout."""
    return command_output(["git", *arguments], cwd=root)


def fetch_caches(repository: str, ref: str) -> tuple[list[str], list[dict]]:
    """Return visible keys and inaccessible cache metadata for diagnostics only."""
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
    caches = [cache for page in pages for cache in page["actions_caches"]]
    return (
        visible_cache_keys(caches, ref, default_branch),
        [
            cache
            for cache in caches
            if cache["ref"] not in (ref, f"refs/heads/{default_branch}")
        ],
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
        subprocess.TimeoutExpired,
        ValueError,
        RecursionError,
    ) as error:
        print(
            f"Published manifest unavailable or invalid; publishing: {error}",
            file=sys.stderr,
        )
        return None


def source_changes(previous: dict, current: dict) -> list[str]:
    """Describe changed source fields without confusing recipe and checkout SHAs."""
    old, new = previous.get("source", {}), current["source"]
    changes = []
    if old.get("recipe_tree") != new["recipe_tree"]:
        changes.append(f"recipe_tree: {old.get('recipe_tree')} -> {new['recipe_tree']}")
    old_inputs, new_inputs = old.get("inputs", {}), new["inputs"]
    for name in sorted(old_inputs.keys() | new_inputs.keys()):
        before, after = old_inputs.get(name), new_inputs.get(name)
        if before != after:
            changes.append(f"{name}: {before} -> {after}")
    old_repos = {entry["name"]: entry for entry in old.get("repositories", [])}
    new_repos = {entry["name"]: entry for entry in new["repositories"]}
    for name in sorted(old_repos.keys() | new_repos.keys()):
        if name not in old_repos:
            changes.append(f"{name}: repository added ({new_repos[name]['commit']})")
        elif name not in new_repos:
            changes.append(f"{name}: repository removed ({old_repos[name]['commit']})")
        else:
            for field in ("url", "ref", "commit"):
                before, after = old_repos[name][field], new_repos[name][field]
                if before != after:
                    changes.append(f"{name} {field}: {before} -> {after}")
    return changes


def build_reason(
    record: dict,
    previous: dict,
    inaccessible: list[dict],
    ref: str,
    *,
    legacy_note: str | None = None,
    force_rebuild: bool = False,
) -> str:
    """Explain the final build decision; rejected legacy fallbacks are secondary."""
    if force_rebuild:
        return "forced rebuild"
    reasons = []
    changed = previous.get("source_digest") not in (None, record["source_digest"])
    if changed:
        changes = source_changes(previous, record) or [
            f"fingerprint: {previous['source_digest']} -> {record['source_digest']}"
        ]
        reasons.append("source inputs changed: " + ", ".join(changes))
    refs = sorted(
        {
            cache["ref"]
            for cache in inaccessible
            if cache["key"].startswith(cache_prefix(record))
        }
    )
    if refs:
        reasons.append(
            f"matching source cache inaccessible from {ref}: " + ", ".join(refs)
        )
    if legacy_note and not changed:
        reasons.append("legacy migration requires an initial build: " + legacy_note)
    return "; ".join(reasons) or "no matching visible source cache"


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
        atomic_write(
            args.workflow_root / "input-manifest.json",
            manifest.canonical_bytes(current),
        )
        published = (
            None if args.force_rebuild else fetch_published(args.repository, args.run)
        )
        changed = publication_changed(current, published, args.force_rebuild)
        # Cache metadata cannot change a no-op or a forced build decision.
        # Avoid paginated API requests (and legacy lookups) in both cases.
        keys, inaccessible = (
            fetch_caches(args.repository, args.ref)
            if changed and not args.force_rebuild
            else ([], [])
        )
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
            sources=sources,
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
            reason = build_reason(
                record,
                old,
                inaccessible,
                args.ref,
                legacy_note=note,
                force_rebuild=args.force_rebuild,
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
                f"{record['package']}/{record['arch']}: restore ({reason}; cache {record['cache_key']})",
                file=sys.stderr,
            )
    return result


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
