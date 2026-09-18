"""Exercise repository assembly only against disposable inputs and fake GPG."""

import bz2
import gzip
import hashlib
import json
import lzma
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("build_repo.sh").resolve()
FAKE_TOOL = r"""
import hashlib
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["TOOL_LOG"], "a") as log:
    log.write(json.dumps([name, args]) + "\n")
failure = os.environ.get("FAIL_TOOL")
if (failure == name or (name == "gpg" and failure in args)
        or (failure == "release-find" and name == "find" and args[0] == "main")):
    print("fixture failure: " + str(failure), file=sys.stderr)
    sys.exit(23)
if name == "gpg":
    if "--list-secret-keys" in args:
        if not os.environ.get("NO_KEYS"):
            print("sec:-:2048:1:FAKEKEY:0:0:::::")
    else:
        release = sys.stdin.read()
        Path(args[args.index("--output") + 1]).write_text("FAKE SIGNATURE\n" + release)
elif name == "dpkg-scanpackages":
    arch = args[args.index("-a") + 1]
    for package in sorted(Path(args[-1]).glob("*.deb")):
        if package.stem.endswith(("_" + arch, "_all")):
            print(f"Package: {package.name}\nFilename: {package}\nArchitecture: {arch}\n")
elif name == "dpkg-scansources":
    for source in sorted(Path(args[0]).glob("*.dsc")):
        print(f"Package: {source.stem}\nDirectory: {source.parent}\n")
elif name in ("md5sum", "sha1sum", "sha256sum"):
    algorithm = name.removesuffix("sum")
    print(hashlib.new(algorithm, Path(args[0]).read_bytes()).hexdigest(), args[0])
elif name == "date":
    print("Fri, 18 Sep 2026 00:00:00 +0000")
else:
    os.execv(os.environ["REAL_" + name], [name, *args])
"""


class BuildRepoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="build-repo-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.inputs = self.root / "packages/rolling/nested input"
        self.inputs.mkdir(parents=True)
        self.site = self.root / "_site"
        (self.site / "packages").mkdir(parents=True)
        (self.site / "index.html").write_text("keep website")
        (self.site / "packages/stale.deb").write_text("Jekyll copy")
        self.artifacts = [
            "example_1_all.deb",
            "example_1_amd64.deb",
            "example_1_arm64.deb",
            "example_1.dsc",
            "example_1.orig.tar.gz",
            "example_1.debian.tar.xz",
            "example_1.tar.bz2",
            "example_1.changes",
        ]
        for name in self.artifacts:
            (self.inputs / name).write_text("fixture " + name)
        (self.inputs / "ignored.txt").write_text("keep input")
        self.env = os.environ.copy()
        for variable in (
            "GPG_KEY_ID",
            "GPG_FINGERPRINT",
            "ORIGIN",
            "FAIL_TOOL",
            "NO_KEYS",
        ):
            self.env.pop(variable, None)
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            TOOL_LOG=str(self.root / "tools.jsonl"),
            REPO_OWNER="Fixture owner",
            GPG_FINGERPRINT="FAKE-FINGERPRINT",
            GNUPGHOME=str(self.root / "unused-gnupg"),
        )
        tools = [
            "gpg",
            "dpkg-scanpackages",
            "dpkg-scansources",
            "md5sum",
            "sha1sum",
            "sha256sum",
            "date",
            "find",
            "mv",
            "gzip",
        ]
        for name in tools:
            if name in ("find", "mv", "gzip"):
                self.env["REAL_" + name] = shutil.which(name)
            tool = self.bin / name
            tool.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            tool.chmod(0o755)

    def run_build(self, **env):
        return subprocess.run(
            [str(SCRIPT)],
            check=False,
            cwd=self.root,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def calls(self):
        return [
            json.loads(line)
            for line in (self.root / "tools.jsonl").read_text().splitlines()
        ]

    def assert_failed(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("successfully", result.stdout)

    def test_success_layout_compression_hashes_and_signatures(self):
        result = self.run_build(GPG_KEY_ID="must-not-win")
        self.assertEqual(result.returncode, 0, result.stderr)
        repo = self.site / "deb"
        pool = repo / "pool/main"
        self.assertEqual(sorted(p.name for p in pool.iterdir()), sorted(self.artifacts))
        for name in self.artifacts:
            self.assertEqual((pool / name).read_text(), "fixture " + name)
            self.assertFalse((self.inputs / name).exists())
        self.assertTrue((self.inputs / "ignored.txt").exists())
        self.assertEqual((self.site / "index.html").read_text(), "keep website")
        self.assertFalse((self.site / "packages").exists())
        self.assertFalse((repo / ".artifacts").exists())
        dist = repo / "dists/rolling"
        indexes = [
            dist / f"main/binary-{arch}/Packages" for arch in ("all", "amd64", "arm64")
        ]
        indexes.append(dist / "main/source/Sources")
        for index in indexes:
            data = index.read_bytes()
            self.assertTrue(data)
            self.assertEqual(
                gzip.decompress(Path(str(index) + ".gz").read_bytes()), data
            )
            self.assertEqual(
                bz2.decompress(Path(str(index) + ".bz2").read_bytes()), data
            )
            if shutil.which("xz"):
                self.assertEqual(
                    lzma.decompress(Path(str(index) + ".xz").read_bytes()), data
                )
        release = (dist / "Release").read_text()
        self.assertIn("Label: Fixture owner\n", release)
        self.assertIn("Architectures: all amd64 arm64\nComponents: main\n", release)
        for section, algorithm in (
            ("MD5Sum", "md5"),
            ("SHA1", "sha1"),
            ("SHA256", "sha256"),
        ):
            entries = []
            for line in release.split(section + ":\n", 1)[1].splitlines():
                if not line.startswith(" "):
                    break
                entries.append(line)
            expected = sorted(p for p in (dist / "main").rglob("*") if p.is_file())
            self.assertEqual(len(entries), len(expected))
            for line, path in zip(entries, expected, strict=True):
                digest, size, filename = line.split()
                self.assertEqual(filename, path.relative_to(dist).as_posix())
                self.assertEqual(int(size), path.stat().st_size)
                self.assertEqual(
                    digest, hashlib.new(algorithm, path.read_bytes()).hexdigest()
                )
        for signature in ("Release.gpg", "InRelease"):
            self.assertEqual(
                (dist / signature).read_text(), "FAKE SIGNATURE\n" + release
            )
        signing = [
            args for name, args in self.calls() if name == "gpg" and "--output" in args
        ]
        self.assertEqual(len(signing), 2)
        for args in signing:
            self.assertIn("FAKE-FINGERPRINT", args)
            self.assertNotIn("must-not-win", args)
            self.assertEqual(
                args[:4], ["--batch", "--yes", "--pinentry-mode", "loopback"]
            )

    def test_owner_and_key_fallbacks(self):
        result = self.run_build(
            REPO_OWNER="",
            ORIGIN="Origin owner",
            GPG_FINGERPRINT="",
            GPG_KEY_ID="FALLBACK",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Label: Origin owner", (self.site / "deb/dists/rolling/Release").read_text()
        )
        self.assertTrue(
            all("FALLBACK" in args for name, args in self.calls() if name == "gpg")
        )

    def test_default_signing_key_and_optional_sources(self):
        for name in self.artifacts:
            if not name.endswith(".deb"):
                (self.inputs / name).unlink()
        result = self.run_build(GPG_FINGERPRINT="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.site / "deb/dists/rolling/main/source/Sources").read_bytes(), b""
        )
        self.assertFalse(
            any("--local-user" in args for name, args in self.calls() if name == "gpg")
        )

    def test_scanning_failures_are_fatal_and_not_signed(self):
        for scanner in ("dpkg-scanpackages", "dpkg-scansources"):
            with self.subTest(scanner=scanner):
                # Each scanner needs fresh inputs because assembly consumes them.
                for name in self.artifacts:
                    (self.inputs / name).write_text("fixture")
                result = self.run_build(FAIL_TOOL=scanner)
                self.assert_failed(result, "fixture failure: " + scanner)
                self.assertFalse(
                    any(
                        "--output" in args
                        for name, args in self.calls()
                        if name == "gpg"
                    )
                )
                self.assertFalse((self.site / "packages").exists())
                self.assertFalse((self.site / "deb/.artifacts").exists())

    def test_signing_failures_are_fatal(self):
        for operation in ("--detach-sign", "--clearsign"):
            with self.subTest(operation=operation):
                for name in self.artifacts:
                    (self.inputs / name).write_text("fixture")
                result = self.run_build(FAIL_TOOL=operation)
                self.assert_failed(result, "GPG signing failed")
                self.assertFalse((self.site / "deb/.artifacts").exists())

    def test_other_command_failures_stop_assembly(self):
        for tool in ("find", "mv", "gzip", "release-find", "sha256sum"):
            with self.subTest(tool=tool):
                for name in self.artifacts:
                    (self.inputs / name).write_text("fixture")
                result = self.run_build(FAIL_TOOL=tool)
                self.assert_failed(result, "fixture failure: " + tool)
                self.assertFalse(
                    any(
                        "--output" in args
                        for name, args in self.calls()
                        if name == "gpg"
                    )
                )

    def test_missing_owner_fails_before_moving(self):
        self.assert_failed(self.run_build(REPO_OWNER=""), "Set REPO_OWNER or ORIGIN")
        self.assertTrue((self.inputs / self.artifacts[0]).exists())

    def test_missing_site_fails_before_moving(self):
        shutil.rmtree(self.site)
        self.assert_failed(self.run_build(), "Missing Jekyll output")
        self.assertTrue((self.inputs / self.artifacts[0]).exists())

    def test_missing_input_directory(self):
        shutil.rmtree(self.root / "packages")
        self.assert_failed(self.run_build(), "Missing package input")

    def test_no_binary_packages_fails_before_moving_sources(self):
        for path in self.inputs.glob("*.deb"):
            path.unlink()
        self.assert_failed(self.run_build(), "No .deb packages found")
        self.assertTrue((self.inputs / "example_1.dsc").exists())

    def test_missing_secret_key_fails_before_moving(self):
        self.assert_failed(
            self.run_build(NO_KEYS="1"), "No GPG secret signing key found"
        )
        self.assertTrue((self.inputs / self.artifacts[0]).exists())


if __name__ == "__main__":
    unittest.main()
