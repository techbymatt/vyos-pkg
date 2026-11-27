"""Audited checkout and pinning of planned source revisions.

Version labels in recipes stay intact; only the checkout moves to the planned
full commits. Every adaptation is validated before any file is written.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tomllib
from pathlib import Path

try:
    from . import package_catalog as catalog
    from .checked_edits import CheckedEdits
    from .git_network import remote_git
    from .prepare_package_build import stage_git_clones
    from .recipe_inputs import (
        audit_kernel_vpp_build,
        kea_repository,
        kernel_uses_vpp,
        recipe_repositories,
    )
    from .source_identity import validate_repositories
    from .workflow_utils import git
except ImportError:
    import package_catalog as catalog
    from checked_edits import CheckedEdits
    from git_network import remote_git
    from prepare_package_build import stage_git_clones
    from recipe_inputs import (
        audit_kernel_vpp_build,
        kea_repository,
        kernel_uses_vpp,
        recipe_repositories,
    )
    from source_identity import validate_repositories
    from workflow_utils import git

PIN_ENV = "PACKAGE_SOURCE_REVISIONS"
KEA_CLONE = "git clone --branch ${BRANCH} "


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
        remote_git("fetch", "origin", commit, cwd=directory, timeout=300)
    git(directory, "checkout", "--detach", commit)
    print(f"Source checkout: {url} {ref} -> {commit}", flush=True)


def pin_recipe(root: Path, package: str, repositories: list[dict]) -> None:
    """Adapt audited Git clones and checkouts without rewriting version labels."""
    edits = CheckedEdits()
    stage_recipe_pins(root, package, repositories, edits)
    stage_git_clones(root, package, edits)
    edits.commit()


def retry_recipe_clones(root: Path, package: str) -> None:
    """Make Test clones resilient without changing the recipe's original refs."""
    catalog.validate_name(package)
    edits = CheckedEdits()
    stage_git_clones(root, package, edits)
    if package == "isc-kea":
        script = root / package / "prebuild.sh"
        repository = kea_repository(edits.read(script))
        stage_kea_clone(script, repository["url"], edits, pinned=False)
    edits.commit()


def stage_recipe_pins(
    root: Path, package: str, repositories: list[dict], edits: CheckedEdits
) -> None:
    """Validate every parent/nested adaptation before changing any builder."""
    repositories = validate_repositories(repositories)
    directory = root / catalog.validate_name(package)
    config = tomllib.loads((directory / "package.toml").read_text())
    expected = recipe_repositories(config)
    nested = []
    if (
        package == "linux-kernel"
        and kernel_uses_vpp(config)
        and audit_kernel_vpp_build((directory / "build-accel-ppp-ng.sh").read_text())
        == "nested"
    ):
        nested = recipe_repositories(
            tomllib.loads((root / "vpp/package.toml").read_text())
        )
        expected.extend(nested)
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
        edits.replace(
            builder, command, "checkout_source(repo_dir, scm_url, commit_id)", count=2
        )
        if any(
            entry.get("build_cmd") == "build_intel_nic" for entry in config["packages"]
        ):
            # Older recipes discard the declared tag before compilation; newer
            # recipes already reset to HEAD. Require one audited reset in either
            # form so extra commands cannot move away from the planned checkout.
            driver = directory / "build-intel-nic.sh"
            resets = re.findall(
                r"^[ \t]*git[ \t]+reset\b.*$", edits.read(driver), re.MULTILINE
            )
            if resets not in (
                ["    git reset --hard origin/main"],
                ["    git reset --hard HEAD"],
            ):
                raise ValueError(
                    f"{driver}: expected exactly one audited Intel driver reset"
                )
            edits.replace(
                driver,
                f"\n{resets[0]}\n",
                "\n    git reset --hard HEAD\n",
            )
    else:
        if not builder.is_symlink() or os.readlink(builder) != "../build.py":
            raise ValueError(f"{package}: unaudited custom source builder")
        builder = root / "build.py"
        edits.replace(
            builder,
            "run(['git', 'checkout', package['commit_id']], cwd=repo_dir, check=True)",
            "checkout_source(repo_dir, package['scm_url'], package['commit_id'])",
        )
    edits.replace(
        builder,
        "from subprocess import run, CalledProcessError",
        "from subprocess import run, CalledProcessError\nfrom package_sources import checkout_source",
    )
    if nested:
        names = {entry["name"] for entry in nested}
        stage_recipe_pins(
            root,
            "vpp",
            [entry for entry in repositories if entry["name"] in names],
            edits,
        )
    if package == "isc-kea":
        repository = next(
            entry for entry in repositories if entry["name"] == "kea-packaging"
        )
        stage_kea_clone(
            directory / "prebuild.sh", repository["url"], edits, pinned=True
        )


def stage_kea_clone(path: Path, url: str, edits: CheckedEdits, *, pinned: bool) -> None:
    """Use the retrying clone helper and stop the hook if cloning fails."""
    command = (
        "python3 -m package_sources checkout --directory kea-packaging --url "
        + shlex.quote(url)
        + ' --ref "$BRANCH"'
    )
    if not pinned:
        command += " --unplanned"
    edits.replace(path, KEA_CLONE + url, command + " || exit $?")
