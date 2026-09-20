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
NAMESPACE = "b" * 64
IMAGE = "ghcr.io/example/build@sha256:" + "c" * 64


def record(name="frr", arch="amd64", group="build", deps=""):
    return {
        "group": group,
        "package": name,
        "arch": arch,
        "commit": REVISION,
        "deps": deps,
    }


def cache_key(row, run="122-1"):
    return f"cache-v2-{row['package']}-{row['arch']}-{REVISION}-{NAMESPACE}-{run}"


class TestPlannerTests(unittest.TestCase):
    def test_raw_inputs_and_explicit_deps(self) -> None:
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
            planner.plan_test("", "hvinfo", "")["build-extra-matrix"]["include"][0][
                "deps"
            ],
            "",
        )

    def test_empty_and_single_arch_plans(self) -> None:
        result = planner.plan_test(" ,\t", "", "")
        self.assertEqual(result["verify-arches"], [])
        self.assertEqual(result["build-matrix"], {"include": []})
        self.assertEqual(
            planner.plan_test("shim-signed", "vyos-live-build", "")["verify-arches"],
            ["amd64"],
        )

    def test_unsafe_inputs_and_cross_group_duplicates_rejected(self) -> None:
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
            set(outputs), {"build-matrix", "build-extra-matrix", "verify-arches"}
        )
        self.assertEqual(outputs["verify-arches"], ["amd64", "arm64"])


class PublishPlannerTests(unittest.TestCase):
    def test_misses_preserve_groups_dependencies_and_runners(self) -> None:
        rows = [record(), record("hvinfo", "arm64", "build-extra", "gnat gprbuild")]
        before = copy.deepcopy(rows)
        result = planner.plan_publish(rows, [], NAMESPACE, "123-1")
        self.assertEqual(result["restore-matrix"], {"include": []})
        self.assertEqual(
            result["build-matrix"]["include"],
            [
                {
                    "group": "build",
                    "package": "frr",
                    "arch": "amd64",
                    "commit": REVISION,
                    "runner_label": "ubuntu-26.04",
                    "cache_key": cache_key(rows[0], "123-1"),
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
        result = planner.plan_publish([row], keys, NAMESPACE, "123-1")
        self.assertEqual(
            result["restore-matrix"]["include"][0]["entries"][0]["cache_key"],
            cache_key(row, "new"),
        )
        self.assertEqual(result["build-matrix"]["include"], [])
        result = planner.plan_publish(
            [row], ["not-" + cache_key(row)], NAMESPACE, "123-1"
        )
        self.assertEqual(len(result["build-matrix"]["include"]), 1)

    def test_force_refresh_and_unchanged_inputs(self) -> None:
        row = record()
        for keys in ([], [cache_key(row)]):
            result = planner.plan_publish(
                [row], keys, NAMESPACE, "123-1", changed=False
            )
            self.assertTrue(
                all(matrix == {"include": []} for matrix in result.values())
            )
            result = planner.plan_publish(
                [row], keys, NAMESPACE, "123-1", changed=False, force_rebuild=True
            )
            self.assertEqual(
                result["build-matrix"]["include"][0]["cache_key"],
                cache_key(row, "123-1"),
            )
            self.assertEqual(result["restore-matrix"], {"include": []})

    def test_restore_batches_eight_without_mixing_runners(self) -> None:
        rows = [
            record(f"source-{index}", arch)
            for index in range(9)
            for arch in ("amd64", "arm64")
        ]
        keys = [cache_key(row) for row in rows]
        batches = planner.plan_publish(rows, keys, NAMESPACE, "123-1")[
            "restore-matrix"
        ]["include"]
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

    def test_source_records_cover_catalog_policy(self) -> None:
        sources = planner.catalog.load_catalog()
        revisions = {
            (group, entry["name"]): REVISION
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
        revisions["build", "frr"] = ""
        with self.assertRaisesRegex(ValueError, "revision"):
            planner.source_records(sources, revisions)


class PublishBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
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

    def output(self, command, cwd=None):
        self.calls.append((command, cwd))
        if command[0] == "git":
            if command[1] == "ls-tree":
                return "100644 blob abc\t scripts/package-build/build.py\n040000 tree def\tscripts/package-build/frr\n100644 blob ghi\tscripts/package-build/common.py"
            if command[1] == "ls-remote":
                return REVISION + "\trefs/heads/rolling"
            return REVISION
        if command[:2] == ["gh", "api"]:
            if "--paginate" in command:
                return json.dumps([{"actions_caches": []}, {"actions_caches": []}])
            if command[2].endswith("/pages"):
                return "https://example.org/repo/"
            return "main"
        if command[0] == "curl":
            if self.published is None:
                raise subprocess.CalledProcessError(22, command)
            return self.published
        raise AssertionError(command)

    def run_publish(self):
        with (
            patch.object(planner, "command_output", side_effect=self.output),
            patch.object(
                planner.cache_namespace, "namespace", return_value=NAMESPACE
            ) as namespace,
            redirect_stderr(io.StringIO()),
        ):
            result = planner.run_publish(self.args)
        return result, namespace

    def test_publish_revision_namespace_and_download_boundaries(self) -> None:
        result, namespace = self.run_publish()
        self.assertTrue(result["changed"])
        self.assertEqual(result["patch-commit"], REVISION)
        self.assertEqual(len(result["build-matrix"]["include"]), 3)
        self.assertEqual(len(result["build-extra-matrix"]["include"]), 2)
        namespace.assert_called_once_with(
            IMAGE,
            REVISION,
            "100644 blob abc\t scripts/package-build/build.py\n100644 blob ghi\tscripts/package-build/common.py",
            REVISION,
            self.root,
        )
        self.assertIn(
            (
                [
                    "git",
                    "log",
                    "-n",
                    "1",
                    "--format=%H",
                    "--",
                    "data/defaults.toml",
                    "scripts/package-build/linux-kernel/",
                ],
                self.args.patch_root / "vyos-build",
            ),
            self.calls,
        )
        self.assertIn(
            (
                [
                    "git",
                    "log",
                    "-n",
                    "1",
                    "--format=%H",
                    "--",
                    "scripts/package-build/pyhumps/",
                ],
                self.args.patch_root / "vyos-build",
            ),
            self.calls,
        )
        self.assertIn(
            (
                [
                    "git",
                    "ls-remote",
                    "https://github.com/vyos/hvinfo.git",
                    "refs/heads/rolling",
                ],
                self.args.patch_root,
            ),
            self.calls,
        )
        curl = next(command for command, _ in self.calls if command[0] == "curl")
        self.assertEqual(
            curl[-1], "https://example.org/repo/input-manifest.json?run=123-1"
        )
        self.assertIn("Cache-Control: no-cache", curl)
        saved = planner.manifest.load_manifest(self.root / "input-manifest.json")
        self.assertEqual(len(saved["packages"]), 5)
        self.assertTrue(all("cache_key" not in row for row in saved["packages"]))

    def test_only_deployed_manifest_suppresses_publication(self) -> None:
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
        self.args.force_rebuild = True
        self.calls.clear()
        self.assertTrue(self.run_publish()[0]["changed"])
        self.assertFalse(any(command[0] == "curl" for command, _ in self.calls))

    def test_invalid_deployed_manifest_republishes(self) -> None:
        for text in ("{broken", "{}", '{"schema_version":1,"schema_version":1}'):
            self.published = text
            self.assertTrue(self.run_publish()[0]["changed"])

    def test_missing_remote_revision_fails(self) -> None:
        sources = planner.catalog.validate_catalog(
            {"build": [], "build-extra": [{"name": "hvinfo"}]}
        )
        with (
            patch.object(planner, "git", return_value=""),
            self.assertRaisesRegex(ValueError, "rolling revision"),
        ):
            planner.resolve_revisions(self.args.patch_root, sources)

    def test_cache_api_failure_is_not_treated_as_a_miss(self) -> None:
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
                ["newer", "older"],
            )
        self.assertIn("--paginate", output.call_args.args[0])
        self.assertIn("--slurp", output.call_args.args[0])

    def test_publish_cli_output_contract(self) -> None:
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
            patch.object(planner.cache_namespace, "namespace", return_value=NAMESPACE),
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
            },
        )
        self.assertEqual(outputs["changed"], "true")
        self.assertEqual(outputs["patch-commit"], REVISION)
        self.assertEqual(len(json.loads(outputs["build-matrix"])["include"]), 3)
        self.assertIn("Publication inputs changed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
