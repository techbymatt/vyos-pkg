"""Patched-recipe source inputs: readers, descriptors and scoped audits.

Everything here derives from the pinned patch repository, without contacting
Git remotes. Network resolution lives in source_resolver.py.
"""

from __future__ import annotations

import copy
import hashlib
import re
import tempfile
import tomllib
from pathlib import Path

try:
    from . import package_catalog as catalog
    from .source_identity import revision, source_text
    from .workflow_utils import canonical_json as canonical_bytes
    from .workflow_utils import git
except ImportError:
    import package_catalog as catalog
    from source_identity import revision, source_text
    from workflow_utils import canonical_json as canonical_bytes
    from workflow_utils import git

KERNEL_VPP_BUILD = """if [ ! -d ${VPP_LIB_CHECK_PATH} ]; then
    cd ../vpp/
    ./build.py
    cd ${CWD}
fi"""
KERNEL_VPP_APT = """apt-get download vpp=${VPP_VERSION} vpp-dev=${VPP_VERSION} \\
    libvppinfra=${VPP_VERSION} libvppinfra-dev=${VPP_VERSION}
for deb in *.deb; do
    dpkg-deb -x ${deb} ${VPP_SYSROOT}
done"""


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
        self.recipes: dict[tuple[str, str], dict] = {}
        self.revisions: dict[tuple[str, str], str] = {}

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
        identity = package, patch_commit
        if identity not in self.recipes:
            self.recipes[identity] = self.read_recipe(package, patch_commit)
        # Resolution/legacy proof annotates repository dictionaries with commits.
        # Never let those annotations mutate the cached discovery snapshot.
        return copy.deepcopy(self.recipes[identity])

    def read_recipe(self, package: str, patch_commit: str) -> dict:
        """Read uncached recipe inputs, including explicitly audited nested sources."""
        _, tree = self.patched_tree(patch_commit)
        directory = f"scripts/package-build/{package}"
        recipe_tree = revision(git(self.root, "rev-parse", f"{tree}:{directory}"))
        config = tomllib.loads(self.text(tree, f"{directory}/package.toml"))
        repositories = recipe_repositories(config)
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
            if (
                kernel_uses_vpp(config)
                and audit_kernel_vpp_build(
                    self.text(tree, f"{directory}/build-accel-ppp-ng.sh")
                )
                == "nested"
            ):
                nested = self.recipe("vpp", patch_commit)
                inputs["vpp_recipe_tree"] = nested["recipe_tree"]
                repositories.extend(nested["repositories"])
        return {
            "recipe_tree": recipe_tree,
            "inputs": inputs,
            "repositories": repositories,
        }

    def legacy_revision(self, package: str, patch_commit: str) -> str:
        """Retain the original path revision for diagnostics and legacy lookups."""
        catalog.validate_name(package)
        identity = package, patch_commit
        if identity in self.revisions:
            return self.revisions[identity]
        upstream, _ = self.patched_tree(patch_commit)
        paths = [f"scripts/package-build/{package}/"]
        if package == "linux-kernel":
            paths.insert(0, "data/defaults.toml")
        self.revisions[identity] = revision(
            git(self.root, "log", "-n", "1", "--format=%H", upstream, "--", *paths)
        )
        return self.revisions[identity]


def recipe_repositories(config: dict) -> list[dict]:
    """Read declared Git sources while retaining their original version labels."""
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
    return repositories


def kernel_uses_vpp(config: dict) -> bool:
    """Identify the kernel sub-build which consumes the sibling VPP recipe."""
    return any(
        entry.get("build_cmd") == "build_accel_ppp_ng" for entry in config["packages"]
    )


def audit_kernel_vpp_build(script: str) -> str:
    """Require one audited VPP dependency path rather than guessing drift.

    Older recipes built the sibling VPP recipe from source; newer recipes
    unpack VPP development packages from APT into a temporary sysroot.
    """
    nested = script.count(KERNEL_VPP_BUILD)
    apt = script.count(KERNEL_VPP_APT)
    if nested == 1 and apt == 0:
        return "nested"
    if (
        apt == 1
        and nested == 0
        and "../vpp" not in script
        and "git clone" not in script
    ):
        return "apt"
    raise ValueError(
        "linux-kernel: expected one audited nested VPP build or APT sysroot"
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


def discover_sources(reader: RecipeReader, sources: dict, patch_commit: str) -> dict:
    """Describe catalogued sources without contacting their Git remotes."""
    descriptions = {}
    for definition in catalog.iter_sources(sources):
        group, name = definition.group, definition.name
        if group == "build":
            source = reader.recipe(name, patch_commit)
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
        descriptions[group, name] = source
    return descriptions
