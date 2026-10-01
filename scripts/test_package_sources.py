"""Source-scoped fingerprints, Git resolution, and exact recipe pinning tests."""

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import tomllib

try:
    from . import package_sources as sources
    from . import prepare_package_build as build_policy
except ImportError:
    import package_sources as sources
    import prepare_package_build as build_policy

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
URL = "https://github.com/example/source.git"
BUILDER = """from subprocess import run, CalledProcessError

def checkout(package, repo_dir):
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
ACCEL_BUILDER = """#!/bin/sh
if [ ! -d ${VPP_LIB_CHECK_PATH} ]; then
    cd ../vpp/
    ./build.py
    cd ${CWD}
fi
"""
INTEL_BUILDER = """#!/bin/sh
set -e
cd "ethernet-linux-$1"
if [ -d .git ]; then
    git clean --force -d -x
    git reset --hard origin/main
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
    """Add the kernel's audited driver and nested VPP recipes to a scratch tree."""
    directory = root / "linux-kernel"
    (directory / "build-accel-ppp-ng.sh").write_text(ACCEL_BUILDER)
    (directory / "build-intel-nic.sh").write_text(INTEL_BUILDER)
    nested = root / "vpp"
    nested.mkdir()
    (nested / "build.py").symlink_to("../build.py")
    repositories = []
    for path, entries in (
        (
            directory / "package.toml",
            [("accel-ppp-ng", "build_accel_ppp_ng"), ("igb", "build_intel_nic")],
        ),
        (
            nested / "package.toml",
            [("vyos-vpp-patches", "/bin/true"), ("vpp", "make pkg-deb")],
        ),
    ):
        text = path.read_text() if path.exists() else ""
        for name, command in entries:
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
        """Legacy verification can reconstruct older scoped inputs safely."""
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

    def test_nested_vpp_sources_and_patched_recipe_only_affect_the_kernel(self):
        """Hash the consumed sibling recipe and both of its external repositories."""
        self.fixture.enable_kernel_dependencies()
        source = self.reader.recipe("linux-kernel", self.fixture.patch_commit)
        self.assertEqual(
            {entry["name"] for entry in source["repositories"]},
            {"accel-ppp-ng", "igb", "vpp", "vyos-vpp-patches"},
        )
        self.assertEqual(
            source["inputs"]["vpp_recipe_tree"],
            git(self.fixture.upstream, "rev-parse", "HEAD:scripts/package-build/vpp"),
        )
        before = {name: self.read(name) for name in ("linux-kernel", "frr", "other")}
        path = "scripts/package-build/vpp/package.toml"
        self.fixture.add_patch(
            path,
            (self.fixture.upstream / path).read_text() + "# nested recipe update\n",
        )
        self.assertNotEqual(self.read("linux-kernel"), before["linux-kernel"])
        for name in ("frr", "other"):
            self.assertEqual(self.read(name), before[name])

    def test_adding_nested_tracking_invalidates_only_incomplete_kernel_identities(self):
        """Previously untracked VPP inputs must not reuse an incomplete source key."""
        self.fixture.enable_kernel_dependencies()
        source = self.reader.recipe("linux-kernel", self.fixture.patch_commit)
        for entry in source["repositories"]:
            entry["commit"] = entry["ref"]
        old = copy.deepcopy(source)
        del old["inputs"]["vpp_recipe_tree"]
        old["repositories"] = [
            entry
            for entry in old["repositories"]
            if entry["name"] not in {"vpp", "vyos-vpp-patches"}
        ]
        self.assertNotEqual(sources.fingerprint(old), sources.fingerprint(source))
        for name in ("vpp", "vyos-vpp-patches"):
            changed = copy.deepcopy(source)
            next(entry for entry in changed["repositories"] if entry["name"] == name)[
                "commit"
            ] = OTHER_COMMIT
            self.assertNotEqual(
                sources.fingerprint(changed), sources.fingerprint(source)
            )

    def test_kernel_nested_vpp_command_drift_fails_discovery(self):
        """Do not silently omit or guess a changed sibling build command."""
        self.fixture.enable_kernel_dependencies()
        self.fixture.update_upstream(
            "scripts/package-build/linux-kernel/build-accel-ppp-ng.sh",
            ACCEL_BUILDER.replace("cd ../vpp/", "cd ../other/"),
        )
        with self.assertRaisesRegex(ValueError, "audited nested VPP build"):
            self.reader.recipe("linux-kernel", self.fixture.patch_commit)

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

    def test_github_short_commit_resolution_and_memoization(self):
        """GitHub abbreviated commits use its commit API once per ref."""
        with patch.object(
            sources, "command_output", side_effect=["", COMMIT]
        ) as output:
            self.assertEqual(self.resolver.resolve(URL, "a" * 8), COMMIT)
            self.assertEqual(self.resolver.resolve(URL, "a" * 8), COMMIT)
            self.assertEqual(self.resolver.resolve(URL, COMMIT), COMMIT)
        self.assertEqual(
            output.call_args_list,
            [
                call(["git", "ls-remote", "--", URL]),
                call(
                    [
                        "gh",
                        "api",
                        "repos/example/source/commits/aaaaaaaa",
                        "--jq",
                        ".sha",
                    ]
                ),
            ],
        )

    def test_gitlab_short_commits_use_an_encoded_project_commit_api(self):
        """Public GitLab commit lookup avoids cloning packaging histories."""
        with patch.object(
            sources, "command_output", side_effect=["", json.dumps({"id": COMMIT})]
        ) as output:
            self.assertEqual(
                self.resolver.resolve(
                    "https://salsa.debian.org/debian/pkg.git", "a" * 8
                ),
                COMMIT,
            )
        self.assertEqual(output.call_count, 2)
        output.assert_called_with(
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

    def test_evidence_commit_expansion_must_preserve_the_recorded_hash_prefix(self):
        """An API resolving a hash-like ref name cannot substitute for a checkout ID."""
        with (
            patch.object(sources, "command_output", return_value=COMMIT),
            self.assertRaisesRegex(ValueError, "source revision changed"),
        ):
            self.resolver.resolve_evidence(URL, "bbbbbbbb", COMMIT)

    def test_evidence_does_not_guess_short_ids_or_clone_large_unneeded_repositories(
        self,
    ):
        """Unknown-host abbreviated logs need existing metadata or a full ID."""
        url = "https://git.kernel.org/firmware.git"
        with patch.object(sources, "command_output") as output:
            with self.assertRaisesRegex(ValueError, "unverifiable abbreviated"):
                self.resolver.resolve_evidence(url, COMMIT[:8], COMMIT)
            self.assertEqual(
                self.resolver.resolve_evidence(url, COMMIT, COMMIT), COMMIT
            )
            with self.assertRaisesRegex(ValueError, "source revision changed"):
                self.resolver.resolve_evidence(url, COMMIT, OTHER_COMMIT)
        output.assert_not_called()

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

    def test_kernel_nested_builder_and_driver_reset_preserve_planned_commits(self):
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
        git(self.root, "clone", "--quiet", self.url, str(checkout))
        with (
            patch.dict(os.environ, {sources.PIN_ENV: json.dumps(repositories)}),
            patch.dict(sys.modules, {"package_sources": sources}),
        ):
            modules = []
            for name, path in (
                ("shared_fixture", root / "build.py"),
                ("kernel_fixture", directory / "build.py"),
            ):
                spec = importlib.util.spec_from_file_location(name, path)
                if spec is None or spec.loader is None:
                    raise ValueError("fixture builder cannot be imported")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                modules.append(module)
            shared, kernel = modules
            self.assertEqual(
                shared.checkout(
                    {"scm_url": self.url, "commit_id": "rolling"}, checkout
                ),
                "rolling",
            )
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
        before = {
            name: (self.root / name / "package.toml").read_text()
            for name in ("linux-kernel", "vpp")
        }
        sources.pin_recipe(self.root, "linux-kernel", repositories)
        text = (directory / "build.py").read_text()
        self.assertEqual(text.count("checkout_source(repo_dir, scm_url, commit_id)"), 2)
        compile(text, "build.py", "exec")
        shared = (self.root / "build.py").read_text()
        self.assertIn(
            "checkout_source(repo_dir, package['scm_url'], package['commit_id'])",
            shared,
        )
        compile(shared, "shared-build.py", "exec")
        self.assertNotIn(
            "git reset --hard origin/main",
            (directory / "build-intel-nic.sh").read_text(),
        )
        self.assertIn(
            "git reset --hard HEAD", (directory / "build-intel-nic.sh").read_text()
        )
        for name, text in before.items():
            self.assertEqual((self.root / name / "package.toml").read_text(), text)

    def kernel(self):
        """Write an audited custom kernel with driver and nested VPP sources."""
        directory = self.recipe("linux-kernel")
        (directory / "build.py").unlink()
        (directory / "build.py").write_text(KERNEL_BUILDER)
        return directory, [repository(), *kernel_dependencies(self.root, "rolling")]

    def test_missing_nested_vpp_source_pins_fail_before_adapting_builders(self):
        """A kernel matrix without both VPP sources cannot compile untracked branches."""
        directory, repositories = self.kernel()
        with self.assertRaisesRegex(ValueError, "planned repositories"):
            sources.pin_recipe(self.root, "linux-kernel", repositories[:-1])
        self.assertEqual((directory / "build.py").read_text(), KERNEL_BUILDER)
        self.assertEqual((self.root / "build.py").read_text(), BUILDER)

    def test_kernel_driver_reset_command_drift_fails_pinning(self):
        """A changed reset target requires review instead of bypassing planned SHAs."""
        directory, repositories = self.kernel()
        (directory / "build-intel-nic.sh").write_text(
            INTEL_BUILDER.replace("origin/main", "origin/master")
        )
        with self.assertRaisesRegex(ValueError, "audited command"):
            sources.pin_recipe(self.root, "linux-kernel", repositories)

    def test_kernel_nested_vpp_build_command_drift_fails_pinning(self):
        """Keep source discovery and execution on the same audited sibling recipe."""
        directory, repositories = self.kernel()
        (directory / "build-accel-ppp-ng.sh").write_text(
            ACCEL_BUILDER.replace("./build.py", "./build.py --packages vpp")
        )
        with self.assertRaisesRegex(ValueError, "audited nested VPP build"):
            sources.pin_recipe(self.root, "linux-kernel", repositories)

    def test_kernel_nested_vpp_custom_builder_is_rejected(self):
        """Nested clones cannot bypass the shared builder's checked adaptations."""
        _, repositories = self.kernel()
        (self.root / "vpp/build.py").unlink()
        (self.root / "vpp/build.py").write_text(BUILDER)
        with self.assertRaisesRegex(ValueError, "unaudited custom source builder"):
            sources.pin_recipe(self.root, "linux-kernel", repositories)

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
        self.assertNotIn("git clone --branch", script)

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
                    if name == "linux-kernel" and sources.kernel_uses_vpp(config):
                        shutil.copytree(
                            source_root / "vpp", root / "vpp", symlinks=True
                        )
                        nested = tomllib.loads((root / "vpp/package.toml").read_text())
                        repositories.extend(
                            dict(item, commit=COMMIT)
                            for item in sources.recipe_repositories(nested)
                        )
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
                    if name == "linux-kernel" and sources.kernel_uses_vpp(config):
                        compile(
                            (root / "build.py").read_text(), "nested-build.py", "exec"
                        )
                        self.assertNotIn(
                            "git reset --hard origin/main",
                            (root / name / "build-intel-nic.sh").read_text(),
                        )


if __name__ == "__main__":
    unittest.main()
