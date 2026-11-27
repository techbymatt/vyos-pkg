"""Source-scoped fingerprints, Git resolution, and exact recipe pinning tests."""

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from . import git_network, network_retry
    from . import package_sources as sources
    from . import prepare_package_build as build_policy
    from . import source_resolver as resolver_module
except ImportError:
    import git_network
    import network_retry
    import package_sources as sources
    import prepare_package_build as build_policy
    import source_resolver as resolver_module

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
URL = "https://github.com/example/source.git"
BUILDER = """from subprocess import run, CalledProcessError

def checkout(package, repo_dir):
    if not repo_dir.exists():
        run(['git', 'clone', package['scm_url'], str(repo_dir)], check=True)
    run(['git', 'checkout', package['commit_id']], cwd=repo_dir, check=True)
    return package['commit_id'].replace('/', '_')
"""
KERNEL_BUILDER = """from subprocess import run, CalledProcessError

def checkout(repo_dir, scm_url, commit_id, exists):
    if exists:
        run(['git', 'checkout', commit_id], cwd=repo_dir, check=True)
    else:
        run(['git', 'clone', scm_url, str(repo_dir)], check=True)
        run(['git', 'checkout', commit_id], cwd=repo_dir, check=True)
"""
APT_ACCEL_BUILDER = """#!/bin/sh
VPP_VERSION=$(apt-cache policy vpp-dev | awk '/Candidate:/ {print $2}')
apt-get download vpp=${VPP_VERSION} vpp-dev=${VPP_VERSION} \\
    libvppinfra=${VPP_VERSION} libvppinfra-dev=${VPP_VERSION}
for deb in *.deb; do
    dpkg-deb -x ${deb} ${VPP_SYSROOT}
done
"""
INTEL_BUILDER = """#!/bin/sh
set -e
cd "ethernet-linux-$1"
if [ -d .git ]; then
    git reset --hard HEAD
    git clean --force -d -x
fi
git rev-parse HEAD
"""


def repository(name="source", ref="rolling", commit=COMMIT, url=URL):
    """Build a resolved external-source descriptor."""
    return {"name": name, "url": url, "ref": ref, "commit": commit}


def descriptor(tree=COMMIT):
    """Build a minimal recipe-source descriptor."""
    return {"recipe_tree": tree, "inputs": {}, "repositories": [repository()]}


def kernel_dependencies(root, ref=COMMIT, url=URL):
    """Add the kernel's audited driver and accel-PPP scripts to a scratch tree."""
    directory = root / "linux-kernel"
    (directory / "build-accel-ppp-ng.sh").write_text(APT_ACCEL_BUILDER)
    (directory / "build-intel-nic.sh").write_text(INTEL_BUILDER)
    path = directory / "package.toml"
    text = path.read_text() if path.exists() else ""
    repositories = []
    for name, command in (
        ("accel-ppp-ng", "build_accel_ppp_ng"),
        ("igb", "build_intel_nic"),
    ):
        text += (
            f'\n[[packages]]\nname = "{name}"\nscm_url = "{url}"\n'
            f'commit_id = "{ref}"\nbuild_cmd = "{command}"\n'
        )
        repositories.append(repository(name, ref, url=url))
    path.write_text(text)
    return repositories


def git(root, *arguments, strip=True):
    """Run fixture Git commands with no dependency on the developer's Git config."""
    result = subprocess.check_output(
        ["git", *arguments],
        cwd=root,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        stderr=subprocess.PIPE,
    )
    return result.strip() if strip else result


def commit(root, message="fixture"):
    """Commit only a disposable fixture repository's changes."""
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.org",
        "commit",
        "-qm",
        message,
    )
    return git(root, "rev-parse", "HEAD")


class RecipeFixture:
    """Small real Git repositories with patched and out-of-folder source inputs."""

    def __init__(self, root):
        """Create a patch repository pinning an embedded upstream checkout."""
        self.patch_root = root / "patch"
        self.upstream = self.patch_root / "vyos-build"
        self.upstream.mkdir(parents=True)
        git(self.upstream, "init", "-qb", "rolling")
        self.build_root = self.upstream / "scripts/package-build"
        self.build_root.mkdir(parents=True)
        (self.build_root / "build.py").write_text(BUILDER)
        for name in ("frr", "other", "linux-kernel"):
            directory = self.build_root / name
            directory.mkdir()
            if name == "linux-kernel":
                (directory / "build.py").write_text(KERNEL_BUILDER)
                config = '[[packages]]\nname = "linux-kernel"\nscm_url = ""\ncommit_id = ""\n'
            else:
                (directory / "build.py").symlink_to("../build.py")
                config = f'[[packages]]\nname = "{name}"\nscm_url = "{URL}"\ncommit_id = "{COMMIT}"\n'
            (directory / "package.toml").write_text(config)
        data = self.upstream / "data"
        (data / "certificates").mkdir(parents=True)
        (data / "defaults.toml").write_text(
            'kernel_version = "6.18.50"\nkernel_flavor = "vyos"\nwebsite_url = "https://example.org"\n'
        )
        (data / "certificates/key.pem").write_text("public certificate\n")
        (data / "certificates/README.md").write_text("documentation\n")
        self.upstream_commit = commit(self.upstream)
        git(self.patch_root, "init", "-qb", "main")
        (self.patch_root / "patches/vyos-build").mkdir(parents=True)
        (self.patch_root / "README.md").write_text("patch repository\n")
        self.pin()

    def pin(self):
        """Record upstream and patch updates and return the parent commit."""
        git(self.patch_root, "add", "README.md", "patches")
        git(
            self.patch_root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{self.upstream_commit},vyos-build",
        )
        git(
            self.patch_root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.org",
            "commit",
            "-qm",
            "pin source inputs",
        )
        self.patch_commit = git(self.patch_root, "rev-parse", "HEAD")
        return self.patch_commit

    def update_upstream(self, path, text):
        """Commit an upstream change and advance the parent pin."""
        (self.upstream / path).write_text(text)
        self.upstream_commit = commit(self.upstream)
        return self.pin()

    def enable_kernel_dependencies(self):
        """Commit the sibling recipes so historical tree tests can consume them."""
        kernel_dependencies(self.build_root)
        self.upstream_commit = commit(self.upstream)
        return self.pin()

    def add_patch(self, path, text, name="0001-recipe.patch"):
        """Generate a real patch while leaving the upstream worktree clean."""
        target = self.upstream / path
        original = target.read_text()
        target.write_text(text)
        patch_text = git(self.upstream, "diff", "--", path, strip=False)
        target.write_text(original)
        git(self.upstream, "update-index", "--refresh")
        (self.patch_root / "patches/vyos-build" / name).write_text(patch_text)
        return self.pin()


class FingerprintTests(unittest.TestCase):
    """Canonical identities and strict source descriptor validation."""

    def test_recipe_config_and_each_external_commit_change_the_fingerprint(self):
        """Every actual source input matters independently."""
        original = descriptor()
        original["repositories"].append(repository("dependency", commit=OTHER_COMMIT))
        before = sources.fingerprint(original)
        variants = []
        changed = copy.deepcopy(original)
        changed["recipe_tree"] = OTHER_COMMIT
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed["inputs"]["kernel_version"] = "6.18.51"
        variants.append(changed)
        for index in range(2):
            changed = copy.deepcopy(original)
            changed["repositories"][index]["commit"] = "c" * 40
            variants.append(changed)
        for changed in variants:
            with self.subTest(source=changed):
                self.assertNotEqual(sources.fingerprint(changed), before)
        original["repositories"].reverse()
        self.assertEqual(sources.fingerprint(original), before)

    def test_standalone_sources_have_no_recipe_or_global_inputs(self):
        """A standalone source is completely described by its resolved repository."""
        source = descriptor(None)
        self.assertRegex(sources.fingerprint(source), r"^[0-9a-f]{64}$")
        for changed in (
            dict(source, inputs={"image": "new-image"}),
            dict(source, repositories=[]),
            dict(source, repositories=[repository(), repository("other")]),
        ):
            with self.subTest(source=changed), self.assertRaises(ValueError):
                sources.validate_source(changed)

    def test_malformed_or_duplicate_sources_are_rejected(self):
        """Corrupt descriptors fail instead of being mistaken for stable inputs."""
        for value in (
            {},
            dict(descriptor(), recipe_tree="not-a-tree"),
            dict(descriptor(), repositories=[repository(), repository()]),
            dict(descriptor(), repositories=[repository(commit="")]),
            dict(descriptor(), inputs={"bad key": "value"}),
            dict(descriptor(), inputs={"version": "two\nlines"}),
            dict(descriptor(), repositories=[repository(url="--upload-pack=bad")]),
        ):
            with self.subTest(source=value), self.assertRaises(ValueError):
                sources.validate_source(value)


class RecipeScopeTests(unittest.TestCase):
    """Real Git tree tests for package-scoped patches and kernel inputs."""

    def setUp(self):
        """Create and clean up a disposable pinned recipe checkout."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = RecipeFixture(Path(temporary.name))
        self.reader = sources.RecipeReader(self.fixture.patch_root)

    def read(self, package):
        """Resolve fixture Git sources without a network lookup."""
        source = self.reader.recipe(package, self.fixture.patch_commit)
        for entry in source["repositories"]:
            entry["commit"] = entry["ref"]
        return sources.fingerprint(source)

    def test_recipe_patch_only_changes_the_affected_package(self):
        """Scoped patch contents affect one recipe, never the whole catalog."""
        before = {name: self.read(name) for name in ("frr", "other", "linux-kernel")}
        path = "scripts/package-build/frr/package.toml"
        text = (self.fixture.upstream / path).read_text()
        self.fixture.add_patch(path, text + "# package-specific change\n")
        self.assertNotEqual(self.read("frr"), before["frr"])
        self.assertEqual(self.read("other"), before["other"])
        self.assertEqual(self.read("linux-kernel"), before["linux-kernel"])
        self.assertEqual(git(self.fixture.upstream, "status", "--porcelain"), "")

    def test_recipe_snapshots_are_memoized_without_leaking_commit_annotations(self):
        """Resolution may annotate copies, never the cached recipe."""
        source = self.reader.recipe("frr", self.fixture.patch_commit)
        source["repositories"][0]["commit"] = OTHER_COMMIT
        source["inputs"]["unexpected"] = "value"
        self.reader.recipe_revision("frr", self.fixture.patch_commit)
        with patch.object(
            sources, "git", side_effect=AssertionError("unexpected reread")
        ):
            cached = self.reader.recipe("frr", self.fixture.patch_commit)
            self.reader.recipe_revision("frr", self.fixture.patch_commit)
        self.assertNotIn("commit", cached["repositories"][0])
        self.assertEqual(cached["inputs"], {})

    def test_cached_patch_tree_matches_real_build_patch_application(self):
        """Isolated index application has exactly the build worktree's semantics."""
        path = "scripts/package-build/frr/package.toml"
        self.fixture.add_patch(
            path, (self.fixture.upstream / path).read_text() + "# patch\n"
        )
        _, expected = self.reader.patched_tree(self.fixture.patch_commit)
        git(
            self.fixture.upstream,
            "apply",
            "--3way",
            str(self.fixture.patch_root / "patches/vyos-build/0001-recipe.patch"),
        )
        self.assertEqual(git(self.fixture.upstream, "write-tree"), expected)

    def test_patch_application_preserves_trailing_whitespace_in_source_files(self):
        """Source fingerprints describe the actual patch bytes applied by builds."""
        path = "scripts/package-build/frr/package.toml"
        self.fixture.add_patch(
            path, (self.fixture.upstream / path).read_text() + "# retained spaces   \n"
        )
        _, expected = self.reader.patched_tree(self.fixture.patch_commit)
        git(
            self.fixture.upstream,
            "apply",
            "--3way",
            str(self.fixture.patch_root / "patches/vyos-build/0001-recipe.patch"),
        )
        self.assertEqual(git(self.fixture.upstream, "write-tree"), expected)
        self.assertTrue(
            (self.fixture.upstream / path).read_text().endswith("spaces   \n")
        )

    def test_shared_builder_and_patch_metadata_do_not_change_source_identities(self):
        """Shared tooling and patch messages are excluded from source identities."""
        before = self.read("frr")
        self.fixture.add_patch(
            "scripts/package-build/build.py", BUILDER + "# shared change\n"
        )
        self.assertEqual(self.read("frr"), before)
        path = self.fixture.patch_root / "patches/vyos-build/0001-recipe.patch"
        path.write_text("Subject: descriptive message changed\n\n" + path.read_text())
        self.fixture.pin()
        self.assertEqual(self.read("frr"), before)

    def test_only_consumed_kernel_defaults_and_certificates_affect_the_kernel(self):
        """Unrelated data/doc changes reuse caches; kernel inputs only affect kernel."""
        before_kernel, before_other = self.read("linux-kernel"), self.read("other")
        defaults = "data/defaults.toml"
        text = (self.fixture.upstream / defaults).read_text()
        self.fixture.update_upstream(
            defaults, text.replace("https://example.org", "https://other.org")
        )
        self.fixture.update_upstream(
            "data/certificates/README.md", "new documentation\n"
        )
        self.assertEqual(self.read("linux-kernel"), before_kernel)
        self.fixture.update_upstream(defaults, text.replace("6.18.50", "6.18.51"))
        new_kernel = self.read("linux-kernel")
        self.assertNotEqual(new_kernel, before_kernel)
        self.fixture.update_upstream(
            "data/certificates/key.pem", "updated certificate\n"
        )
        self.assertNotEqual(self.read("linux-kernel"), new_kernel)
        self.assertEqual(self.read("other"), before_other)

    def test_old_patch_pins_are_read_without_changing_the_checkout(self):
        """Isolated index reading reconstructs arbitrary patch commits safely."""
        old_pin = self.fixture.patch_commit
        old_source = self.reader.recipe("frr", old_pin)
        path = "scripts/package-build/frr/package.toml"
        self.fixture.update_upstream(
            path, (self.fixture.upstream / path).read_text() + "# update\n"
        )
        self.assertNotEqual(
            self.reader.recipe("frr", self.fixture.patch_commit), old_source
        )
        self.assertEqual(self.reader.recipe("frr", old_pin), old_source)
        self.assertEqual(
            git(self.fixture.upstream, "rev-parse", "HEAD"),
            self.fixture.upstream_commit,
        )

    def test_kernel_vpp_command_drift_fails_discovery(self):
        """Reject drifted, sibling-built, or git-cloning VPP commands."""
        self.fixture.enable_kernel_dependencies()
        for label, script in (
            (
                "legacy sibling build",
                (
                    "#!/bin/sh\nif [ ! -d ${VPP_LIB_CHECK_PATH} ]; then\n"
                    "    cd ../vpp/\n    ./build.py\n    cd ${CWD}\nfi\n"
                ),
            ),
            (
                "changed apt packages",
                APT_ACCEL_BUILDER.replace(
                    "libvppinfra-dev=${VPP_VERSION}",
                    "vpp-plugin-core=${VPP_VERSION}",
                ),
            ),
            ("apt plus sibling ref", APT_ACCEL_BUILDER + "echo ../vpp\n"),
            (
                "apt plus git clone",
                APT_ACCEL_BUILDER + "git clone https://example.org/vpp.git\n",
            ),
            ("duplicate apt block", APT_ACCEL_BUILDER + APT_ACCEL_BUILDER),
        ):
            with self.subTest(case=label):
                self.fixture.update_upstream(
                    "scripts/package-build/linux-kernel/build-accel-ppp-ng.sh",
                    script,
                )
                with self.assertRaisesRegex(
                    ValueError, "audited APT VPP sysroot"
                ):
                    self.reader.recipe("linux-kernel", self.fixture.patch_commit)

    def test_kernel_apt_vpp_sysroot_tracks_only_kernel_repositories(self):
        """APT-sourced VPP headers/libs require no sibling VPP recipe or repos."""
        self.fixture.enable_kernel_dependencies()
        apt_source = self.reader.recipe("linux-kernel", self.fixture.patch_commit)
        self.assertEqual(
            {entry["name"] for entry in apt_source["repositories"]},
            {"accel-ppp-ng", "igb"},
        )
        self.assertNotIn("vpp_recipe_tree", apt_source["inputs"])

    def test_missing_or_failed_recipes_fail_planning(self):
        """A missing source folder or unappliable patch cannot become a cache hit."""
        with self.assertRaises(subprocess.CalledProcessError):
            self.reader.recipe("missing", self.fixture.patch_commit)
        (self.fixture.patch_root / "patches/vyos-build/broken.patch").write_text(
            "not a patch\n"
        )
        self.fixture.pin()
        with self.assertRaises(subprocess.CalledProcessError):
            self.reader.recipe("frr", self.fixture.patch_commit)

    def test_kea_packaging_repo_is_a_separate_scoped_source(self):
        """Nested packaging refs must be tracked, with command drift rejected."""
        script = '#!/bin/sh\nBRANCH="Kea-3.0.3"\n' + sources.KEA_CLONE + URL + "\n"
        self.assertEqual(
            sources.kea_repository(script),
            {"name": "kea-packaging", "url": URL, "ref": "Kea-3.0.3"},
        )
        for changed in (
            script.replace("${BRANCH}", "$BRANCH"),
            script + script,
            script.replace('"Kea-3.0.3"', '"$DYNAMIC_BRANCH"'),
        ):
            with self.subTest(script=changed), self.assertRaises(ValueError):
                sources.kea_repository(changed)


class GitResolutionTests(unittest.TestCase):
    """Real Git refs, annotated tags, and abbreviated commit resolution."""

    def setUp(self):
        """Make a local remote with branch, lightweight tag, and annotated tag."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        git(self.remote, "init", "-qb", "rolling")
        (self.remote / "file").write_text("first source\n")
        self.first = commit(self.remote)
        git(self.remote, "tag", "v1")
        (self.remote / "file").write_text("second source\n")
        self.second = commit(self.remote)
        git(
            self.remote,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.org",
            "tag",
            "-am",
            "annotated",
            "v2",
        )
        self.url = self.remote.as_uri()
        self.resolver = sources.Resolver()
        self.addCleanup(self.resolver.close)

    def test_pinned_refs_ignore_unrelated_branch_commits(self):
        """Branches resolve to their tip; fixed refs resolve to their actual source."""
        self.assertEqual(self.resolver.resolve(self.url, "rolling"), self.second)
        self.assertEqual(self.resolver.resolve(self.url, "v1"), self.first)
        self.assertEqual(self.resolver.resolve(self.url, "v2"), self.second)
        self.assertEqual(self.resolver.resolve(self.url, self.first[:8]), self.first)
        self.assertEqual(self.resolver.resolve(self.url, self.first), self.first)
        (self.remote / "file").write_text("third source\n")
        third = commit(self.remote)
        updated = sources.Resolver()
        self.addCleanup(updated.close)
        self.assertEqual(updated.resolve(self.url, "rolling"), third)
        self.assertEqual(updated.resolve(self.url, "v1"), self.first)
        self.assertEqual(updated.resolve(self.url, "v2"), self.second)

    def test_missing_and_ambiguous_refs_fail(self):
        """Ref errors fail explicitly rather than invalidating other package caches."""
        git(self.remote, "branch", "v1", self.first)
        for ref in ("missing", "v1"):
            with (
                self.subTest(ref=ref),
                self.assertRaisesRegex(ValueError, "missing or ambiguous"),
            ):
                self.resolver.resolve(self.url, ref)

    def test_retried_ref_lookup_caches_only_the_successful_snapshot(self):
        """A transient connection failure does not poison lookups for the same URL."""
        failure = subprocess.CalledProcessError(
            128,
            ["git", "ls-remote"],
            stderr="fatal: Could not resolve host: example.org",
        )
        refs = f"{COMMIT}\trefs/heads/rolling\n{OTHER_COMMIT}\trefs/tags/v1"
        with (
            patch.object(git_network, "_git", side_effect=[failure, refs]) as remote,
            patch.object(network_retry.time, "sleep") as delay,
            patch.object(network_retry.sys, "stderr"),
        ):
            self.assertEqual(self.resolver.resolve(URL, "rolling"), COMMIT)
            self.assertEqual(self.resolver.resolve(URL, "v1"), OTHER_COMMIT)
        self.assertEqual(remote.call_count, 2)
        delay.assert_called_once_with(5)

    def test_github_short_commit_resolution_and_memoization(self):
        """GitHub abbreviated commits use its commit API once per ref."""
        with (
            patch.object(resolver_module, "remote_git", return_value="") as remote,
            patch.object(resolver_module, "command_output", return_value=COMMIT) as output,
        ):
            self.assertEqual(self.resolver.resolve(URL, "a" * 8), COMMIT)
            self.assertEqual(self.resolver.resolve(URL, "a" * 8), COMMIT)
            self.assertEqual(self.resolver.resolve(URL, COMMIT), COMMIT)
        remote.assert_called_once_with("ls-remote", "--", URL)
        output.assert_called_once_with(
            [
                "gh",
                "api",
                "repos/example/source/commits/aaaaaaaa",
                "--jq",
                ".sha",
            ]
        )

    def test_independent_remotes_resolve_in_parallel_with_one_snapshot_per_url(self):
        """Three independent URL lookups must meet without duplicating ref requests."""
        urls = [f"https://example.org/source-{index}.git" for index in range(3)]
        barrier = threading.Barrier(3)

        def lookup(*arguments):
            self.assertEqual(arguments[:2], ("ls-remote", "--"))
            barrier.wait(timeout=5)
            return f"{COMMIT}\trefs/heads/rolling\n{OTHER_COMMIT}\trefs/tags/v1"

        requested = [
            {"url": url, "ref": ref}
            for url in urls
            for ref in ("rolling", "v1", "rolling")
        ]
        with patch.object(resolver_module, "remote_git", side_effect=lookup) as output:
            resolved = self.resolver.resolve_many(requested, jobs=3)
        self.assertEqual(output.call_count, 3)
        self.assertEqual(
            resolved,
            {
                (url, ref): COMMIT if ref == "rolling" else OTHER_COMMIT
                for url in urls
                for ref in ("rolling", "v1")
            },
        )

    def test_parallel_lookups_respect_the_concurrency_limit(self):
        """The planner cannot flood metadata services as package membership grows."""
        lock, ready = threading.Lock(), threading.Event()
        active = 0
        maximum = 0

        def lookup(*arguments):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    ready.set()
            self.assertTrue(ready.wait(timeout=5))
            with lock:
                active -= 1
            return f"{COMMIT}\trefs/heads/rolling"

        with patch.object(resolver_module, "remote_git", side_effect=lookup):
            resolved = self.resolver.resolve_many(
                [
                    {"url": f"https://example.org/{index}.git", "ref": "rolling"}
                    for index in range(8)
                ],
                jobs=2,
            )
        self.assertEqual(len(resolved), 8)
        self.assertEqual(maximum, 2)

    def test_parallel_metadata_clones_have_distinct_directories(self):
        """Two abbreviated-commit lookups cannot allocate the same temporary clone."""
        barrier = threading.Barrier(2)
        directories = []

        def clone(directory, url, *options, timeout):
            self.assertEqual(options, ("--bare", "--filter=blob:none"))
            self.assertEqual(timeout, 300)
            directories.append(directory)
            barrier.wait(timeout=5)

        with (
            patch.object(resolver_module, "remote_git", return_value=""),
            patch.object(resolver_module, "clone_repository", side_effect=clone),
            patch.object(resolver_module, "git", return_value=COMMIT),
        ):
            self.resolver.resolve_many(
                [
                    {"url": f"https://example.org/{index}.git", "ref": COMMIT[:8]}
                    for index in range(2)
                ],
                jobs=2,
            )
        self.assertEqual(len(set(directories)), 2)
        self.assertEqual(set(directories), set(self.resolver.repositories.values()))

    def test_parallel_ref_failure_is_not_treated_as_a_missing_cache(self):
        """A metadata lookup failure propagates instead of inventing source revisions."""
        with (
            patch.object(
                resolver_module, "remote_git", side_effect=ValueError("lookup failed")
            ),
            self.assertRaisesRegex(ValueError, "lookup failed"),
        ):
            self.resolver.resolve_many([{"url": URL, "ref": "rolling"}], jobs=2)
        with self.assertRaisesRegex(ValueError, "jobs must be positive"):
            self.resolver.resolve_many([], jobs=0)

    def test_catalog_discovery_resolves_once_per_source_not_per_architecture(self):
        """Recipe and standalone descriptors share one bounded bulk resolution phase."""
        catalog = sources.catalog.validate_catalog(
            {"build": [{"name": "frr"}], "build-extra": [{"name": "extra"}]}
        )
        recipe = descriptor()
        del recipe["repositories"][0]["commit"]
        reader = unittest.mock.Mock()
        reader.recipe.return_value = copy.deepcopy(recipe)
        reader.recipe_revision.return_value = OTHER_COMMIT
        resolved = {
            (URL, "rolling"): COMMIT,
            ("https://github.com/vyos/extra.git", "rolling"): OTHER_COMMIT,
        }
        with patch.object(
            self.resolver, "resolve_many", return_value=resolved
        ) as lookup:
            result = sources.resolve_sources(
                reader, self.resolver, catalog, COMMIT, jobs=4
            )
        reader.recipe.assert_called_once_with("frr", COMMIT)
        self.assertEqual(len(lookup.call_args.args[0]), 2)
        self.assertEqual(lookup.call_args.kwargs, {"jobs": 4})
        self.assertEqual(result["build", "frr"]["commit"], OTHER_COMMIT)
        self.assertEqual(result["build-extra", "extra"]["commit"], OTHER_COMMIT)
        self.assertEqual(
            result["build", "frr"]["source"]["repositories"][0]["commit"], COMMIT
        )

    def test_gitlab_short_commits_use_an_encoded_project_commit_api(self):
        """Public GitLab commit lookup avoids cloning packaging histories."""
        url = "https://salsa.debian.org/debian/pkg.git"
        with (
            patch.object(resolver_module, "remote_git", return_value="") as remote,
            patch.object(
                resolver_module, "command_output", return_value=json.dumps({"id": COMMIT})
            ) as output,
        ):
            self.assertEqual(self.resolver.resolve(url, "a" * 8), COMMIT)
        remote.assert_called_once_with("ls-remote", "--", url)
        output.assert_called_once_with(
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
                "https://salsa.debian.org/api/v4/projects/debian%2Fpkg/repository/commits/aaaaaaaa?stats=false",
            ]
        )

    def test_numeric_tags_and_hexadecimal_branch_names_precede_short_commits(self):
        """Firmware date tags and hex-like branch names do not trigger bare clones."""
        git(self.remote, "tag", "20260410", self.first)
        git(self.remote, "branch", "deadbeef", self.second)
        self.assertEqual(self.resolver.resolve(self.url, "20260410"), self.first)
        self.assertEqual(self.resolver.resolve(self.url, "deadbeef"), self.second)
        self.assertEqual(self.resolver.repositories, {})

    def test_planned_checkout_survives_a_moving_branch(self):
        """Compilation gets the planned SHA even after rolling advances."""
        checkout = self.root / "checkout"
        git(self.root, "clone", "--quiet", self.url, str(checkout))
        with patch.dict(
            os.environ,
            {
                sources.PIN_ENV: json.dumps(
                    [repository(url=self.url, commit=self.first)]
                )
            },
        ):
            sources.checkout_source(checkout, self.url, "rolling")
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), self.first)
        self.assertEqual((checkout / "file").read_text(), "first source\n")
        with (
            patch.dict(os.environ, {sources.PIN_ENV: "[]"}),
            self.assertRaises(ValueError),
        ):
            sources.checkout_source(checkout, self.url, "rolling")

    def test_checkout_fetches_a_planned_commit_missing_from_the_clone(self):
        """A checkout predating planning fetches the planned SHA rather than the tip."""
        checkout = self.root / "checkout"
        git(self.root, "clone", "--quiet", self.url, str(checkout))
        (self.remote / "file").write_text("newly planned source\n")
        planned = commit(self.remote)
        (self.remote / "file").write_text("later unplanned source\n")
        commit(self.remote)
        with patch.dict(
            os.environ,
            {sources.PIN_ENV: json.dumps([repository(url=self.url, commit=planned)])},
        ):
            sources.checkout_source(checkout, self.url, "rolling")
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), planned)

    def test_kernel_builder_and_driver_reset_preserve_planned_commits(self):
        """Execute checkout/cleanup fixtures, never hardware compilation, against real Git."""
        git(self.remote, "branch", "main", self.second)
        root = self.root / "recipes"
        directory = root / "linux-kernel"
        directory.mkdir(parents=True)
        (root / "build.py").write_text(BUILDER)
        (directory / "build.py").write_text(KERNEL_BUILDER)
        (directory / "package.toml").write_text(
            '[[packages]]\nname = "linux-kernel"\nscm_url = ""\ncommit_id = ""\n'
        )
        repositories = [
            dict(entry, commit=self.first)
            for entry in kernel_dependencies(root, "rolling", self.url)
        ]
        sources.pin_recipe(root, "linux-kernel", repositories)
        checkout = directory / "ethernet-linux-igb"
        with (
            patch.dict(os.environ, {sources.PIN_ENV: json.dumps(repositories)}),
            patch.dict(
                sys.modules,
                {
                    "package_sources": sources,
                    "git_network": git_network,
                },
            ),
        ):
            spec = importlib.util.spec_from_file_location(
                "kernel_fixture", directory / "build.py"
            )
            if spec is None or spec.loader is None:
                raise ValueError("fixture builder cannot be imported")
            kernel = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(kernel)
            kernel.checkout(checkout, self.url, "rolling", False)
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), self.first)
            kernel.checkout(checkout, self.url, "rolling", True)
        subprocess.run(
            ["sh", "build-intel-nic.sh", "igb"],
            cwd=directory,
            check=True,
            capture_output=True,
        )
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), self.first)
        self.assertNotEqual(git(checkout, "rev-parse", "origin/main"), self.first)


class PinRecipeTests(unittest.TestCase):
    """Checked source command adaptations preserve version labels and build modes."""

    def setUp(self):
        """Create a scratch builder and recipe tree."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "build.py").write_text(BUILDER)

    def recipe(self, name, extra=""):
        """Write a recipe using the standard builder and a moving branch."""
        directory = self.root / name
        directory.mkdir()
        (directory / "build.py").symlink_to("../build.py")
        (directory / "package.toml").write_text(
            f'[[packages]]\nname = "source"\nscm_url = "{URL}"\ncommit_id = "rolling"\n'
            + extra
        )
        return directory

    def test_shared_builder_pins_only_checkout_not_version_metadata(self):
        """The original ref remains available for source/package-version fallbacks."""
        directory = self.recipe("frr")
        before = (directory / "package.toml").read_text()
        sources.pin_recipe(self.root, "frr", [repository()])
        text = (self.root / "build.py").read_text()
        self.assertIn(
            "checkout_source(repo_dir, package['scm_url'], package['commit_id'])", text
        )
        self.assertIn("return package['commit_id'].replace('/', '_')", text)
        self.assertEqual((directory / "package.toml").read_text(), before)
        compile(text, "build.py", "exec")

    def test_kernel_custom_builder_pins_both_clone_and_existing_repo_paths(self):
        """Kernel sub-builds obey the same exact source revision contract."""
        directory = self.recipe("linux-kernel")
        (directory / "build.py").unlink()
        (directory / "build.py").write_text(KERNEL_BUILDER)
        repositories = [repository(), *kernel_dependencies(self.root, "rolling")]
        before = (directory / "package.toml").read_text()
        sources.pin_recipe(self.root, "linux-kernel", repositories)
        text = (directory / "build.py").read_text()
        self.assertEqual(text.count("checkout_source(repo_dir, scm_url, commit_id)"), 2)
        compile(text, "build.py", "exec")
        shared = (self.root / "build.py").read_text()
        self.assertNotIn("checkout_source(", shared)
        compile(shared, "shared-build.py", "exec")
        self.assertEqual((directory / "build-intel-nic.sh").read_text(), INTEL_BUILDER)
        self.assertEqual((directory / "package.toml").read_text(), before)

    def kernel(self):
        """Write an audited custom kernel with driver and accel-PPP sources."""
        directory = self.recipe("linux-kernel")
        (directory / "build.py").unlink()
        (directory / "build.py").write_text(KERNEL_BUILDER)
        return directory, [repository(), *kernel_dependencies(self.root, "rolling")]

    def test_missing_kernel_source_pins_fail_before_adapting_builders(self):
        """A kernel matrix missing a declared source fails before changing builders."""
        directory, repositories = self.kernel()
        with self.assertRaisesRegex(ValueError, "planned repositories"):
            sources.pin_recipe(self.root, "linux-kernel", repositories[:-1])
        self.assertEqual((directory / "build.py").read_text(), KERNEL_BUILDER)
        self.assertEqual((self.root / "build.py").read_text(), BUILDER)

    def test_kernel_driver_reset_command_drift_fails_pinning(self):
        """Missing, changed, legacy, or extra resets fail before any builder is rewritten."""
        directory, repositories = self.kernel()
        driver = directory / "build-intel-nic.sh"
        current = "    git reset --hard HEAD\n"
        legacy = "    git reset --hard origin/main\n"
        unexpected = "    git reset --hard origin/master\n"
        for label, reset in (
            ("legacy origin/main", legacy),
            ("changed target", unexpected),
            ("changed options", "    git reset HEAD\n"),
            ("missing", ""),
            ("duplicate HEAD", current * 2),
            ("mixed", current + legacy),
            ("HEAD and unexpected", current + unexpected),
        ):
            with self.subTest(reset=label):
                (directory / "build.py").write_text(KERNEL_BUILDER)
                (self.root / "build.py").write_text(BUILDER)
                script = INTEL_BUILDER.replace(current, reset)
                driver.write_text(script)
                with self.assertRaisesRegex(ValueError, "audited"):
                    sources.pin_recipe(self.root, "linux-kernel", repositories)
                self.assertEqual((directory / "build.py").read_text(), KERNEL_BUILDER)
                self.assertEqual((self.root / "build.py").read_text(), BUILDER)
                self.assertEqual(driver.read_text(), script)

    def test_kernel_vpp_command_drift_fails_pinning(self):
        """Keep source discovery and execution on the same audited APT VPP sysroot."""
        directory, repositories = self.kernel()
        for label, script in (
            (
                "legacy nested build",
                (
                    "#!/bin/sh\nif [ ! -d ${VPP_LIB_CHECK_PATH} ]; then\n"
                    "    cd ../vpp/\n    ./build.py\n    cd ${CWD}\nfi\n"
                ),
            ),
            ("apt drift", APT_ACCEL_BUILDER.replace("dpkg-deb -x", "dpkg -i")),
        ):
            with self.subTest(case=label):
                (directory / "build-accel-ppp-ng.sh").write_text(script)
                with self.assertRaisesRegex(
                    ValueError, "audited APT VPP sysroot"
                ):
                    sources.pin_recipe(self.root, "linux-kernel", repositories)
                self.assertEqual((directory / "build.py").read_text(), KERNEL_BUILDER)
                self.assertEqual((self.root / "build.py").read_text(), BUILDER)

    def test_kea_nested_clone_is_pinned_and_source_order_is_irrelevant(self):
        """Kea's packaging clone gets the planned revision through the helper."""
        directory = self.recipe("isc-kea", 'pre_build_hook = "cd ..; ./prebuild.sh"\n')
        (directory / "prebuild.sh").write_text(
            '#!/bin/sh\nBRANCH="Kea-3.0.3"\n' + sources.KEA_CLONE + URL + "\n"
        )
        sources.pin_recipe(
            self.root,
            "isc-kea",
            [repository(), repository("kea-packaging", "Kea-3.0.3")],
        )
        script = (directory / "prebuild.sh").read_text()
        self.assertIn("python3 -m package_sources checkout", script)
        self.assertIn('--ref "$BRANCH"', script)
        self.assertIn("|| exit $?", script)
        self.assertNotIn("--unplanned", script)
        self.assertNotIn("git clone --branch", script)

    def test_unplanned_kea_uses_the_recipe_ref_and_stops_on_clone_failure(self):
        """The Test hook keeps its branch and never copies packaging after failure."""
        directory = self.recipe("isc-kea")
        config = (directory / "package.toml").read_text()
        path = directory / "prebuild.sh"
        path.write_text(
            '#!/bin/sh\nBRANCH="Kea-3.0.3"\n'
            + sources.KEA_CLONE
            + URL
            + "\ntouch copied\n"
        )
        sources.retry_recipe_clones(self.root, "isc-kea")
        self.assertEqual((directory / "package.toml").read_text(), config)
        self.assertIn(
            "run(['git', 'checkout', package['commit_id']]",
            (self.root / "build.py").read_text(),
        )
        self.assertIn('--ref "$BRANCH" --unplanned || exit $?', path.read_text())
        fake_python = directory / "python3"
        fake_python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 17\n')
        fake_python.chmod(0o755)
        result = subprocess.run(
            ["sh", str(path)],
            cwd=directory,
            env={
                **os.environ,
                "PATH": str(directory) + os.pathsep + os.environ["PATH"],
            },
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 17)
        self.assertEqual(
            result.stdout.splitlines()[-3:], ["--ref", "Kea-3.0.3", "--unplanned"]
        )
        self.assertFalse((directory / "copied").exists())

    def test_clone_command_drift_leaves_both_kernel_builders_unchanged(self):
        """Publish and Test fail before changing anything if an audited clone drifts."""
        directory, repositories = self.kernel()
        builder = directory / "build.py"
        changed = KERNEL_BUILDER.replace(
            "'clone', scm_url", "'clone', '--depth=1', scm_url"
        )
        builder.write_text(changed)
        for planned in (True, False):
            with (
                self.subTest(planned=planned),
                self.assertRaisesRegex(ValueError, "audited command"),
            ):
                if planned:
                    sources.pin_recipe(self.root, "linux-kernel", repositories)
                else:
                    sources.retry_recipe_clones(self.root, "linux-kernel")
            self.assertEqual(builder.read_text(), changed)
            self.assertEqual((self.root / "build.py").read_text(), BUILDER)

    def test_repository_or_checkout_command_drift_fails(self):
        """Unexpected upstream changes cannot silently compile untracked sources."""
        self.recipe("frr")
        with self.assertRaisesRegex(ValueError, "planned repositories"):
            sources.pin_recipe(self.root, "frr", [repository(ref="other")])
        (self.root / "build.py").write_text(BUILDER.replace("'checkout'", "'switch'"))
        with self.assertRaisesRegex(ValueError, "audited command"):
            sources.pin_recipe(self.root, "frr", [repository()])


@unittest.skipUnless(
    os.environ.get("VYOS_BUILD_ROOT"), "optional upstream recipe checkout"
)
class UpstreamSourceTests(unittest.TestCase):
    """Check source pinning against every catalog recipe without compiling it."""

    def test_all_catalog_recipe_source_adaptations_apply(self):
        """All currently supported source builders and nested hooks are audited."""
        source_root = Path(os.environ["VYOS_BUILD_ROOT"])
        for arch in ("amd64", "arm64"):
            for entry in sources.catalog.load_catalog()["build"]:
                name = entry["name"]
                with (
                    self.subTest(package=name, arch=arch),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    shutil.copy2(source_root / "build.py", root / "build.py")
                    shutil.copytree(source_root / name, root / name, symlinks=True)
                    build_policy.prepare(root, name, arch)
                    config = tomllib.loads((root / name / "package.toml").read_text())
                    repositories = [
                        repository(item["name"], item["commit_id"], url=item["scm_url"])
                        for item in config["packages"]
                        if item.get("scm_url")
                    ]
                    if name == "isc-kea":
                        extra = sources.kea_repository(
                            (root / name / "prebuild.sh").read_text()
                        )
                        repositories.append(dict(extra, commit=COMMIT))
                    sources.pin_recipe(root, name, repositories)
                    builder = (
                        root / name / "build.py"
                        if name == "linux-kernel"
                        else root / "build.py"
                    )
                    compile(builder.read_text(), "build.py", "exec")
                    if name == "linux-kernel" and any(
                        entry.get("build_cmd") == "build_intel_nic"
                        for entry in config["packages"]
                    ):
                        self.assertIn(
                            "    git reset --hard HEAD",
                            (root / name / "build-intel-nic.sh").read_text(),
                        )


if __name__ == "__main__":
    unittest.main()
