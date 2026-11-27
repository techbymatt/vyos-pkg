"""Bounded Git ref resolution for package sources.

Only refs actually built are resolved. Remotes are queried concurrently with
one ls-remote snapshot per URL; abbreviated non-GitHub commits expand through
commit APIs or metadata-only clones shared by every ref of that URL.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    from .git_network import clone_repository, remote_git
    from .recipe_inputs import RecipeReader, discover_sources
    from .source_identity import (
        REVISION,
        fingerprint,
        revision,
        source_text,
        validate_source,
    )
    from .workflow_utils import command_output, git
except ImportError:
    from git_network import clone_repository, remote_git
    from recipe_inputs import RecipeReader, discover_sources
    from source_identity import (
        REVISION,
        fingerprint,
        revision,
        source_text,
        validate_source,
    )
    from workflow_utils import command_output, git

SHORT_REVISION = r"[0-9a-fA-F]{7,39}"
GITLAB_HOSTS = {"salsa.debian.org", "gitlab.com", "gitlab.isc.org"}
SOURCE_JOBS = 8


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
                output = remote_git("ls-remote", "--", url)
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

    def resolve_many(
        self, repositories: list[dict], *, jobs: int = SOURCE_JOBS
    ) -> dict[tuple[str, str], str]:
        """Resolve independent remotes concurrently, serializing refs per URL.

        All requests for one URL share one ls-remote snapshot and metadata clone.
        Do not parallelize recipes or isolated-index patching.
        Results and exceptions are consumed in deterministic URL order.
        """
        if jobs < 1:
            raise ValueError("source lookup jobs must be positive")
        grouped: dict[str, list[str]] = {}
        for entry in repositories:
            url, ref = (
                source_text(entry["url"], "URL"),
                source_text(entry["ref"], "ref"),
            )
            if ref not in grouped.setdefault(url, []):
                grouped[url].append(ref)

        def resolve_url(url: str) -> dict[tuple[str, str], str]:
            return {(url, ref): self.resolve(url, ref) for ref in grouped[url]}

        result = {}
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            for resolved in executor.map(resolve_url, sorted(grouped)):
                result.update(resolved)
        return result

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
                # Parallel URL lookups must not race for the same clone directory.
                directory = (
                    Path(self.temporary.name) / hashlib.sha256(url.encode()).hexdigest()
                )
                clone_repository(
                    directory, url, "--bare", "--filter=blob:none", timeout=300
                )
                self.repositories[url] = directory
            commit = git(
                self.repositories[url], "rev-parse", "--verify", f"{ref}^{{commit}}"
            )
        return revision(commit)


def resolve_sources(
    reader: RecipeReader,
    resolver: Resolver,
    sources: dict,
    patch_commit: str,
    *,
    jobs: int = SOURCE_JOBS,
) -> dict:
    """Discover sources serially, then resolve independent remotes in parallel."""
    descriptions = discover_sources(reader, sources, patch_commit)
    revisions = {
        (group, name): reader.recipe_revision(name, patch_commit)
        for group, name in descriptions
        if group == "build"
    }
    commits = resolver.resolve_many(
        [entry for source in descriptions.values() for entry in source["repositories"]],
        jobs=jobs,
    )
    result = {}
    for identity, source in descriptions.items():
        for repository in source["repositories"]:
            repository["commit"] = commits[repository["url"], repository["ref"]]
        source = validate_source(source)
        result[identity] = {
            "commit": revisions.get(identity) or source["repositories"][0]["commit"],
            "source": source,
            "source_digest": fingerprint(source),
        }
    return result
