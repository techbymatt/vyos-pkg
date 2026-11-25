"""List build network targets without resolving refs or running any recipe."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    from . import package_catalog, package_sources
    from .workflow_utils import cli_error, command_output, output_lines
except ImportError:
    import package_catalog
    import package_sources
    from workflow_utils import cli_error, command_output, output_lines

VYOS_BUILD = "https://github.com/vyos/vyos-build.git"
VYOS_1X = "https://github.com/vyos/vyos-1x.git"


def validate_target(target: list[str]) -> list[str]:
    """Accept only direct, credential-free HTTP(S) URLs in public diagnostics."""
    method, label, url = target
    if method not in {"git", "get", "head"} or any(c in label for c in "\t\r\n|<>"):
        raise ValueError("invalid network probe method or label")
    if any(c.isspace() or c in "|\\<>`" or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError(f"invalid URL for {label}")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.fragment
        or (method == "git" and parsed.query)
    ):
        raise ValueError(f"expected a credential-free HTTP(S) URL for {label}")
    return target


def submodule_urls(root: Path) -> set[str]:
    """Read registered remotes from the already checked-out patch tree."""
    urls = set()
    for path in root.rglob(".gitmodules"):
        if path.is_symlink() or ".git" in path.relative_to(root).parts:
            continue
        try:
            entries = command_output(
                [
                    "git",
                    "config",
                    "--file",
                    str(path),
                    "--get-regexp",
                    r"^submodule\..*\.url$",
                ]
            )
        except subprocess.CalledProcessError as error:
            if error.returncode == 1:
                continue  # No submodule URL in this file.
            raise
        urls.update(line.split(maxsplit=1)[1] for line in entries.splitlines())
    return urls


def recipe_targets(root: Path) -> list[list[str]]:
    commit = package_sources.git(root, "rev-parse", "HEAD")
    upstream = package_sources.git(root, "rev-parse", "HEAD:vyos-build")
    print(f"Patch revision {commit}; vyos-build {upstream}", file=sys.stderr)
    sources = package_sources.discover_sources(
        package_sources.RecipeReader(root), package_catalog.load_catalog(), commit
    )
    urls = submodule_urls(root)
    urls.update(
        repository["url"]
        for source in sources.values()
        for repository in source["repositories"]
    )
    targets = [["git", "Git refs", url] for url in sorted(urls)]
    if kernel := sources.get(("build", "linux-kernel")):
        version = kernel["inputs"]["kernel_version"]
        if re.fullmatch(r"[0-9][0-9a-zA-Z.-]*", version) is None:
            raise ValueError("invalid kernel version for download probes")
        base = f"https://www.kernel.org/pub/linux/kernel/v6.x/linux-{version}"
        targets.extend(
            [
                ["head", "Kernel tarball", f"{base}.tar.xz"],
                ["get", "Kernel signature", f"{base}.tar.sign"],
            ]
        )
    return [validate_target(target) for target in targets]


def apt_targets() -> list[list[str]]:
    """Ask APT what it would request, without updating indexes or installing packages."""
    uris = command_output(
        ["apt-get", "--print-uris", "update"], timeout=20, env={"LC_ALL": "C"}
    )
    return [
        ["get" if fields[0].endswith("/InRelease") else "head", "APT index", fields[0]]
        for line in uris.splitlines()
        if (fields := shlex.split(line))
        and urlsplit(fields[0]).scheme in {"http", "https"}
    ]


def go_compiler_version() -> str:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/build-recipe.yaml"
    ).read_text()
    match = re.search(
        r"""^[ \t]+go-version: ["']?([0-9.]+)["']?[ \t]*$""", workflow, re.MULTILINE
    )
    if match is None:
        print(
            "Go archive probes skipped: no pinned version in build-recipe.yaml",
            file=sys.stderr,
        )
        return ""
    return match[1]


def build_targets(arch: str, repository: str) -> list[list[str]]:
    """Build independent probes, including when recipe checkout is unavailable."""
    resources = json.loads(
        Path(__file__).with_name("build_probe_resources.json").read_text()
    )
    go_version = go_compiler_version()
    targets = [
        ["git", "Workflow checkout", f"https://github.com/{repository}.git"],
        ["git", "Build submodule", VYOS_BUILD],
        ["git", "Recipe submodule", VYOS_1X],
        ["git", "Go toolchain manifest", "https://github.com/actions/go-versions.git"],
    ]
    targets.extend(
        ["git", "Standalone source", f"https://github.com/vyos/{entry['name']}.git"]
        for entry in package_catalog.load_catalog()["build-extra"]
    )
    targets.extend(
        [method, name, url.format(arch=arch, go_version=go_version)]
        for method, entries in resources.items()
        for name, url in entries.items()
        if go_version or "{go_version}" not in url
    )
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_subparsers(dest="mode", required=True)
    mode.add_parser("recipes").add_argument("--patch-root", type=Path, required=True)
    inventory = mode.add_parser("inventory")
    inventory.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    inventory.add_argument("--repository", required=True)
    inventory.add_argument("--recipes", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.mode == "recipes":
            for line in output_lines({"targets": recipe_targets(args.patch_root)}):
                print(line)
            return 0
        targets = build_targets(args.arch, args.repository)
        targets.extend(json.loads(args.recipes.read_text()))
        try:
            targets.extend(validate_target(target) for target in apt_targets())
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"APT inventory incomplete: {error}", file=sys.stderr)
            if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(summary, "a", encoding="utf-8") as report:
                    report.write(
                        "**Partial inventory:** APT index targets unavailable.\n\n"
                    )
        targets = [validate_target(target) for target in targets]
        seen = set()
        for method, label, url in targets:
            if (method, url) in seen:
                continue
            print(f"{method}\t{label}\t{url}")
            seen.add((method, url))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return cli_error("build_probe_targets", error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
