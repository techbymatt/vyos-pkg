"""Execution settings, compact restore matrices and catalog growth safeguards."""

import copy
import unittest

try:
    from . import build_matrix as matrix
    from .package_cache import CacheIndex
    from .test_plan_builds import cache_key, record
except ImportError:
    import build_matrix as matrix
    from package_cache import CacheIndex
    from test_plan_builds import cache_key, record


class BuildMatrixTests(unittest.TestCase):
    """Pure plans scale without changing cache identities or producer completeness."""

    def test_catalog_settings_reach_both_test_and_publish_builds(self):
        """New package settings require no package-specific workflow branches."""
        catalog = matrix.catalog.validate_catalog(
            {
                "build": [{"name": "new", "go": True, "timeout_minutes": 60}],
                "build-extra": [
                    {
                        "name": "extra",
                        "timeout_minutes": 45,
                        "architecture": "independent_only",
                    }
                ],
            }
        )
        test = matrix.plan_test("new", "extra", "libexample-dev", catalog)
        publish = matrix.plan_publish(
            [record("new"), record("extra", group="build-extra")],
            [],
            "123-1",
            sources=catalog,
        )
        for result in (test, publish):
            self.assertEqual(result["build-matrix"]["include"][0]["go"], True)
            self.assertEqual(
                result["build-matrix"]["include"][0]["timeout_minutes"], 60
            )
            self.assertEqual(
                result["build-extra-matrix"]["include"][0]["timeout_minutes"], 45
            )
        self.assertEqual(test["verify-arches"], ["amd64", "arm64"])
        self.assertEqual(len(test["build-extra-matrix"]["include"]), 1)

    def test_expensive_sources_queue_first_without_changing_manifest_order(self):
        """Build scheduling is separate from source identity/canonical publication order."""
        rows = [
            record(name, arch)
            for name in ("frr", "podman", "linux-kernel")
            for arch in ("amd64", "arm64")
        ]
        before = copy.deepcopy(rows)
        entries = matrix.plan_publish(rows, [], "123-1")["build-matrix"]["include"]
        self.assertEqual(
            [entry["package"] for entry in entries],
            ["linux-kernel"] * 2 + ["podman"] * 2 + ["frr"] * 2,
        )
        self.assertEqual(rows, before)

    def test_execution_policy_changes_leave_source_cache_keys_unchanged(self):
        """Go, priority and timeout are local build policy, not cloned source inputs."""
        row = record("new")
        old = matrix.catalog.validate_catalog(
            {"build": [{"name": "new"}], "build-extra": []}
        )
        new = matrix.catalog.validate_catalog(
            {
                "build": [
                    {"name": "new", "go": True, "priority": 100, "timeout_minutes": 60}
                ],
                "build-extra": [],
            }
        )
        for catalog in (old, new):
            result = matrix.plan_publish(
                [row], [cache_key(row)], "123-1", sources=catalog
            )
            self.assertEqual(result["build-matrix"]["include"], [])
            self.assertEqual(
                result["restore-matrix"]["include"][0]["entries"][0]["cache_key"],
                cache_key(row),
            )

    def test_restore_entries_contain_only_the_execution_contract(self):
        """Large nested repository descriptors are not repeated in restore job outputs."""
        row = record("linux-kernel")
        result = matrix.plan_publish([row], [cache_key(row)], "123-1")
        entry = result["restore-matrix"]["include"][0]["entries"][0]
        self.assertEqual(set(entry), {"group", "package", "arch", "cache_key"})
        self.assertEqual(result["verify-plan"], ["deb-linux-kernel-amd64"])
        self.assertEqual(result["verify-arches"], ["amd64"])

    def test_cache_index_preserves_newest_exact_source_prefix(self):
        """Similar package names/digests and unrelated keys cannot authorize reuse."""
        row = record("source-amd64")
        other = record("source-amd64-extra")
        index = CacheIndex(
            [
                cache_key(other),
                "not-" + cache_key(row),
                cache_key(row, "new"),
                cache_key(row, "old"),
            ]
        )
        self.assertEqual(index.source_hit(row), cache_key(row, "new"))
        self.assertIsNone(index.source_hit(record("missing")))

    def test_duplicate_producers_fail_before_uploading_colliding_artifacts(self):
        """The planned artifact set has a one-to-one relationship with producers."""
        with self.assertRaisesRegex(ValueError, "duplicate package producers"):
            matrix.plan_publish([record(), record()], [], "123-1")

    def test_large_catalogs_fail_with_an_actionable_matrix_limit(self):
        """Matrix expansion does not silently exceed the GitHub scheduling limit."""
        with self.assertRaisesRegex(ValueError, "256-job matrix limit"):
            matrix.plan_test(
                ",".join(f"source-{index}" for index in range(129)), "", ""
            )

    def test_unknown_runner_architectures_fail_closed(self):
        """Unsupported targets cannot silently fall through to the arm64 runner."""
        with self.assertRaisesRegex(ValueError, "architecture"):
            matrix.runner_label("riscv64")


if __name__ == "__main__":
    unittest.main()
