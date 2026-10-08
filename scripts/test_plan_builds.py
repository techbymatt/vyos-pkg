"""Pure planner and mocked git/GitHub/HTTP boundary tests."""

import argparse
import copy
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

try:
    from . import plan_builds as planner
except ImportError:
    import plan_builds as planner

REVISION = "a" * 40
IMAGE = "ghcr.io/example/build@sha256:" + "c" * 64


def record(name="frr", arch="amd64", group="build", deps=""):
    """Build a minimal source record row."""
    source = {
        "recipe_tree": REVISION if group == "build" else None,
        "inputs": {},
        "repositories": [
            {
                "name": name,
                "url": f"https://github.com/vyos/{name}.git",
                "ref": "rolling",
                "commit": REVISION,
            }
        ],
    }
    return {
        "group": group,
        "package": name,
        "arch": arch,
        "commit": REVISION,
        "deps": deps,
        "source": source,
        "source_digest": planner.package_sources.fingerprint(source),
    }


def cache_key(row, run="122-1"):
    """Build the deterministic cache key for a record row."""
    return f"cache-v3-{row['group']}-{row['package']}-{row['arch']}-{row['source_digest']}-{run}"


class TestPlannerTests(unittest.TestCase):
    """Tests for the Test-workflow planner."""

    def test_raw_inputs_and_explicit_deps(self) -> None:
        """Raw inputs and explicit deps shape both build matrices."""
        result = planner.plan_test(
            "pyhumps, frr\nfrr\tnew-source", "hvinfo,live-boot", "bison, flex\tbison"
        )
        entries = result["build-matrix"]["include"]
        self.assertEqual(
            [(e["package"], e["arch"]) for e in entries],
            [
                ("pyhumps", "amd64"),
                ("frr", "amd64"),
                ("frr", "arm64"),
                ("new-source", "amd64"),
                ("new-source", "arm64"),
            ],
        )
        extra = result["build-extra-matrix"]["include"]
        self.assertEqual([e["deps"] for e in extra], ["bison flex"] * 3)
        self.assertEqual(result["verify-arches"], ["amd64", "arm64"])
        self.assertEqual(
            result["verify-plan"],
            [
                "deb-frr-amd64",
                "deb-frr-arm64",
                "deb-hvinfo-amd64",
                "deb-hvinfo-arm64",
                "deb-live-boot-amd64",
                "deb-new-source-amd64",
                "deb-new-source-arm64",
                "deb-pyhumps-amd64",
            ],
        )
        self.assertEqual(
            planner.plan_test("", "hvinfo", "")["build-extra-matrix"]["include"][0][
                "deps"
            ],
            "",
        )

    def test_empty_and_single_arch_plans(self) -> None:
        """Blank input plans nothing; amd64-only sources pin verify arches."""
        result = planner.plan_test(" ,\t", "", "")
        self.assertEqual(result["verify-arches"], [])
        self.assertEqual(result["build-matrix"], {"include": []})
        self.assertEqual(result["verify-plan"], [])
        self.assertEqual(
            planner.plan_test("shim-signed", "vyos-live-build", "")["verify-arches"],
            ["amd64"],
        )

    def test_unsafe_inputs_and_cross_group_duplicates_rejected(self) -> None:
        """Unsafe packages, deps, and cross-group duplicates raise ValueError."""
        for packages, extra, deps in [
            ("../foo", "", ""),
            ("foo;id", "", ""),
            ("", "foo", "--allow-unauthenticated"),
            ("foo", "foo", ""),
        ]:
            with (
                self.subTest(packages=packages, extra=extra, deps=deps),
                self.assertRaises(ValueError),
            ):
                planner.plan_test(packages, extra, deps)

    def test_cli_stdout_is_only_output_lines(self) -> None:
        """The CLI writes only contracted name=value JSON lines to stdout."""
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = planner.main(
                [
                    "test",
                    "--packages",
                    "pyhumps, frr",
                    "--extra-packages",
                    "hvinfo",
                    "--deps",
                    "bison flex",
                ]
            )
        self.assertEqual(status, 0)
        outputs = {
            name: json.loads(value)
            for name, value in (
                line.split("=", 1) for line in stdout.getvalue().splitlines()
            )
        }
        self.assertEqual(
            set(outputs),
            {"build-matrix", "build-extra-matrix", "verify-arches", "verify-plan"},
        )
        self.assertEqual(outputs["verify-arches"], ["amd64", "arm64"])
        self.assertEqual(
            outputs["verify-plan"],
            [
                "deb-frr-amd64",
                "deb-frr-arm64",
                "deb-hvinfo-amd64",
                "deb-hvinfo-arm64",
                "deb-pyhumps-amd64",
            ],
        )

    def test_output_lines_reject_line_breaks(self) -> None:
        """GITHUB_OUTPUT emission rejects values spanning multiple lines."""
        self.assertEqual(
            list(planner.output_lines({"changed": True})), ["changed=true"]
        )
        for value in ("a\nb", "a\rb"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "would span multiple lines"),
            ):
                list(planner.output_lines({"value": value}))


class PublishPlannerTests(unittest.TestCase):
    """Tests for the Publish-workflow planner."""

    def test_misses_preserve_groups_dependencies_and_runners(self) -> None:
        """Cache misses plan grouped builds with deps and runner labels intact."""
        rows = [record(), record("hvinfo", "arm64", "build-extra", "gnat gprbuild")]
        before = copy.deepcopy(rows)
        result = planner.plan_publish(rows, [], "123-1")
        self.assertEqual(result["restore-matrix"], {"include": []})
        self.assertEqual(
            result["build-matrix"]["include"],
            [
                {
                    "group": "build",
                    "package": "frr",
                    "arch": "amd64",
                    "commit": REVISION,
                    "source": rows[0]["source"],
                    "source_digest": rows[0]["source_digest"],
                    "runner_label": "ubuntu-26.04",
                    "cache_key": cache_key(rows[0], "123-1"),
                    "timeout_minutes": 150,
                    "go": False,
                }
            ],
        )
        self.assertEqual(
            result["build-extra-matrix"]["include"][0]["deps"], "gnat gprbuild"
        )
        self.assertEqual(
            result["build-extra-matrix"]["include"][0]["runner_label"],
            "ubuntu-26.04-arm",
        )
        self.assertEqual(rows, before)

    def test_newest_visible_cache_and_exact_prefix(self) -> None:
        """Newest exact-prefix cache on a visible ref counts as a hit."""
        row = record()
        caches = [
            {
                "key": cache_key(row, "old"),
                "ref": "refs/heads/topic",
                "created_at": "2026-01-01",
            },
            {
                "key": cache_key(row, "hidden"),
                "ref": "refs/heads/other",
                "created_at": "2026-01-04",
            },
            {
                "key": cache_key(row, "new"),
                "ref": "refs/heads/main",
                "created_at": "2026-01-03",
            },
            {
                "key": "not-" + cache_key(row),
                "ref": "refs/heads/main",
                "created_at": "2026-01-05",
            },
        ]
        keys = planner.visible_cache_keys(caches, "refs/heads/topic", "main")
        result = planner.plan_publish([row], keys, "123-1")
        self.assertEqual(
            result["restore-matrix"]["include"][0]["entries"][0]["cache_key"],
            cache_key(row, "new"),
        )
        self.assertEqual(result["build-matrix"]["include"], [])
        result = planner.plan_publish([row], ["not-" + cache_key(row)], "123-1")
        self.assertEqual(len(result["build-matrix"]["include"]), 1)

    def test_default_branch_cannot_restore_the_six_feature_branch_caches(self):
        """Repository API visibility is not workflow restore visibility."""
        names = (
            "libnss-mapuser",
            "libpam-radius-auth",
            "linux-kernel",
            "shim-signed",
            "tacacs",
            "vyos-1x",
        )
        rows = [
            record(name, arch)
            for name in names
            for arch in (("amd64",) if name == "shim-signed" else ("amd64", "arm64"))
        ]
        caches = [
            {
                "key": cache_key(row),
                "ref": "refs/heads/fix/ci/caching",
                "created_at": "2026-10-01T10:00:00Z",
            }
            for row in rows
        ]
        keys = planner.visible_cache_keys(caches, "refs/heads/rolling", "rolling")
        result = planner.plan_publish(rows, keys, "123-1")
        self.assertEqual(len(result["build-matrix"]["include"]), 11)
        self.assertEqual(result["restore-matrix"]["include"], [])
        caches.extend(
            dict(cache, ref="refs/heads/rolling", created_at="2026-10-01T12:00:00Z")
            for cache in list(caches)
        )
        keys = planner.visible_cache_keys(caches, "refs/heads/rolling", "rolling")
        result = planner.plan_publish(rows, keys, "124-1")
        self.assertEqual(result["build-matrix"]["include"], [])
        self.assertEqual(
            sum(len(batch["entries"]) for batch in result["restore-matrix"]["include"]),
            11,
        )
        self.assertEqual(len(result["verify-plan"]), 11)

    def test_kernel_tracking_change_preserves_other_requested_source_caches(self):
        """A complete kernel descriptor invalidates only the two kernel producers."""
        names = (
            "libnss-mapuser",
            "libpam-radius-auth",
            "linux-kernel",
            "shim-signed",
            "tacacs",
            "vyos-1x",
        )
        rows = [
            record(name, arch)
            for name in names
            for arch in (("amd64",) if name == "shim-signed" else ("amd64", "arm64"))
        ]
        keys = [cache_key(row) for row in rows]
        for row in rows:
            if row["package"] == "linux-kernel":
                row["source"]["inputs"]["vpp_recipe_tree"] = "b" * 40
                row["source"]["repositories"].append(
                    {
                        "name": "vpp",
                        "url": "https://github.com/FDio/vpp",
                        "ref": "stable/2510",
                        "commit": "b" * 40,
                    }
                )
                row["source_digest"] = planner.package_sources.fingerprint(
                    row["source"]
                )
        result = planner.plan_publish(rows, keys, "123-1")
        self.assertEqual(
            [
                (row["package"], row["arch"])
                for row in result["build-matrix"]["include"]
            ],
            [("linux-kernel", "amd64"), ("linux-kernel", "arm64")],
        )
        self.assertEqual(
            sum(len(batch["entries"]) for batch in result["restore-matrix"]["include"]),
            9,
        )
        self.assertEqual(len(result["verify-plan"]), 11)

    def test_force_refresh_and_unchanged_inputs(self) -> None:
        """Unchanged inputs skip all work unless force_rebuild is set."""
        row = record()
        for keys in ([], [cache_key(row)]):
            result = planner.plan_publish([row], keys, "123-1", changed=False)
            self.assertTrue(
                all(
                    result[name] == {"include": []}
                    for name in ("build-matrix", "build-extra-matrix", "restore-matrix")
                )
            )
            self.assertEqual(result["verify-plan"], [])
            result = planner.plan_publish(
                [row], keys, "123-1", changed=False, force_rebuild=True
            )
            self.assertEqual(
                result["build-matrix"]["include"][0]["cache_key"],
                cache_key(row, "123-1"),
            )
            self.assertEqual(result["restore-matrix"], {"include": []})
            self.assertEqual(result["verify-plan"], ["deb-frr-amd64"])

    def test_restore_batches_eight_without_mixing_runners(self) -> None:
        """Restore entries batch to eight per job without mixing runners."""
        rows = [
            record(f"source-{index}", arch)
            for index in range(9)
            for arch in ("amd64", "arm64")
        ]
        keys = [cache_key(row) for row in rows]
        batches = planner.plan_publish(rows, keys, "123-1")["restore-matrix"]["include"]
        self.assertEqual([len(batch["entries"]) for batch in batches], [8, 1, 8, 1])
        self.assertEqual(
            [batch["runner_label"] for batch in batches],
            ["ubuntu-26.04"] * 2 + ["ubuntu-26.04-arm"] * 2,
        )
        self.assertEqual(
            len(
                {
                    (e["package"], e["arch"])
                    for batch in batches
                    for e in batch["entries"]
                }
            ),
            18,
        )

    def test_verify_plan_covers_build_and_restored_records(self) -> None:
        """Every planned producer contributes exactly one expected directory."""
        rows = [record(), record("hvinfo", "arm64", "build-extra", "gnat gprbuild")]
        result = planner.plan_publish(rows, [cache_key(rows[0])], "123-1")
        self.assertEqual(result["verify-plan"], ["deb-frr-amd64", "deb-hvinfo-arm64"])

    def test_source_change_rebuilds_only_that_sources_architectures(self) -> None:
        """A recipe or cloned revision change leaves unrelated cache hits intact."""
        before = [
            record(name, arch)
            for name in ("frr", "other")
            for arch in ("amd64", "arm64")
        ]
        keys = [cache_key(row) for row in before]
        for field in ("recipe_tree", "repository_commit"):
            with self.subTest(input=field):
                rows = copy.deepcopy(before)
                for row in rows:
                    if row["package"] == "frr":
                        if field == "recipe_tree":
                            row["source"]["recipe_tree"] = "c" * 40
                        else:
                            row["source"]["repositories"][0]["commit"] = "c" * 40
                        row["source_digest"] = planner.package_sources.fingerprint(
                            row["source"]
                        )
                result = planner.plan_publish(rows, keys, "123-1")
                self.assertEqual(
                    [
                        (entry["package"], entry["arch"])
                        for entry in result["build-matrix"]["include"]
                    ],
                    [("frr", "amd64"), ("frr", "arm64")],
                )
                self.assertEqual(
                    [
                        entry["package"]
                        for batch in result["restore-matrix"]["include"]
                        for entry in batch["entries"]
                    ],
                    ["other", "other"],
                )

    def test_local_dependencies_do_not_invalidate_source_caches(self) -> None:
        """Local prerequisites can republish without recompiling unchanged sources."""
        row = record("hvinfo", group="build-extra", deps="gnat")
        key = cache_key(row)
        row["deps"] = "gnat gprbuild"
        result = planner.plan_publish([row], [key], "123-1")
        self.assertEqual(result["build-extra-matrix"]["include"], [])
        self.assertEqual(
            result["restore-matrix"]["include"][0]["entries"][0]["cache_key"], key
        )

    def test_legacy_keys_require_proof_and_are_saved_under_source_keys(self) -> None:
        """Ignoring the legacy namespace alone never authorizes cache reuse."""
        row = record()
        key = f"cache-v2-frr-amd64-{REVISION}-{'b' * 64}-122-1"
        result = planner.plan_publish([row], [key], "123-1")
        self.assertEqual(len(result["build-matrix"]["include"]), 1)
        proven = {("build", "frr", "amd64"): key}
        result = planner.plan_publish([row], [key], "123-1", legacy_matches=proven)
        entry = result["restore-matrix"]["include"][0]["entries"][0]
        self.assertEqual(entry["cache_key"], key)
        self.assertEqual(entry["save_cache_key"], cache_key(row, "123-1"))
        result = planner.plan_publish(
            [row], [key], "123-1", legacy_matches=proven, force_rebuild=True
        )
        self.assertEqual(result["restore-matrix"]["include"], [])

    def test_source_records_cover_catalog_policy(self) -> None:
        """Every catalog source yields a record; missing revisions are rejected."""
        sources = planner.catalog.load_catalog()
        revisions = {
            (group, entry["name"]): {
                key: value
                for key, value in record(entry["name"], group=group).items()
                if key in ("commit", "source", "source_digest")
            }
            for group in sources
            for entry in sources[group]
        }
        rows = planner.source_records(sources, revisions)
        self.assertEqual(len(rows), 99)
        self.assertEqual(
            [r["arch"] for r in rows if r["package"] == "shim-signed"], ["amd64"]
        )
        self.assertEqual(
            [r["deps"] for r in rows if r["package"] == "hvinfo"], ["gnat gprbuild"] * 2
        )
        revisions["build", "frr"]["commit"] = ""
        with self.assertRaisesRegex(ValueError, "revision"):
            planner.source_records(sources, revisions)


class CacheDiagnosticTests(unittest.TestCase):
    """Final decision reasons distinguish source changes, cache scope, and migration."""

    def test_actual_source_changes_take_precedence_over_legacy_fallback_failures(self):
        """Show the kernel version and cloned SHA, not a rejected old cache's missing log."""
        old = record("linux-kernel")
        old["source"]["inputs"]["kernel_version"] = "6.18.50"
        old["source_digest"] = planner.package_sources.fingerprint(old["source"])
        new = copy.deepcopy(old)
        new["source"]["inputs"]["kernel_version"] = "6.18.54"
        new["source"]["recipe_tree"] = "b" * 40
        new["source"]["repositories"][0]["commit"] = "c" * 40
        new["source_digest"] = planner.package_sources.fingerprint(new["source"])
        reason = planner.build_reason(
            new,
            old,
            [],
            "refs/heads/rolling",
            legacy_note="producer did not record the checkout",
        )
        self.assertIn("source inputs changed", reason)
        self.assertIn("kernel_version: 6.18.50 -> 6.18.54", reason)
        self.assertIn(f"recipe_tree: {REVISION} -> {'b' * 40}", reason)
        self.assertIn(f"linux-kernel commit: {REVISION} -> {'c' * 40}", reason)
        self.assertNotIn("did not record", reason)

    def test_matching_hidden_cache_and_unproven_legacy_inputs_are_explained(self):
        """An existing archive can be both inaccessible and lacking fallback proof."""
        row = record()
        caches = [
            {"key": cache_key(row), "ref": "refs/heads/fix/ci/caching"},
            {"key": "not-" + cache_key(row), "ref": "refs/heads/unrelated"},
        ]
        reason = planner.build_reason(
            row,
            row,
            caches,
            "refs/heads/rolling",
            legacy_note="producer did not record the frr checkout",
        )
        self.assertIn(
            "matching source cache inaccessible from refs/heads/rolling: refs/heads/fix/ci/caching",
            reason,
        )
        self.assertIn(
            "legacy migration requires an initial build: producer did not record the frr checkout",
            reason,
        )
        self.assertNotIn("unrelated", reason)
        self.assertEqual(
            planner.build_reason(
                row, row, caches, "refs/heads/rolling", force_rebuild=True
            ),
            "forced rebuild",
        )
        self.assertEqual(
            planner.build_reason(row, row, [], "refs/heads/rolling"),
            "no matching visible source cache",
        )

    def test_source_changes_identify_added_removed_and_changed_repositories(self):
        """Report nested sources and ref/URL changes even when checkout SHAs match."""
        old = record()
        new = record("vpp")
        changes = planner.source_changes(old, new)
        self.assertIn(f"frr: repository removed ({REVISION})", changes)
        self.assertIn(f"vpp: repository added ({REVISION})", changes)
        new = copy.deepcopy(old)
        new["source"]["repositories"][0].update(
            url="https://example.org/frr.git", ref="v1"
        )
        changes = planner.source_changes(old, new)
        self.assertIn(
            "frr url: https://github.com/vyos/frr.git -> https://example.org/frr.git",
            changes,
        )
        self.assertIn("frr ref: rolling -> v1", changes)


class PublishBoundaryTests(unittest.TestCase):
    """Mocked git/GitHub/HTTP boundary tests for run_publish."""

    def setUp(self) -> None:
        """Build a scratch workflow root with a fixture catalog."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts/package_catalog.json").write_text(
            json.dumps(
                {
                    "build": [
                        {"name": "linux-kernel"},
                        {"name": "pyhumps", "architecture": "independent_only"},
                    ],
                    "build-extra": [{"name": "hvinfo", "deps": ["gnat", "gprbuild"]}],
                }
            )
        )
        (self.root / "vyos-pkg.asc").write_bytes(b"public key")
        self.args = argparse.Namespace(
            patch_root=self.root / "patch",
            workflow_root=self.root,
            image=IMAGE,
            repository_commit="d" * 40,
            repository="owner/repo",
            ref="refs/heads/topic",
            run="123-1",
            force_rebuild=False,
        )
        self.calls = []
        self.published = None
        self.caches = []
        self.resolved = {
            (group, name): {
                key: value
                for key, value in record(name, group=group).items()
                if key in ("commit", "source", "source_digest")
            }
            for group, name in (
                ("build", "linux-kernel"),
                ("build", "pyhumps"),
                ("build-extra", "hvinfo"),
            )
        }

    def output(self, command, cwd=None):
        """Record and stub git, gh, and curl command outputs."""
        self.calls.append((command, cwd))
        if command[0] == "git":
            return REVISION
        if command[:2] == ["gh", "api"]:
            if "--paginate" in command:
                return json.dumps(
                    [{"actions_caches": self.caches}, {"actions_caches": []}]
                )
            if command[2].endswith("/pages"):
                return "https://example.org/repo/"
            return "main"
        if command[0] == "curl":
            if self.published is None:
                raise subprocess.CalledProcessError(22, command)
            return self.published
        raise AssertionError(command)

    def run_publish(self):
        """Run run_publish against the stubbed command boundaries."""
        stderr = io.StringIO()
        with (
            patch.object(planner, "command_output", side_effect=self.output),
            patch.object(
                planner.package_sources, "resolve_sources", return_value=self.resolved
            ) as resolution,
            redirect_stderr(stderr),
        ):
            result = planner.run_publish(self.args)
        self.diagnostics = stderr.getvalue()
        return result, resolution

    def test_publish_source_and_download_boundaries(self) -> None:
        """Source resolution and manifest download use the expected boundaries."""
        result, resolution = self.run_publish()
        self.assertTrue(result["changed"])
        self.assertEqual(result["patch-commit"], REVISION)
        self.assertEqual(len(result["build-matrix"]["include"]), 3)
        self.assertEqual(len(result["build-extra-matrix"]["include"]), 2)
        resolution.assert_called_once()
        reader, resolver, catalog, patch_commit = resolution.call_args.args
        self.assertEqual(reader.patch_root, self.args.patch_root)
        self.assertIsInstance(resolver, planner.package_sources.Resolver)
        self.assertEqual(
            catalog,
            planner.catalog.load_catalog(self.root / "scripts/package_catalog.json"),
        )
        self.assertEqual(patch_commit, REVISION)
        curl = next(command for command, _ in self.calls if command[0] == "curl")
        self.assertEqual(
            curl[-1], "https://example.org/repo/input-manifest.json?run=123-1"
        )
        self.assertIn("Cache-Control: no-cache", curl)
        saved = planner.manifest.load_manifest(self.root / "input-manifest.json")
        self.assertEqual(len(saved["packages"]), 5)
        self.assertEqual(saved["schema_version"], 2)
        self.assertTrue(all("source_digest" in row for row in saved["packages"]))
        self.assertTrue(all("cache_key" not in row for row in saved["packages"]))
        self.assertEqual(
            result["verify-plan"],
            [
                "deb-hvinfo-amd64",
                "deb-hvinfo-arm64",
                "deb-linux-kernel-amd64",
                "deb-linux-kernel-arm64",
                "deb-pyhumps-amd64",
            ],
        )

    def test_only_deployed_manifest_suppresses_publication(self) -> None:
        """A deployed matching manifest suppresses publication until force_rebuild."""
        self.run_publish()
        self.assertTrue(self.run_publish()[0]["changed"])
        self.published = (self.root / "input-manifest.json").read_text()
        result, _ = self.run_publish()
        self.assertFalse(result["changed"])
        self.assertTrue(
            all(
                result[name]["include"] == []
                for name in ("build-matrix", "build-extra-matrix", "restore-matrix")
            )
        )
        self.assertEqual(result["verify-plan"], [])
        self.args.force_rebuild = True
        self.calls.clear()
        self.assertTrue(self.run_publish()[0]["changed"])
        self.assertFalse(any(command[0] == "curl" for command, _ in self.calls))

    def test_image_change_republishes_only_restored_packages(self) -> None:
        """A new build image changes publication inputs, never source cache keys."""
        self.run_publish()
        self.published = (self.root / "input-manifest.json").read_text()
        rows = json.loads(self.published)["packages"]
        self.caches = [
            {
                "key": cache_key(row),
                "ref": "refs/heads/main",
                "created_at": "2026-01-01",
            }
            for row in rows
        ]
        self.args.image = "ghcr.io/example/build@sha256:" + "e" * 64
        result, _ = self.run_publish()
        self.assertTrue(result["changed"])
        self.assertEqual(result["build-matrix"]["include"], [])
        self.assertEqual(result["build-extra-matrix"]["include"], [])
        self.assertEqual(
            sum(len(batch["entries"]) for batch in result["restore-matrix"]["include"]),
            5,
        )
        self.assertIn(
            "restore (matching source inputs; cache cache-v3-", self.diagnostics
        )

    def test_hidden_matching_caches_are_only_used_for_diagnostics(self):
        """The real publish boundary never passes inaccessible keys to migration or restore."""
        self.run_publish()
        rows = planner.manifest.load_manifest(self.root / "input-manifest.json")[
            "packages"
        ]
        self.caches = [
            {
                "key": cache_key(row),
                "ref": "refs/heads/fix/ci/caching",
                "created_at": "2026-10-01",
            }
            for row in rows
        ]
        with patch.object(
            planner.legacy_package_cache.LegacyVerifier, "matches", return_value={}
        ) as proof:
            result, _ = self.run_publish()
        self.assertEqual(proof.call_args.args[1], [])
        self.assertEqual(result["restore-matrix"]["include"], [])
        self.assertIn(
            "matching source cache inaccessible from refs/heads/topic: refs/heads/fix/ci/caching",
            self.diagnostics,
        )

    def test_verified_legacy_inputs_are_wired_to_the_restore_save_entry(self) -> None:
        """Verified old archives migrate using the new source key in the same run."""
        key = f"cache-v2-linux-kernel-amd64-{REVISION}-{'b' * 64}-122-1"
        self.caches = [
            {"key": key, "ref": "refs/heads/main", "created_at": "2026-01-01"}
        ]
        with patch.object(
            planner.legacy_package_cache.LegacyVerifier,
            "matches",
            return_value={("build", "linux-kernel", "amd64"): key},
        ) as proof:
            result, _ = self.run_publish()
        proof.assert_called_once()
        entry = result["restore-matrix"]["include"][0]["entries"][0]
        self.assertEqual(entry["cache_key"], key)
        self.assertEqual(
            entry["save_cache_key"], cache_key(record("linux-kernel"), "123-1")
        )

    def test_forced_and_unchanged_publications_do_not_query_legacy_evidence(
        self,
    ) -> None:
        """Source proof is only needed when publication will restore old archives."""
        self.run_publish()
        self.published = (self.root / "input-manifest.json").read_text()
        with patch.object(
            planner.legacy_package_cache.LegacyVerifier, "matches"
        ) as proof:
            self.assertFalse(self.run_publish()[0]["changed"])
            self.args.force_rebuild = True
            self.assertTrue(self.run_publish()[0]["changed"])
        proof.assert_not_called()

    def test_forced_and_unchanged_publications_skip_paginated_cache_metadata(
        self,
    ) -> None:
        """Cache API work cannot improve no-op or forced-build decisions."""
        self.run_publish()
        self.published = (self.root / "input-manifest.json").read_text()
        for force in (False, True):
            self.args.force_rebuild = force
            self.calls.clear()
            result, _ = self.run_publish()
            self.assertEqual(result["changed"], force)
            self.assertFalse(any("--paginate" in command for command, _ in self.calls))

    def test_invalid_deployed_manifest_republishes(self) -> None:
        """Broken or malformed deployed manifests trigger republication."""
        for text in ("{broken", "{}", '{"schema_version":1,"schema_version":1}'):
            self.published = text
            self.assertTrue(self.run_publish()[0]["changed"])

    def test_published_manifest_timeout_is_unavailable_not_a_cache_miss(self) -> None:
        """An unavailable deployment permits planning; cache metadata still fails closed."""
        with (
            patch.object(
                planner,
                "command_output",
                side_effect=subprocess.TimeoutExpired("curl", 120),
            ),
            redirect_stderr(io.StringIO()),
        ):
            self.assertIsNone(planner.fetch_published("owner/repo", "123-1"))

    def test_missing_source_revision_fails_before_cache_planning(self) -> None:
        """Source lookup failures stop publication, not trigger mass cache misses."""
        with (
            patch.object(planner, "command_output", side_effect=self.output),
            patch.object(
                planner.package_sources,
                "resolve_sources",
                side_effect=ValueError("missing source ref"),
            ),
            self.assertRaisesRegex(ValueError, "missing source ref"),
        ):
            planner.run_publish(self.args)
        self.assertFalse(any(command[0] == "gh" for command, _ in self.calls))

    def test_cache_api_failure_is_not_treated_as_a_miss(self) -> None:
        """Cache API errors propagate instead of planning rebuilds."""
        with (
            patch.object(
                planner,
                "command_output",
                side_effect=subprocess.CalledProcessError(1, "gh"),
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            planner.fetch_caches("owner/repo", "refs/heads/main")

    def test_cache_pages_are_combined_before_visibility_and_recency_selection(
        self,
    ) -> None:
        """Paginated cache pages are slurped together before filtering and sorting."""
        pages = [
            {
                "actions_caches": [
                    {
                        "key": "older",
                        "ref": "refs/heads/topic",
                        "created_at": "2026-01-01",
                    }
                ]
            },
            {
                "actions_caches": [
                    {
                        "key": "newer",
                        "ref": "refs/heads/main",
                        "created_at": "2026-01-02",
                    },
                    {
                        "key": "hidden",
                        "ref": "refs/heads/other",
                        "created_at": "2026-01-03",
                    },
                ]
            },
        ]
        with patch.object(
            planner, "command_output", side_effect=["main", json.dumps(pages)]
        ) as output:
            self.assertEqual(
                planner.fetch_caches("owner/repo", "refs/heads/topic"),
                (
                    ["newer", "older"],
                    [
                        {
                            "key": "hidden",
                            "ref": "refs/heads/other",
                            "created_at": "2026-01-03",
                        }
                    ],
                ),
            )
        self.assertIn("--paginate", output.call_args.args[0])
        self.assertIn("--slurp", output.call_args.args[0])

    def test_publish_cli_output_contract(self) -> None:
        """The publish CLI prints exactly the contracted name=value outputs."""
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = ["publish"]
        for name in (
            "patch_root",
            "workflow_root",
            "image",
            "repository_commit",
            "repository",
            "ref",
            "run",
        ):
            arguments.extend(
                ["--" + name.replace("_", "-"), str(getattr(self.args, name))]
            )
        with (
            patch.object(planner, "command_output", side_effect=self.output),
            patch.object(
                planner.package_sources, "resolve_sources", return_value=self.resolved
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(planner.main(arguments), 0)
        outputs = dict(line.split("=", 1) for line in stdout.getvalue().splitlines())
        self.assertEqual(
            set(outputs),
            {
                "patch-commit",
                "changed",
                "build-matrix",
                "build-extra-matrix",
                "restore-matrix",
                "verify-arches",
                "verify-plan",
            },
        )
        self.assertEqual(outputs["changed"], "true")
        self.assertEqual(outputs["patch-commit"], REVISION)
        self.assertEqual(len(json.loads(outputs["build-matrix"])["include"]), 3)
        self.assertEqual(len(json.loads(outputs["verify-plan"])), 5)
        self.assertIn("Publication inputs changed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
