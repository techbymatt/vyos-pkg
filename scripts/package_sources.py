"""Fingerprint package sources and pin recipe checkouts to planned commits.

Only a package's patched recipe, the Git revisions it builds, and explicitly
audited out-of-folder source inputs contribute to its fingerprint. Build images,
local orchestration/adaptations, shared builders, and unrelated patches do not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import tomllib

try:
    from . import package_catalog as catalog
    from .prepare_package_build import replace_once
except ImportError:
    import package_catalog as catalog
    from prepare_package_build import replace_once

REVISION = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
SHORT_REVISION = r"[0-9a-fA-F]{7,39}"
PIN_ENV = "PACKAGE_SOURCE_REVISIONS"
KEA_CLONE = "git clone --branch ${BRANCH} "
GITLAB_HOSTS = {"salsa.debian.org", "gitlab.com", "gitlab.isc.org"}


def revision(value: object) -> str:
    """Require a full, lowercase Git object ID."""
    if not isinstance(value, str) or re.fullmatch(REVISION, value) is None:
        raise ValueError(f"invalid source revision: {value!r}")
    return value


def source_text(value: object, field: str) -> str:
    """Require nonempty single-line text that cannot be a command-line option."""
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("-")
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"invalid source {field}: {value!r}")
    return value


def validate_repositories(value: object) -> list[dict]:
    """Validate and sort resolved repositories without changing ref labels."""
    if not isinstance(value, list):
        raise TypeError("source repositories must be an array")
    result = []
    seen = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "url",
            "ref",
            "commit",
        }:
            raise ValueError("source repository requires name/url/ref/commit")
        name = catalog.validate_name(entry["name"])
        if name in seen:
            raise ValueError(f"duplicate source repository: {name}")
        seen.add(name)
        result.append(
            {
                "name": name,
                "url": source_text(entry["url"], "URL"),
                "ref": source_text(entry["ref"], "ref"),
                "commit": revision(entry["commit"]),
            }
        )
    return sorted(result, key=lambda entry: entry["name"])


def validate_source(value: object) -> dict:
    """Validate the canonical per-source descriptor used by caches/manifests."""
    if not isinstance(value, dict) or set(value) != {
        "recipe_tree",
        "inputs",
        "repositories",
    }:
        raise ValueError("source requires recipe_tree/inputs/repositories")
    tree = value["recipe_tree"]
    if tree is not None:
        revision(tree)
    inputs = value["inputs"]
    if not isinstance(inputs, dict):
        raise TypeError("source inputs must be an object")
    for key, text in inputs.items():
        if not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]*", key) is None:
            raise ValueError("invalid scoped source input name")
        source_text(text, f"input {key}")
    repositories = validate_repositories(value["repositories"])
    if tree is None and (inputs or len(repositories) != 1):
        raise ValueError(
            "standalone sources require exactly one repository and no inputs"
        )
    return {"recipe_tree": tree, "inputs": dict(inputs), "repositories": repositories}


def canonical_bytes(value: object) -> bytes:
    """Serialize source data with deterministic keys and preserved boundaries."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def fingerprint(source: dict) -> str:
    """Hash source identities, never local tooling or a global namespace."""
    return hashlib.sha256(canonical_bytes(validate_source(source))).hexdigest()


def cache_prefix(record: dict) -> str:
    """Identify one source/architecture without a global build-input namespace."""
    return (
        f"cache-v3-{record['group']}-{record['package']}-{record['arch']}-"
        f"{record['source_digest']}-"
    )


def command_output(
    arguments: list[str],
    cwd: Path | None = None,
    *,
    env: dict | None = None,
    input_text: str | None = None,
    timeout: int = 120,
    strip: bool = True,
) -> str:
    """Run a bounded, noninteractive command and return stripped stdout."""
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout,
    ).stdout
    return result.strip() if strip else result


def git(root: Path, *arguments: str, **kwargs) -> str:
    """Run Git in a repository without altering persistent Git configuration."""
    return command_output(["git", *arguments], cwd=root, **kwargs)


class Resolver:
    """Resolve only refs actually built, memoizing remote and commit lookups."""

    def __init__(self) -> None:
        """Create temporary metadata storage for abbreviated non-GitHub commits."""
        self.temporary = tempfile.TemporaryDirectory(prefix="package-revisions-")
        self.commits: dict[tuple[str, str], str] = {}
        self.remotes: dict[str, dict[str, str]] = {}
        self.repositories: dict[str, Path] = {}

    def close(self) -> None:
        """Remove metadata-only clones after planning."""
        self.temporary.cleanup()

    def resolve(self, url: str, ref: str) -> str:
        """Resolve full/abbreviated commits, branches, and peeled annotated tags."""
        source_text(url, "URL")
        source_text(ref, "ref")
        identity = url, ref
        if identity in self.commits:
            return self.commits[identity]
        if re.fullmatch(REVISION, ref.lower()):
            commit = ref.lower()
        else:
            if url not in self.remotes:
                output = command_output(["git", "ls-remote", "--", url])
                self.remotes[url] = {
                    name: revision(sha)
                    for sha, name in (line.split() for line in output.splitlines())
                }
            remote = self.remotes[url]
            names = (
                [ref]
                if ref == "HEAD" or ref.startswith("refs/")
                else [f"refs/heads/{ref}", f"refs/tags/{ref}"]
            )
            matches = [name for name in names if name in remote]
            if len(matches) > 1:
                raise ValueError(f"missing or ambiguous source ref {url} {ref}")
            if matches:
                # Ref names take precedence over abbreviated object IDs. In
                # particular, firmware's YYYYMMDD tags contain only hex digits.
                name = matches[0]
                commit = remote.get(name + "^{}", remote[name])
            elif re.fullmatch(SHORT_REVISION, ref):
                commit = self.abbreviated_commit(url, ref)
            else:
                raise ValueError(f"missing or ambiguous source ref {url} {ref}")
        self.commits[identity] = revision(commit)
        return commit

    def abbreviated_commit(self, url: str, ref: str) -> str:
        """Expand a commit ID through a commit API or metadata-only Git clone."""
        parsed = urlsplit(url)
        path = parsed.path.removesuffix(".git").strip("/")
        if parsed.hostname == "github.com" and len(path.split("/")) == 2:
            commit = command_output(
                [
                    "gh",
                    "api",
                    f"repos/{path}/commits/{quote(ref, safe='')}",
                    "--jq",
                    ".sha",
                ]
            )
        elif parsed.hostname in GITLAB_HOSTS:
            endpoint = (
                f"https://{parsed.netloc}/api/v4/projects/{quote(path, safe='')}/"
                f"repository/commits/{quote(ref, safe='')}?stats=false"
            )
            commit = json.loads(
                command_output(
                    [
                        "curl",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--location",
                        "--connect-timeout",
                        "10",
                        "--max-time",
                        "60",
                        endpoint,
                    ]
                )
            )["id"]
        else:
            if url not in self.repositories:
                directory = Path(self.temporary.name) / str(len(self.repositories))
                command_output(
                    [
                        "git",
                        "clone",
                        "--bare",
                        "--filter=blob:none",
                        "--",
                        url,
                        str(directory),
                    ],
                    timeout=300,
                )
                self.repositories[url] = directory
            commit = git(
                self.repositories[url], "rev-parse", "--verify", f"{ref}^{{commit}}"
            )
        return revision(commit)

    def resolve_evidence(self, url: str, abbreviated: str, expected: str) -> str:
        """Expand legacy IDs without cloning large repositories just for proof.

        Commit APIs and metadata clones already needed by current recipes can
        resolve abbreviated IDs unambiguously. Other hosts need a full recorded
        ID; a tag name or matching short prefix alone is not sufficient proof.
        """
        if (
            re.fullmatch(REVISION, abbreviated) is None
            and (url, abbreviated) not in self.commits
            and url not in self.repositories
            and urlsplit(url).hostname not in GITLAB_HOSTS | {"github.com"}
        ):
            raise ValueError(
                "producer recorded only an unverifiable abbreviated checkout"
            )
        identity = url, abbreviated
        if identity not in self.commits:
            self.commits[identity] = (
                revision(abbreviated)
                if re.fullmatch(REVISION, abbreviated)
                else self.abbreviated_commit(url, abbreviated)
            )
        actual = self.commits[identity]
        if actual != expected or not actual.startswith(abbreviated.lower()):
            raise ValueError("recipe-cloned source revision changed")
        return actual


class RecipeReader:
    """Read current/historical patched recipes through isolated Git indexes.

    Applying the same patches with --cached --3way produces the same tracked
    tree as the build checkout, without rewriting its worktree or real index.
    Historical trees are used only to prove legacy-cache source identities.
    """

    def __init__(self, patch_root: Path) -> None:
        """Keep patched tree results shared across all package architectures."""
        self.patch_root = patch_root
        self.root = patch_root / "vyos-build"
        self.trees: dict[str, tuple[str, str]] = {}

    def patched_tree(self, patch_commit: str) -> tuple[str, str]:
        """Return the pinned upstream commit and downstream-patched tree."""
        revision(patch_commit)
        if patch_commit not in self.trees:
            entry = git(self.patch_root, "ls-tree", patch_commit, "vyos-build").split()
            if len(entry) != 4 or entry[:2] != ["160000", "commit"]:
                raise ValueError("patch repository must pin the vyos-build submodule")
            upstream = revision(entry[2])
            patches = git(
                self.patch_root,
                "ls-tree",
                "-r",
                "--name-only",
                patch_commit,
                "--",
                "patches/vyos-build/",
            ).splitlines()
            with tempfile.TemporaryDirectory(prefix="package-index-") as temporary:
                env = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
                git(self.root, "read-tree", upstream, env=env)
                for path in sorted(path for path in patches if path.endswith(".patch")):
                    patch = git(
                        self.patch_root, "show", f"{patch_commit}:{path}", strip=False
                    )
                    git(
                        self.root,
                        "apply",
                        "--cached",
                        "--3way",
                        "-",
                        env=env,
                        input_text=patch,
                    )
                tree = revision(git(self.root, "write-tree", env=env))
            self.trees[patch_commit] = upstream, tree
        return self.trees[patch_commit]

    def text(self, tree: str, path: str) -> str:
        """Read a tracked file from a patched tree."""
        return git(self.root, "show", f"{tree}:{path}", strip=False)

    def recipe(self, package: str, patch_commit: str) -> dict:
        """Describe one recipe's patched files, scoped inputs, and Git refs."""
        catalog.validate_name(package)
        _, tree = self.patched_tree(patch_commit)
        directory = f"scripts/package-build/{package}"
        recipe_tree = revision(git(self.root, "rev-parse", f"{tree}:{directory}"))
        config = tomllib.loads(self.text(tree, f"{directory}/package.toml"))
        repositories = []
        for entry in config["packages"]:
            url, ref = entry.get("scm_url", ""), entry.get("commit_id", "")
            if url or ref:
                repositories.append(
                    {
                        "name": catalog.validate_name(entry["name"]),
                        "url": source_text(url, "URL"),
                        "ref": source_text(ref, "ref"),
                    }
                )
        if package == "isc-kea":
            repositories.append(
                kea_repository(self.text(tree, f"{directory}/prebuild.sh"))
            )
        inputs = {}
        if package == "linux-kernel":
            defaults = tomllib.loads(self.text(tree, "data/defaults.toml"))
            kernel = next(
                entry for entry in config["packages"] if entry["name"] == "linux-kernel"
            )
            inputs = {
                key: source_text(kernel.get(key, defaults.get(key)), key)
                for key in ("kernel_version", "kernel_flavor")
            }
            certificates = [
                line
                for line in git(
                    self.root, "ls-tree", "-r", tree, "--", "data/certificates/"
                ).splitlines()
                if line.startswith(("100644 blob ", "100755 blob "))
                and line.endswith(".pem")
            ]
            inputs["certificates"] = hashlib.sha256(
                canonical_bytes(certificates)
            ).hexdigest()
        return {
            "recipe_tree": recipe_tree,
            "inputs": inputs,
            "repositories": repositories,
        }

    def legacy_revision(self, package: str, patch_commit: str) -> str:
        """Retain the original path revision for diagnostics and legacy lookups."""
        upstream, _ = self.patched_tree(patch_commit)
        paths = [f"scripts/package-build/{package}/"]
        if package == "linux-kernel":
            paths.insert(0, "data/defaults.toml")
        return revision(
            git(self.root, "log", "-n", "1", "--format=%H", upstream, "--", *paths)
        )


def kea_repository(script: str) -> dict:
    """Read the audited extra packaging clone, failing on command/variable drift."""
    branches = re.findall(r'^BRANCH="([^"$]+)"$', script, re.MULTILINE)
    clones = re.findall(
        r"^git clone --branch \$\{BRANCH\} (\S+)$", script, re.MULTILINE
    )
    if len(branches) != 1 or len(clones) != 1:
        raise ValueError("isc-kea: expected one audited packaging repository clone")
    return {
        "name": "kea-packaging",
        "url": source_text(clones[0], "URL"),
        "ref": source_text(branches[0], "ref"),
    }


def resolve_sources(
    reader: RecipeReader, resolver: Resolver, sources: dict, patch_commit: str
) -> dict:
    """Resolve the entire catalog once per source, not once per architecture."""
    result = {}
    for group in catalog.GROUPS:
        for entry in sources[group]:
            name = entry["name"]
            if group == "build":
                source = reader.recipe(name, patch_commit)
                commit = reader.legacy_revision(name, patch_commit)
            else:
                source = {
                    "recipe_tree": None,
                    "inputs": {},
                    "repositories": [
                        {
                            "name": name,
                            "url": f"https://github.com/vyos/{name}.git",
                            "ref": "rolling",
                        }
                    ],
                }
                commit = resolver.resolve(source["repositories"][0]["url"], "rolling")
            for repository in source["repositories"]:
                repository["commit"] = resolver.resolve(
                    repository["url"], repository["ref"]
                )
            source = validate_source(source)
            result[group, name] = {
                "commit": commit,
                "source": source,
                "source_digest": fingerprint(source),
            }
    return result


def checkout_source(directory: Path, url: str, ref: str) -> None:
    """Check out the planned commit, preserving original ref labels in recipes."""
    repositories = validate_repositories(json.loads(os.environ[PIN_ENV]))
    matches = [
        entry["commit"]
        for entry in repositories
        if (entry["url"], entry["ref"]) == (url, ref)
    ]
    if len(set(matches)) != 1:
        raise ValueError(f"missing or ambiguous planned checkout {url} {ref}")
    commit = matches[0]
    try:
        git(directory, "cat-file", "-e", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError:
        git(directory, "fetch", "origin", commit, timeout=300)
    git(directory, "checkout", "--detach", commit)
    print(f"Source checkout: {url} {ref} -> {commit}", flush=True)


def pin_recipe(root: Path, package: str, repositories: list[dict]) -> None:
    """Adapt audited Git checkout commands without rewriting version labels."""
    repositories = validate_repositories(repositories)
    directory = root / catalog.validate_name(package)
    config = tomllib.loads((directory / "package.toml").read_text())
    expected = [
        {"name": entry["name"], "url": entry["scm_url"], "ref": entry["commit_id"]}
        for entry in config["packages"]
        if entry.get("scm_url") or entry.get("commit_id")
    ]
    if package == "isc-kea":
        expected.append(kea_repository((directory / "prebuild.sh").read_text()))
    actual = [
        {key: entry[key] for key in ("name", "url", "ref")} for entry in repositories
    ]
    if sorted(expected, key=lambda entry: entry["name"]) != actual:
        raise ValueError(
            f"{package}: planned repositories do not match the patched recipe"
        )
    builder = directory / "build.py"
    if package == "linux-kernel":
        command = "run(['git', 'checkout', commit_id], cwd=repo_dir, check=True)"
        text = builder.read_text()
        if text.count(command) != 2:
            raise ValueError(
                "linux-kernel: expected two audited source checkout commands"
            )
        builder.write_text(
            text.replace(command, "checkout_source(repo_dir, scm_url, commit_id)")
        )
    else:
        if not builder.is_symlink() or os.readlink(builder) != "../build.py":
            raise ValueError(f"{package}: unaudited custom source builder")
        builder = root / "build.py"
        replace_once(
            builder,
            "run(['git', 'checkout', package['commit_id']], cwd=repo_dir, check=True)",
            "checkout_source(repo_dir, package['scm_url'], package['commit_id'])",
        )
    replace_once(
        builder,
        "from subprocess import run, CalledProcessError",
        "from subprocess import run, CalledProcessError\nfrom package_sources import checkout_source",
    )
    if package == "isc-kea":
        repository = next(
            entry for entry in repositories if entry["name"] == "kea-packaging"
        )
        replace_once(
            directory / "prebuild.sh",
            KEA_CLONE + repository["url"],
            "python3 -m package_sources checkout --directory kea-packaging --url "
            + shlex.quote(repository["url"])
            + ' --ref "$BRANCH"',
        )


def main(argv: list[str] | None = None) -> int:
    """Pin recipe commands or perform a nested clone at its planned revision."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pin = commands.add_parser("pin")
    pin.add_argument("--root", type=Path, required=True)
    pin.add_argument("--package", required=True)
    pin.add_argument("--sources", required=True)
    checkout = commands.add_parser("checkout")
    checkout.add_argument("--directory", type=Path, required=True)
    checkout.add_argument("--url", required=True)
    checkout.add_argument("--ref", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "pin":
            pin_recipe(args.root, args.package, json.loads(args.sources))
        else:
            source_text(args.url, "URL")
            if not args.directory.exists():
                command_output(
                    ["git", "clone", "--", args.url, str(args.directory)], timeout=300
                )
            checkout_source(args.directory, args.url, args.ref)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        print(f"package_sources: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
