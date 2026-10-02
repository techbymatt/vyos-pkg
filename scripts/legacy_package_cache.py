"""Prove legacy cache source identities before migrating their archives.

The old namespace is not proof of recipe-cloned revisions. Producer manifests,
the historical patched recipe, and successful build-step checkout logs must
agree with today's source descriptor. Missing/ambiguous evidence is a miss for
that entry, never a reason to trust the newest namespace or tag name blindly.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import PurePosixPath

try:
    from . import package_sources as sources
    from . import publish_manifest as manifest
except ImportError:
    import package_sources as sources
    import publish_manifest as manifest

LEGACY_KEY = re.compile(
    r"^cache-v2-(?P<package>[a-z0-9][a-z0-9_+.-]*)-(?P<arch>amd64|arm64)-"
    r"(?P<commit>[0-9a-f]{40}|[0-9a-f]{64})-(?P<namespace>[0-9a-f]{64})-"
    r"(?P<run>[0-9]+)-(?P<attempt>[0-9]+)$"
)
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PROOF_TIMEOUT = 600
MAX_MANIFEST_BYTES = 1024 * 1024


def archive_manifest(data: bytes) -> dict:
    """Read a bounded producer manifest from its artifact without extracting files."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = archive.infolist()
        if (
            len(files) != 1
            or files[0].filename != "input-manifest.json"
            or files[0].file_size > MAX_MANIFEST_BYTES
        ):
            raise ValueError("invalid producer manifest artifact")
        return manifest.validate_manifest(
            json.loads(archive.read(files[0]), object_pairs_hook=manifest.unique_object)
        )


def source_directory(package: str, name: str) -> str:
    """Map audited kernel driver entries to their actual clone directories."""
    if package == "linux-kernel" and name in {
        "igb",
        "ixgbe",
        "ixgbevf",
        "i40e",
        "ice",
        "iavf",
    }:
        return f"ethernet-linux-{name}"
    return name


def checkout_evidence(
    log: str, job: dict, package: str, repositories: list[dict]
) -> dict:
    """Associate checkout IDs with clone directories only inside the build step.

    Earlier workflow/submodule checkouts must not be confused with a moving
    source branch which emits no actual HEAD. --branch tag clones can emit
    'Note: switching to ...' instead of the common builder's 'HEAD is now at'.
    Only full IDs in those notes are evidence: a hexadecimal-looking ref such
    as the firmware tag '20260410' is not an abbreviated checkout ID.
    """
    steps = [step for step in job["steps"] if step["name"] == "Build the package"]
    if len(steps) != 1 or steps[0]["conclusion"] != "success":
        raise ValueError("producer did not execute one successful package build")
    step = steps[0]
    start = datetime.fromisoformat(step["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(step["completed_at"].replace("Z", "+00:00"))
    directories = {
        source_directory(package, entry["name"]): entry["name"]
        for entry in repositories
    }
    commits = {}
    parent_refs = set()
    active = None
    for line in log.splitlines():
        line = ANSI.sub("", line)
        timestamp = TIMESTAMP.search(line)
        if timestamp is None:
            continue
        when = datetime.fromisoformat(timestamp[0].replace("Z", "+00:00"))
        text = line[timestamp.end() :].strip()
        if when < start:
            match = re.fullmatch(r"ref: ([0-9a-f]{40}|[0-9a-f]{64})", text)
            if match:
                parent_refs.add(match[1])
        if not start <= when <= end:
            continue
        clone = re.fullmatch(r"Cloning into '([^']+)'\.\.\.", text)
        if clone:
            active = directories.get(PurePosixPath(clone[1]).name)
        checkout = re.match(r"HEAD is now at ([0-9a-f]{7,64})\b", text)
        if checkout is None:
            checkout = re.match(rf"Note: switching to '({sources.REVISION})'", text)
        if active and checkout:
            previous = commits.get(active)
            if previous is not None and not (
                previous.startswith(checkout[1]) or checkout[1].startswith(previous)
            ):
                raise ValueError(f"ambiguous producer checkout for {active}")
            # A following abbreviated HEAD must not discard a full clone ID.
            commits[active] = max((previous or "", checkout[1]), key=len)
    if len(parent_refs) != 1:
        raise ValueError("producer patch checkout is missing or ambiguous")
    return {"patch_commit": parent_refs.pop(), "commits": commits}


class LegacyVerifier:
    """Memoized, time-bounded proof lookup for visible legacy cache entries."""

    def __init__(
        self, repository: str, reader: sources.RecipeReader, resolver: sources.Resolver
    ):
        """Share source metadata lookups with the current publication planner."""
        self.repository = repository
        self.reader = reader
        self.resolver = resolver
        self.deadline = time.monotonic() + PROOF_TIMEOUT
        self.manifests: dict[str, dict | None] = {}
        self.jobs: dict[tuple[str, str], list[dict] | None] = {}
        self.evidence: dict[int, dict | None] = {}
        self.reasons: dict[tuple[str, str, str], str] = {}

    def api(self, endpoint: str, *, paginate: bool = False) -> object:
        """Read GitHub producer metadata, combining every page before selection."""
        command = ["gh", "api"]
        if paginate:
            command.extend(("--paginate", "--slurp"))
        command.append(f"repos/{self.repository}/{endpoint}")
        return json.loads(sources.command_output(command, timeout=60))

    def producer_manifest(self, run: str) -> dict:
        """Load the producer's candidate inputs once, regardless of deployment."""
        if run not in self.manifests:
            self.manifests[run] = None
            pages = self.api(
                f"actions/runs/{run}/artifacts?per_page=100", paginate=True
            )
            artifacts = [
                entry
                for page in pages
                for entry in page["artifacts"]
                if entry["name"] == "input-manifest" and not entry["expired"]
            ]
            if len(artifacts) != 1:
                raise ValueError("producer manifest artifact unavailable or ambiguous")
            data = subprocess.check_output(
                [
                    "gh",
                    "api",
                    f"repos/{self.repository}/actions/artifacts/{artifacts[0]['id']}/zip",
                ],
                timeout=60,
                stderr=subprocess.PIPE,
            )
            self.manifests[run] = archive_manifest(data)
        value = self.manifests[run]
        if value is None:
            raise ValueError("producer manifest unavailable")
        return value

    def producer_job(self, run: str, attempt: str, record: dict) -> dict:
        """Require the successful producer from the cache's original run attempt."""
        identity = run, attempt
        if identity not in self.jobs:
            self.jobs[identity] = None
            pages = self.api(
                f"actions/runs/{run}/attempts/{attempt}/jobs?per_page=100",
                paginate=True,
            )
            self.jobs[identity] = [job for page in pages for job in page["jobs"]]
        marker = f"({record['group']}, {record['package']}, {record['arch']}, {record['commit']},"
        jobs = [job for job in (self.jobs[identity] or []) if marker in job["name"]]
        if len(jobs) != 1 or jobs[0]["conclusion"] != "success":
            raise ValueError("successful producer job unavailable or ambiguous")
        return jobs[0]

    def producer_evidence(
        self, job: dict, package: str, repositories: list[dict]
    ) -> dict:
        """Retain parsed evidence, not large package compilation logs, in memory."""
        identifier = job["id"]
        if identifier not in self.evidence:
            self.evidence[identifier] = None
            log = sources.command_output(
                [
                    "gh",
                    "api",
                    f"repos/{self.repository}/actions/jobs/{identifier}/logs",
                    "--allow-escape-sequences",
                ],
                timeout=120,
            )
            self.evidence[identifier] = checkout_evidence(
                log, job, package, repositories
            )
        value = self.evidence[identifier]
        if value is None:
            raise ValueError("producer checkout evidence unavailable")
        return value

    def verify(self, record: dict, key: str) -> bool:
        """Prove all source inputs for one entry; incomplete evidence is not a hit."""
        identity = manifest.package_identity(record)
        try:
            parsed = LEGACY_KEY.fullmatch(key)
            if parsed is None or (parsed["package"], parsed["arch"]) != (
                record["package"],
                record["arch"],
            ):
                return False
            if time.monotonic() >= self.deadline:
                raise ValueError("legacy source proof time budget exhausted")
            producer = self.producer_manifest(parsed["run"])
            rows = [
                row
                for row in producer["packages"]
                if manifest.package_identity(row) == identity
            ]
            if len(rows) != 1 or rows[0]["commit"] != parsed["commit"]:
                raise ValueError("producer inputs do not match the cache key")
            old = rows[0]
            job = self.producer_job(parsed["run"], parsed["attempt"], old)
            if record["group"] == "build-extra":
                if old["commit"] != record["commit"]:
                    raise ValueError("standalone source revision changed")
            else:
                source = self.reader.recipe(record["package"], producer["patch_commit"])
                if (
                    self.reader.legacy_revision(
                        record["package"], producer["patch_commit"]
                    )
                    != old["commit"]
                ):
                    raise ValueError(
                        "historical recipe revision does not match producer inputs"
                    )
                current = record["source"]
                if (source["recipe_tree"], source["inputs"]) != (
                    current["recipe_tree"],
                    current["inputs"],
                ):
                    raise ValueError("patched recipe or scoped source inputs changed")
                evidence = self.producer_evidence(
                    job, record["package"], source["repositories"]
                )
                if evidence["patch_commit"] != producer["patch_commit"]:
                    raise ValueError("producer checked out different patch inputs")
                for entry in source["repositories"]:
                    abbreviated = evidence["commits"].get(entry["name"])
                    if abbreviated is None:
                        raise ValueError(
                            f"producer did not record the {entry['name']} checkout"
                        )
                    expected = [
                        item["commit"]
                        for item in current["repositories"]
                        if (item["name"], item["url"], item["ref"])
                        == (entry["name"], entry["url"], entry["ref"])
                    ]
                    if len(expected) != 1:
                        raise ValueError("recipe-cloned repositories changed")
                    if re.fullmatch(sources.REVISION, entry["ref"].lower()):
                        actual = entry["ref"].lower()
                        if not actual.startswith(abbreviated):
                            raise ValueError(
                                "producer checkout does not match its pinned commit"
                            )
                    else:
                        actual = self.resolver.resolve_evidence(
                            entry["url"], abbreviated, expected[0]
                        )
                    entry["commit"] = actual
                if sources.fingerprint(source) != record["source_digest"]:
                    raise ValueError("recipe-cloned source revisions changed")
            self.reasons[identity] = "legacy source inputs verified"
            return True
        except subprocess.TimeoutExpired:
            # An infrastructure timeout must not schedule a mass rebuild.
            raise
        except subprocess.CalledProcessError as error:
            stderr = error.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            if (
                error.cmd[0] in ("gh", "curl")
                and re.search(r"HTTP (?:404|410)|returned error: (?:404|410)", stderr)
                is None
            ):
                raise
            self.reasons[identity] = "producer evidence unavailable: " + str(error)
            return False
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            zipfile.BadZipFile,
        ) as error:
            self.reasons[identity] = str(error)
            return False

    def matches(self, records: list[dict], cached_keys: list[str]) -> dict:
        """Prefer source-key hits; otherwise select the newest proven legacy hit."""
        legacy = [key for key in cached_keys if LEGACY_KEY.fullmatch(key)]
        result = {}
        for record in records:
            if any(key.startswith(sources.cache_prefix(record)) for key in cached_keys):
                continue
            identity = manifest.package_identity(record)
            candidates = [
                key
                for key in legacy
                if key.startswith(f"cache-v2-{record['package']}-{record['arch']}-")
            ]
            for key in candidates:
                if self.verify(record, key):
                    result[identity] = key
                    break
        return result
