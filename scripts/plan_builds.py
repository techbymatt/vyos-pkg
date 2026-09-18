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
from pathlib import Path
from urllib.parse import quote

try:
    from . import (
        cache_namespace,
    )
    from . import (
        package_catalog as catalog,
    )
    from . import (
        publish_manifest as manifest,
    )
except ImportError:
    import cache_namespace
    import package_catalog as catalog
    import publish_manifest as manifest

RESTORE_BATCH_SIZE = 8


def runner_label(arch: str) -> str:
    return "ubuntu-24.04" if arch == "amd64" else "ubuntu-24.04-arm"


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
    return result


def source_records(sources: dict, revisions: dict[tuple[str, str], str]) -> list[dict]:
    records = []
    for group in catalog.GROUPS:
        for source in sources[group]:
            name = source["name"]
            commit = manifest.require_pattern(
                revisions[group, name], manifest.REVISION, f"{group}/{name} revision"
            )
            for arch in catalog.architectures(group, name, sources):
                records.append(
                    {
                        "group": group,
                        "package": name,
                        "arch": arch,
                        "commit": commit,
                        "deps": " ".join(source["deps"]),
                    }
                )
    return records


def visible_cache_keys(caches: list[dict], ref: str, default_branch: str) -> list[str]:
    """Newest first across both refs visible to the workflow cache restore."""
    visible = [c for c in caches if c["ref"] in (ref, f"refs/heads/{default_branch}")]
    return [
        c["key"] for c in sorted(visible, key=lambda c: c["created_at"], reverse=True)
    ]


def plan_publish(
    records: list[dict],
    cached_keys: list[str],
    namespace: str,
    run: str,
    *,
    changed: bool = True,
    force_rebuild: bool = False,
) -> dict:
    result = {
        name: {"include": []}
        for name in ("build-matrix", "build-extra-matrix", "restore-matrix")
    }
    if not changed and not force_rebuild:
        return result
    hits: dict[str, list[dict]] = {}
    for record in records:
        prefix = (
            f"cache-v2-{record['package']}-{record['arch']}-"
            f"{record['commit']}-{namespace}-"
        )
        key = (
            None
            if force_rebuild
            else next((key for key in cached_keys if key.startswith(prefix)), None)
        )
        entry = dict(
            record,
            runner_label=runner_label(record["arch"]),
            cache_key=key or prefix + run,
        )
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
    return result


def publication_changed(
    current: dict, published: object, force_rebuild: bool = False
) -> bool:
    if force_rebuild or published is None:
        return True
    try:
        return manifest.canonical_bytes(current) != manifest.canonical_bytes(published)
    except (ValueError, RecursionError):
        return True


def command_output(arguments: list[str], cwd: Path | None = None) -> str:
    return subprocess.check_output(arguments, cwd=cwd, text=True).strip()


def git(root: Path, *arguments: str) -> str:
    return command_output(["git", *arguments], cwd=root)


def resolve_revisions(patch_root: Path, sources: dict) -> dict[tuple[str, str], str]:
    revisions = {}
    for group in catalog.GROUPS:
        for source in sources[group]:
            name = source["name"]
            if group == "build":
                paths = [f"scripts/package-build/{name}/"]
                if name == "linux-kernel":
                    paths.insert(0, "data/defaults.toml")
                commit = git(
                    patch_root / "vyos-build",
                    "log",
                    "-n",
                    "1",
                    "--format=%H",
                    "--",
                    *paths,
                )
            else:
                remote = git(
                    patch_root,
                    "ls-remote",
                    f"https://github.com/vyos/{name}.git",
                    "refs/heads/rolling",
                )
                fields = remote.split()
                if len(fields) != 2 or fields[1] != "refs/heads/rolling":
                    raise ValueError(
                        f"missing or ambiguous rolling revision for {name}"
                    )
                commit = fields[0]
            revisions[group, name] = manifest.require_pattern(
                commit, manifest.REVISION, f"{name} revision"
            )
    return revisions


def fetch_caches(repository: str, ref: str) -> list[str]:
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
    sources = catalog.load_catalog(args.workflow_root / "scripts/package_catalog.json")
    patch_commit = git(args.patch_root, "rev-parse", "HEAD")
    shared = git(
        args.patch_root / "vyos-build", "ls-tree", "HEAD", "scripts/package-build/"
    )
    shared = "\n".join(
        line for line in shared.splitlines() if line.split()[1] != "tree"
    )
    namespace = cache_namespace.namespace(
        args.image,
        git(args.patch_root, "rev-parse", "HEAD:patches"),
        shared,
        git(args.patch_root / "vyos-build", "rev-parse", "HEAD:data"),
        args.workflow_root,
    )
    records = source_records(sources, resolve_revisions(args.patch_root, sources))
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
    result = {"patch-commit": patch_commit, "changed": changed}
    result.update(
        plan_publish(
            records,
            keys,
            namespace,
            args.run,
            changed=changed,
            force_rebuild=args.force_rebuild,
        )
    )
    print(
        f"Publication inputs changed (or refresh forced): {str(changed).lower()}",
        file=sys.stderr,
    )
    for name in ("build-matrix", "build-extra-matrix", "restore-matrix"):
        print(f"{name}: {len(result[name]['include'])} jobs", file=sys.stderr)
    return result


def main(argv: list[str] | None = None) -> int:
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
        for name, value in result.items():
            encoded = (
                value
                if isinstance(value, str)
                else json.dumps(value, separators=(",", ":"))
            )
            print(f"{name}={encoded}")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"plan_builds: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
