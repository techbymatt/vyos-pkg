"""Deterministic stdlib tests; run: python3 -m unittest discover -s scripts."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

try:
    from . import publish_manifest as pm
except ImportError:
    import publish_manifest as pm


class PublishManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packages = self.root / "packages.json"
        self.key = self.root / "public.asc"
        self.key.write_bytes(b"fixed public key bytes\r\n")
        self.current = self.root / "current.json"
        self.published = self.root / "published.json"
        self.revision = "a" * 40
        self.image = "ghcr.io/example/build@sha256:" + "b" * 64
        self.rows = [
            dict(
                group="build-extra",
                package="zeta",
                arch="arm64",
                commit="c" * 64,
                deps="bison flex",
            ),
            dict(
                group="build",
                package="node_exporter",
                arch="arm64",
                commit=self.revision,
                deps="",
            ),
            dict(
                group="build",
                package="node_exporter",
                arch="amd64",
                commit=self.revision,
                deps="",
            ),
        ]
        self.write_rows(self.rows)

    def write_rows(self, rows: list[dict]) -> None:
        self.rows = rows
        self.packages.write_text(json.dumps(rows), encoding="utf-8")

    def create(self) -> pm.Manifest:
        return pm.create_manifest(
            self.rows, self.revision, "d" * 64, self.image, self.key
        )

    def cli(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = pm.main(list(args))
        return status, stdout.getvalue(), stderr.getvalue()

    def compare(self) -> tuple[int, str, str]:
        return self.cli(
            "compare",
            "--current",
            str(self.current),
            "--published",
            str(self.published),
        )

    def assert_changed(self, changed: pm.Manifest) -> None:
        self.current.write_bytes(pm.canonical_bytes(self.create()))
        self.published.write_bytes(pm.canonical_bytes(changed))
        self.assertEqual(self.compare(), (1, "", ""))

    def assert_malformed(self, text: str) -> None:
        self.current.write_bytes(pm.canonical_bytes(self.create()))
        self.published.write_text(text, encoding="utf-8")
        status, stdout, stderr = self.compare()
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("publish_manifest:", stderr)

    def test_create_canonical_file(self) -> None:
        self.assertEqual(
            self.cli(
                "create",
                "--packages",
                str(self.packages),
                "--repository-commit",
                self.revision,
                "--patch-commit",
                "d" * 64,
                "--image",
                self.image,
                "--signing-key",
                str(self.key),
                "--output",
                str(self.current),
            ),
            (0, "", ""),
        )
        manifest = self.create()
        expected = (
            json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            + b"\n"
        )
        self.assertEqual(self.current.read_bytes(), expected)
        self.assertEqual(
            [pm.package_identity(p) for p in manifest["packages"]],
            [
                ("build", "node_exporter", "amd64"),
                ("build", "node_exporter", "arm64"),
                ("build-extra", "zeta", "arm64"),
            ],
        )
        self.assertEqual(
            manifest["signing_key_sha256"],
            hashlib.sha256(self.key.read_bytes()).hexdigest(),
        )

    def test_compare_ignores_json_format_and_package_order(self) -> None:
        manifest = self.create()
        self.current.write_bytes(pm.canonical_bytes(manifest))
        manifest["packages"].reverse()
        self.published.write_text(json.dumps(manifest, indent=4), encoding="utf-8")
        self.assertEqual(self.compare(), (0, "", ""))

    def test_changed_repository(self) -> None:
        changed = self.create()
        changed["repository_commit"] = "e" * 40
        self.assert_changed(changed)

    def test_changed_patch(self) -> None:
        changed = self.create()
        changed["patch_commit"] = "f" * 64
        self.assert_changed(changed)

    def test_changed_image(self) -> None:
        changed = self.create()
        changed["image"] = "other/image@sha256:" + "0" * 64
        self.assert_changed(changed)

    def test_changed_public_key(self) -> None:
        original = self.create()
        self.key.write_bytes(b"different public key bytes")
        self.assert_changed(original)

    def test_changed_package_dependencies(self) -> None:
        changed = self.create()
        changed["packages"][0]["deps"] = "flex"
        self.assert_changed(changed)

    def test_changed_package_revision(self) -> None:
        changed = self.create()
        changed["packages"][0]["commit"] = "2" * 40
        self.assert_changed(changed)

    def test_missing_manifest_field(self) -> None:
        malformed = dict(self.create())
        del malformed["image"]
        self.assert_malformed(json.dumps(malformed))

    def test_boolean_schema(self) -> None:
        self.assert_malformed(json.dumps(dict(self.create(), schema_version=True)))

    def test_duplicate_json_key(self) -> None:
        text = json.dumps(self.create()).replace(
            '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
        )
        self.assert_malformed(text)

    def test_duplicate_package(self) -> None:
        self.write_rows([self.rows[0], self.rows[0]])
        with self.assertRaisesRegex(ValueError, "duplicate package"):
            self.create()

    def test_empty_package_sha(self) -> None:
        self.rows[0]["commit"] = ""
        self.write_rows(self.rows)
        with self.assertRaisesRegex(ValueError, "invalid package commit"):
            self.create()

    def test_empty_source_records(self) -> None:
        self.write_rows([])
        with self.assertRaisesRegex(ValueError, "packages must be a non-empty array"):
            self.create()

    def test_empty_manifest_packages(self) -> None:
        malformed = self.create()
        malformed["packages"] = []
        self.assert_malformed(json.dumps(malformed))

    def test_empty_repository_revision(self) -> None:
        self.revision = ""
        with self.assertRaisesRegex(ValueError, "invalid repository_commit"):
            self.create()

    def test_empty_patch_revision(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid patch_commit"):
            pm.create_manifest(self.rows, self.revision, "", self.image, self.key)

    def test_unpinned_image(self) -> None:
        self.image = "image:rolling"
        with self.assertRaisesRegex(ValueError, "invalid image"):
            self.create()

    def test_missing_package_field(self) -> None:
        del self.rows[0]["deps"]
        with self.assertRaisesRegex(ValueError, "package must contain exactly"):
            self.create()

    def test_invalid_json(self) -> None:
        self.assert_malformed("{broken")

    def test_unreadable_current(self) -> None:
        self.published.write_bytes(pm.canonical_bytes(self.create()))
        status, stdout, stderr = self.compare()
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("publish_manifest:", stderr)

    def test_invalid_utf8_current(self) -> None:
        self.current.write_bytes(b"\xff")
        self.published.write_bytes(pm.canonical_bytes(self.create()))
        status, stdout, stderr = self.compare()
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("publish_manifest:", stderr)


if __name__ == "__main__":
    unittest.main()
