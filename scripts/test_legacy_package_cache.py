"""Producer-proven cache migration, including real historical recipe trees."""

import copy
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

try:
    from . import legacy_package_cache as legacy
    from . import package_sources as sources
    from .test_package_sources import COMMIT, OTHER_COMMIT, URL, RecipeFixture
except ImportError:
    import legacy_package_cache as legacy
    import package_sources as sources
    from test_package_sources import COMMIT, OTHER_COMMIT, URL, RecipeFixture

IMAGE = "ghcr.io/example/build@sha256:" + "c" * 64


def archive(value, filename="input-manifest.json"):
    """Create a producer artifact entirely in memory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        output.writestr(filename, json.dumps(value))
    return buffer.getvalue()


def job(row, identifier=17):
    """Build metadata for an original successful reusable-workflow producer."""
    return {
        "id": identifier,
        "name": f"Build / build ({row['group']}, {row['package']}, {row['arch']}, {row['commit']}, runner)",
        "conclusion": "success",
        "steps": [
            {
                "name": "Build the package",
                "conclusion": "success",
                "started_at": "2026-10-01T00:10:00Z",
                "completed_at": "2026-10-01T00:20:00Z",
            }
        ],
    }


def checkout_log(patch_commit, name="frr", commit=COMMIT):
    """Include distracting workflow clones outside the actual build step."""
    return (
        f"2026-10-01T00:01:00Z ref: {patch_commit}\n"
        "2026-10-01T00:02:00Z Cloning into '/workspace/vyos-1x'...\n"
        f"2026-10-01T00:02:01Z HEAD is now at {COMMIT[:8]} unrelated checkout\n"
        f"2026-10-01T00:10:01.123456Z Cloning into '{name}'...\n"
        f"2026-10-01T00:10:02Z HEAD is now at {commit[:8]} built source\n"
    )


class CheckoutEvidenceTests(unittest.TestCase):
    """Only source checkouts in a successful build step count as evidence."""

    def test_workflow_submodule_sha_cannot_prove_a_moving_source_branch(self):
        """A missing rolling SHA cannot be inferred from the earlier submodule pin."""
        row = {
            "group": "build",
            "package": "vyos-1x",
            "arch": "amd64",
            "commit": COMMIT,
        }
        log = checkout_log(COMMIT, "vyos-1x").replace(
            f"2026-10-01T00:10:02Z HEAD is now at {COMMIT[:8]} built source",
            "2026-10-01T00:10:02Z Already on 'rolling'",
        )
        evidence = legacy.checkout_evidence(
            log, job(row), "vyos-1x", [{"name": "vyos-1x"}]
        )
        self.assertEqual(evidence["commits"], {})

    def test_multiple_sources_and_nested_tag_clone_are_associated_correctly(self):
        """The Kea packaging tag has distinct evidence from the main source."""
        row = {
            "group": "build",
            "package": "isc-kea",
            "arch": "amd64",
            "commit": COMMIT,
        }
        log = checkout_log(COMMIT, "isc-kea") + (
            "2026-10-01T00:11:00Z Cloning into 'kea-packaging'...\n"
            f"2026-10-01T00:11:01Z Note: switching to '{OTHER_COMMIT}'.\n"
        )
        evidence = legacy.checkout_evidence(
            log, job(row), "isc-kea", [{"name": "isc-kea"}, {"name": "kea-packaging"}]
        )
        self.assertEqual(
            evidence["commits"], {"isc-kea": COMMIT[:8], "kea-packaging": OTHER_COMMIT}
        )

    def test_kernel_driver_directory_names_and_duplicate_heads(self):
        """Audited clone-directory aliases work, but conflicting HEADs fail proof."""
        row = {
            "group": "build",
            "package": "linux-kernel",
            "arch": "arm64",
            "commit": COMMIT,
        }
        log = checkout_log(COMMIT, "ethernet-linux-igb")
        evidence = legacy.checkout_evidence(
            log, job(row), "linux-kernel", [{"name": "igb"}]
        )
        self.assertEqual(evidence["commits"], {"igb": COMMIT[:8]})
        log += f"2026-10-01T00:12:00Z HEAD is now at {OTHER_COMMIT[:8]} conflicting reset\n"
        with self.assertRaisesRegex(ValueError, "ambiguous producer checkout"):
            legacy.checkout_evidence(log, job(row), "linux-kernel", [{"name": "igb"}])

    def test_skipped_build_or_ambiguous_patch_checkout_cannot_prove_inputs(self):
        """Incomplete producer metadata never becomes a proven cache hit."""
        row = {"group": "build", "package": "frr", "arch": "amd64", "commit": COMMIT}
        metadata = job(row)
        metadata["steps"][0]["conclusion"] = "skipped"
        with self.assertRaises(ValueError):
            legacy.checkout_evidence(
                checkout_log(COMMIT), metadata, "frr", [{"name": "frr"}]
            )
        log = checkout_log(COMMIT) + f"2026-10-01T00:03:00Z ref: {OTHER_COMMIT}\n"
        with self.assertRaisesRegex(ValueError, "patch checkout"):
            legacy.checkout_evidence(log, job(row), "frr", [{"name": "frr"}])


class LegacyVerifierTests(unittest.TestCase):
    """End-to-end proof lookups against fixture Git trees and mocked GitHub reads."""

    def setUp(self):
        """Set up a real pinned recipe and its original schema-v1 producer inputs."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = RecipeFixture(Path(temporary.name))
        self.reader = sources.RecipeReader(self.fixture.patch_root)
        self.resolver = sources.Resolver()
        self.addCleanup(self.resolver.close)
        self.verifier = legacy.LegacyVerifier("owner/repo", self.reader, self.resolver)
        self.row = self.current_record("frr")
        old = {
            key: self.row[key] for key in ("group", "package", "arch", "commit", "deps")
        }
        self.producer = {
            "schema_version": 1,
            "repository_commit": "d" * 40,
            "patch_commit": self.fixture.patch_commit,
            "image": IMAGE,
            "signing_key_sha256": "e" * 64,
            "packages": [old],
        }
        self.producer_jobs = [job(old)]
        self.log = checkout_log(self.fixture.patch_commit)
        self.real_command = sources.command_output
        self.real_check_output = subprocess.check_output
        self.api = self.enterContext(
            patch.object(self.verifier, "api", side_effect=self.api_output)
        )
        self.download = self.enterContext(
            patch.object(
                legacy.subprocess,
                "check_output",
                side_effect=self.download_output,
            )
        )
        self.commands = self.enterContext(
            patch.object(sources, "command_output", side_effect=self.command_output)
        )

    def current_record(self, name, group="build", commit=COMMIT):
        """Build a current source record from real recipe trees."""
        if group == "build":
            source = self.reader.recipe(name, self.fixture.patch_commit)
            revision = self.reader.legacy_revision(name, self.fixture.patch_commit)
            for entry in source["repositories"]:
                entry["commit"] = commit
        else:
            revision = commit
            source = {
                "recipe_tree": None,
                "inputs": {},
                "repositories": [
                    {
                        "name": name,
                        "url": f"https://github.com/vyos/{name}.git",
                        "ref": "rolling",
                        "commit": commit,
                    }
                ],
            }
        return {
            "group": group,
            "package": name,
            "arch": "amd64",
            "commit": revision,
            "deps": "",
            "source": source,
            "source_digest": sources.fingerprint(source),
        }

    def key(self, row=None, namespace="b", run="122"):
        """Build an original namespaced key with producer run/attempt identity."""
        row = row or self.producer["packages"][0]
        return f"cache-v2-{row['package']}-{row['arch']}-{row['commit']}-{namespace * 64}-{run}-1"

    def api_output(self, endpoint, *, paginate=False):
        """Return every producer metadata page, including unrelated entries."""
        self.assertTrue(paginate)
        if "/artifacts?" in endpoint:
            return [
                {"artifacts": [{"name": "unrelated", "expired": False, "id": 1}]},
                {"artifacts": [{"name": "input-manifest", "expired": False, "id": 2}]},
            ]
        if "/attempts/1/jobs?" in endpoint:
            return [{"jobs": []}, {"jobs": self.producer_jobs}]
        raise AssertionError(endpoint)

    def download_output(self, arguments, **kwargs):
        """Mock binary GitHub downloads without intercepting fixture Git commands."""
        if arguments[:2] == ["gh", "api"]:
            return archive(self.producer)
        return self.real_check_output(arguments, **kwargs)

    def command_output(self, arguments, cwd=None, **kwargs):
        """Mock only producer logs, preserving actual Git tree reconstruction."""
        if arguments[:2] == ["gh", "api"]:
            self.assertTrue(arguments[2].endswith("/logs"))
            return self.log
        return self.real_command(arguments, cwd, **kwargs)

    def test_proven_source_match_ignores_namespace_and_memoizes_producer_reads(self):
        """Global namespace changes can restore only independently proven sources."""
        self.assertTrue(self.verifier.verify(self.row, self.key()))
        self.assertTrue(self.verifier.verify(self.row, self.key(namespace="c")))
        self.assertEqual(self.api.call_count, 2)
        self.download.assert_called_once()
        logs = [
            call
            for call in self.commands.call_args_list
            if call.args[0][:2] == ["gh", "api"]
        ]
        self.assertEqual(len(logs), 1)

    def test_source_key_hits_skip_all_legacy_proof_requests(self):
        """After migration, normal restoration requires no historical log lookups."""
        current = sources.cache_prefix(self.row) + "123-1"
        self.assertEqual(self.verifier.matches([self.row], [current, self.key()]), {})
        self.api.assert_not_called()

    def test_standalone_cache_is_proven_by_its_recorded_external_commit(self):
        """Standalone caches need no inference about recipe-cloned branches."""
        self.row = self.current_record("hvinfo", "build-extra")
        self.producer["packages"] = [
            {
                key: self.row[key]
                for key in ("group", "package", "arch", "commit", "deps")
            }
        ]
        self.producer_jobs = [job(self.producer["packages"][0])]
        self.assertTrue(self.verifier.verify(self.row, self.key()))
        self.assertFalse(
            any(
                call.args[0][:2] == ["gh", "api"]
                for call in self.commands.call_args_list
            )
        )
        self.row["commit"] = OTHER_COMMIT
        self.assertFalse(self.verifier.verify(self.row, self.key()))

    def test_recipe_or_scoped_patch_change_is_not_a_legacy_hit(self):
        """Same declared Git commit does not conceal a recipe patch change."""
        path = "scripts/package-build/frr/package.toml"
        self.fixture.add_patch(
            path, (self.fixture.upstream / path).read_text() + "# new patch\n"
        )
        row = self.current_record("frr")
        self.assertFalse(self.verifier.verify(row, self.key()))
        self.assertIn(
            "scoped source inputs changed",
            self.verifier.reasons[("build", "frr", "amd64")],
        )

    def test_unrelated_shared_patch_can_still_prove_the_same_package_source(self):
        """Historical and current parent commits can differ without cache churn."""
        path = "scripts/package-build/build.py"
        self.fixture.add_patch(
            path, (self.fixture.upstream / path).read_text() + "# shared update\n"
        )
        self.assertTrue(self.verifier.verify(self.current_record("frr"), self.key()))

    def test_unrecorded_source_revision_is_unverifiable_even_if_a_ref_is_pinned(self):
        """A matching recipe revision or tag label is not checkout evidence."""
        self.log = self.log.replace(
            f"HEAD is now at {COMMIT[:8]} built source", "Already on 'rolling'"
        )
        self.assertFalse(self.verifier.verify(self.row, self.key()))
        self.assertIn(
            "did not record", self.verifier.reasons[("build", "frr", "amd64")]
        )

    def test_source_tag_checkout_is_expanded_and_compared_with_the_current_commit(self):
        """A tag's actual producer commit must match, not just its tag name."""
        path = "scripts/package-build/frr/package.toml"
        self.fixture.update_upstream(
            path, (self.fixture.upstream / path).read_text().replace(COMMIT, "v1")
        )
        self.row = self.current_record("frr")
        self.producer["patch_commit"] = self.fixture.patch_commit
        old = {
            key: self.row[key] for key in ("group", "package", "arch", "commit", "deps")
        }
        self.producer["packages"] = [old]
        self.producer_jobs = [job(old)]
        self.log = checkout_log(self.fixture.patch_commit)
        with patch.object(
            self.resolver, "resolve_evidence", return_value=COMMIT
        ) as resolve:
            self.assertTrue(self.verifier.verify(self.row, self.key()))
            resolve.assert_called_once_with(URL, COMMIT[:8], COMMIT)
        changed = copy.deepcopy(self.row)
        changed["source"]["repositories"][0]["commit"] = OTHER_COMMIT
        changed["source_digest"] = sources.fingerprint(changed["source"])
        with patch.object(
            self.resolver,
            "resolve_evidence",
            side_effect=ValueError("source revision changed"),
        ):
            self.assertFalse(self.verifier.verify(changed, self.key()))

    def test_mismatched_producer_key_patch_pin_and_unsuccessful_job_are_rejected(self):
        """All parts of the original producer identity must be consistent."""
        self.assertFalse(
            self.verifier.verify(
                self.row,
                self.key(dict(self.producer["packages"][0], commit=OTHER_COMMIT)),
            )
        )
        self.log = checkout_log(OTHER_COMMIT)
        self.assertFalse(self.verifier.verify(self.row, self.key()))
        self.verifier.evidence.clear()
        self.log = checkout_log(self.fixture.patch_commit)
        self.producer_jobs[0]["conclusion"] = "failure"
        self.assertFalse(self.verifier.verify(self.row, self.key()))

    def test_invalid_or_missing_artifact_rebuilds_only_unverifiable_entries(self):
        """Missing producer artifacts are incomplete proof, never blanket hits."""
        self.download.side_effect = lambda *args, **kwargs: b"not a zip"
        self.assertFalse(self.verifier.verify(self.row, self.key()))
        self.assertFalse(self.verifier.verify(self.row, self.key(namespace="c")))
        self.download.assert_called_once()

    def test_infrastructure_errors_do_not_become_mass_rebuilds(self):
        """A GitHub outage or timeout fails planning instead of bypassing caches."""
        for error in (
            subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 503"),
            subprocess.TimeoutExpired(["gh", "api"], 60),
        ):
            with self.subTest(error=error):
                self.verifier.manifests.clear()
                self.api.side_effect = error
                with self.assertRaises(subprocess.SubprocessError):
                    self.verifier.verify(self.row, self.key())

    def test_proof_budget_and_404_missing_logs_are_entry_specific_misses(self):
        """Unavailable historical evidence has an explicit conservative fallback."""
        self.verifier.deadline = 0
        self.assertFalse(self.verifier.verify(self.row, self.key()))
        self.api.assert_not_called()
        self.verifier.deadline = float("inf")
        self.api.side_effect = subprocess.CalledProcessError(
            1, ["gh", "api"], stderr="HTTP 404"
        )
        self.assertFalse(self.verifier.verify(self.row, self.key()))


class ProducerArtifactTests(unittest.TestCase):
    """Artifact parsing is bounded and never extracts arbitrary archive members."""

    def test_wrong_or_multiple_members_are_rejected(self):
        """An unexpected member cannot substitute for recorded producer inputs."""
        with self.assertRaises(ValueError):
            legacy.archive_manifest(archive({}, "../input-manifest.json"))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            output.writestr("input-manifest.json", "{}")
            output.writestr("extra", "data")
        with self.assertRaises(ValueError):
            legacy.archive_manifest(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
