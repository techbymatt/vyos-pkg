"""Contracts shared by the manual and publication workflows (stdlib only)."""

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

try:
    from .cache_namespace import job_text
except ImportError:
    from cache_namespace import job_text

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github/workflows"
ACTIONS = ROOT / ".github/actions"


class TestWorkflowTests(unittest.TestCase):
    def test_callers_share_build_implementations_and_choose_cache_policy(self):
        for caller, cached in (("test", "false"), ("publish", "true")):
            workflow = (WORKFLOWS / f"{caller}.yaml").read_text()
            for job, reusable in (("build", "recipe"), ("build-extra", "standalone")):
                with self.subTest(caller=caller, job=job):
                    text = job_text(workflow, job)
                    self.assertIn(
                        f"uses: ./.github/workflows/build-{reusable}.yaml", text
                    )
                    self.assertIn(f"cache: {cached}", text)
                    self.assertIn("image:", text)
                    self.assertIn("build-ref:", text)
                    self.assertNotIn("steps:", text)

    def test_planners_receive_raw_inputs_and_publication_context(self):
        test = job_text((WORKFLOWS / "test.yaml").read_text(), "setup-matrix")
        for env, name in (
            ("PACKAGES", "package"),
            ("EXTRA_PACKAGES", "package-extra"),
            ("DEPS", "deps"),
        ):
            self.assertIn(f"{env}: ${{{{ inputs.{name} }}}}", test)
        self.assertIn(
            'plan_builds.py test --packages "$PACKAGES" --extra-packages "$EXTRA_PACKAGES" --deps "$DEPS"',
            test,
        )
        self.assertNotIn("jq", test)
        publish = job_text((WORKFLOWS / "publish.yaml").read_text(), "cache-check")
        self.assertIn("working-directory: patch", publish)
        self.assertIn("plan_builds.py publish", publish)
        for flag in (
            "--patch-root",
            "--workflow-root",
            "--image",
            "--repository-commit",
            "--repository",
            "--ref",
            "--run",
        ):
            self.assertIn(flag, publish)
        self.assertIn("force+=(--force-rebuild)", publish)
        self.assertIn('"${force[@]}" >> "$GITHUB_OUTPUT"', publish)
        self.assertIn("workflow/input-manifest.json", publish)

    def test_native_builds_apply_policy_before_execution(self):
        for name in ("recipe", "standalone"):
            text = (WORKFLOWS / f"build-{name}.yaml").read_text()
            self.assertIn("runs-on: ${{ matrix.runner_label }}", text)
            self.assertIn("image: ${{ inputs.image }}", text)
            self.assertIn("if: ${{ inputs.cache }}", text)
            self.assertIn("steps.cache.outputs.cache-hit != 'true'", text)
            self.assertIn("path: ${{ steps.paths.outputs.cache }}", text)
            self.assertIn("uses: ./workflow/.github/actions/package-artifacts", text)
            build = (
                "python3 build.py" if name == "recipe" else "dpkg-buildpackage --build="
            )
            self.assertLess(text.index("prepare_package_build.py"), text.index(build))
            if name == "recipe":
                self.assertLess(
                    text.index("git apply --3way"),
                    text.index("prepare_package_build.py"),
                )
                self.assertIn("go-version:", text)
                self.assertIn("Pin-Priority: 1001", text)
                self.assertIn(
                    "cache: false", text
                )  # No implicit setup-go cache in Test.
            else:
                self.assertIn("then build_type=any", text)
                self.assertIn("--group build-extra --root packages", text)
                self.assertIn("matrix.commit || inputs.build-ref", text)

    def test_shared_output_policy_precedes_both_uploads(self):
        text = (ACTIONS / "package-artifacts/action.yaml").read_text()
        check = text.index("package_build_policy.py check")
        self.assertLess(check, text.index("name: Upload the .deb"))
        self.assertLess(check, text.index("name: Upload the remaining"))
        self.assertIn("if-no-files-found: error", text)
        self.assertIn("path: ${{ steps.paths.outputs.debs }}", text)
        self.assertIn("path: ${{ steps.paths.outputs.sources }}", text)
        restore = (ACTIONS / "restore-package/action.yaml").read_text()
        self.assertIn("uses: ./workflow/.github/actions/package-artifacts", restore)
        self.assertIn("path: ${{ steps.paths.outputs.cache }}", restore)
        self.assertIn("CACHE_PATHS: ${{ steps.paths.outputs.cache }}", restore)
        self.assertIn('>"$shim/zstd"', restore)
        self.assertIn("fail-on-cache-miss: true", restore)

    def test_path_contract_matches_legacy_cache_order(self):
        text = (ACTIONS / "package-paths/action.yaml").read_text()
        script = textwrap.dedent(text.split("      run: |\n", 1)[1])
        for group in ("build", "build-extra"):
            with self.subTest(group=group), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "output"
                subprocess.run(
                    ["bash", "-eu", "-c", script],
                    check=True,
                    env={
                        **os.environ,
                        "GROUP": group,
                        "PACKAGE": "linux-kernel",
                        "GITHUB_OUTPUT": str(output),
                    },
                )
                result = output.read_text()
                directory = (
                    "vyos-build/scripts/package-build/linux-kernel"
                    if group == "build"
                    else "packages"
                )
                metadata = (
                    "vyos-build/scripts/package-build/**"
                    if group == "build"
                    else "packages"
                )
                sources = "\n".join(
                    f"{metadata}/{suffix}"
                    for suffix in (
                        "*.buildinfo",
                        "*.changes",
                        "*.dsc",
                        "*debian.tar.*",
                        "*orig.tar.*",
                    )
                )
                self.assertIn(
                    f"cache<<PATHS\n{directory}/*.deb\n{sources}\nPATHS", result
                )

    def test_restore_keeps_eight_native_slots(self):
        text = job_text((WORKFLOWS / "publish.yaml").read_text(), "restore-cached")
        self.assertIn("runs-on: ${{ matrix.runner_label }}", text)
        for slot in range(8):
            self.assertIn(f"entry: ${{{{ toJSON(matrix.entries[{slot}]) }}}}", text)
        self.assertEqual(
            text.count("uses: ./workflow/.github/actions/restore-package"), 8
        )

    def test_combined_verification_preserves_producer_directories(self):
        for name in ("test", "publish"):
            text = job_text((WORKFLOWS / f"{name}.yaml").read_text(), "verify")
            self.assertIn("uses: ./.github/workflows/verify-packages.yaml", text)
            self.assertIn("!contains(needs.*.result, 'failure')", text)
            if name == "test":
                self.assertIn(
                    "architectures: ${{ needs.setup-matrix.outputs.verify-arches }}",
                    text,
                )
            else:
                self.assertIn('architectures: \'["amd64","arm64"]\'', text)
                self.assertIn("needs.cache-check.outputs.changed == 'true'", text)
        verify = (WORKFLOWS / "verify-packages.yaml").read_text()
        self.assertIn("runs-on: ubuntu-24.04", verify)
        self.assertNotIn("container:", verify)
        self.assertNotIn("matrix:", verify)
        self.assertIn("pattern: deb-*\n", verify)
        self.assertIn("merge-multiple: false", verify)
        self.assertIn(
            '--artifacts packages --expected-arches "$EXPECTED_ARCHES"', verify
        )
        self.assertIn("> lintian-report.txt 2>&1 || status=$?", verify)
        self.assertIn("continue-on-error: true", verify)

    def test_publication_requires_successful_verification(self):
        text = job_text((WORKFLOWS / "publish.yaml").read_text(), "publish")
        self.assertIn("needs.verify.result == 'success'", text)
        self.assertIn("      - verify\n", text)
        self.assertIn("_site/input-manifest.json", text)


if __name__ == "__main__":
    unittest.main()
