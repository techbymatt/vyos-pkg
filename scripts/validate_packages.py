#!/usr/bin/env python3
"""Statically validate .deb metadata, archives, optional md5sums and identities.

Requires dpkg and dpkg-deb, but never installs packages or runs their scripts.
Archives are spooled to temporary files and regular members are streamed, not
extracted: absolute symlinks are inert data, never host filesystem references.
This is not a dependency, signature, installability or runtime check. Inputs must
remain unchanged during validation; temporary disk use scales with archive size.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO

FIELDS = ("Package", "Version", "Architecture", "Maintainer", "Description")


def run_tool(command: list[str], output: BinaryIO | int = subprocess.PIPE) -> bytes:
    result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"{command[0]} {command[1]} failed: {detail}")
    return result.stdout or b""


def safe_path(name: str) -> str:
    if name.startswith("/") or ".." in name.split("/") or "\x00" in name:
        raise ValueError(f"unsafe archive path: {name!r}")
    return "/".join(part for part in name.split("/") if part not in ("", "."))


def read_archive(path: Path, option: str) -> tuple[dict[str, str], bytes | None]:
    """Read all regular bytes; resolve hardlinks only in the archive namespace."""
    digests: dict[str, str] = {}
    members: dict[str, bytes] = {}
    hardlinks: dict[str, str] = {}
    sums = None
    with tempfile.TemporaryFile() as stream:
        run_tool(["dpkg-deb", option, str(path)], stream)
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            for member in archive:
                name = safe_path(member.name)
                if not name and not member.isdir():
                    raise ValueError("empty archive member path")
                if name in members:
                    raise ValueError(f"duplicate archive path: {name}")
                members[name] = member.type
                if member.isreg():
                    source = archive.extractfile(member)
                    assert source is not None
                    with source:
                        digest = hashlib.md5(usedforsecurity=False)
                        chunks = []
                        for chunk in iter(
                            lambda source=source: source.read(1024 * 1024), b""
                        ):
                            digest.update(chunk)
                            if option == "--ctrl-tarfile" and name == "md5sums":
                                chunks.append(chunk)
                        digests[name] = digest.hexdigest()
                        if option == "--ctrl-tarfile" and name == "md5sums":
                            sums = b"".join(chunks)
                elif member.islnk():
                    hardlinks[name] = safe_path(member.linkname)
                elif not (member.isdir() or member.issym()):
                    raise ValueError(f"unsupported archive member type: {name}")
            # tarfile stops at the first zero header. Check the complete tail,
            # including the second end marker, instead of ignoring corrupt data.
            stream.seek(archive.offset)
            tail_size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                tail_size += len(chunk)
                if chunk.strip(b"\x00"):
                    raise ValueError("nonzero data after archive end")
            if tail_size < 1024 or tail_size % 512:
                raise ValueError("truncated archive end markers")
    for name in members:
        parts = name.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in members and members[parent] != tarfile.DIRTYPE:
                raise ValueError(f"non-directory archive parent: {parent}")
    for name, target in hardlinks.items():
        visited = {name}
        while target in hardlinks and target not in visited:
            visited.add(target)
            target = hardlinks[target]
        if target not in digests:
            raise ValueError(f"invalid hardlink target: {name}")
        digests[name] = digests[target]
    if option == "--ctrl-tarfile" and "md5sums" in members and sums is None:
        raise ValueError("md5sums must be a regular control file")
    return digests, sums


def verify_checksums(sums: bytes, digests: dict[str, str]) -> None:
    seen = set()
    for line in sums.decode("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{32}) [ *](.+)", line)
        if match is None:
            raise ValueError("malformed md5sums entry")
        expected, raw_name = match.groups()
        name = safe_path(raw_name)
        if not name or name in seen:
            raise ValueError(f"invalid or duplicate md5sums path: {name}")
        seen.add(name)
        if name not in digests:
            raise ValueError(f"md5sums missing regular file: {name}")
        if digests[name] != expected.lower():
            raise ValueError(f"md5sums checksum mismatch: {name}")


def validate_packages(paths: list[Path], arch: str) -> None:
    if arch not in ("amd64", "arm64") or not paths:
        raise ValueError("expected amd64 or arm64 and at least one .deb path")
    _validate_packages([(path, arch) for path in paths])


def validate_artifacts(directory: Path) -> None:
    """Validate unmerged deb-* downloads from both architecture build jobs."""
    groups: dict[str, list[Path]] = {"amd64": [], "arm64": []}
    try:
        for child in sorted(directory.iterdir()):
            if not child.is_dir():
                continue
            match = re.fullmatch(r"deb-.*-(amd64|arm64)", child.name)
            if match is None:
                raise ValueError(f"unknown artifact directory name: {child.name}")
            groups[match[1]].extend(sorted(child.rglob("*.deb")))
    except OSError as error:
        raise ValueError(f"{directory}: {error}") from error
    for arch, paths in groups.items():
        if not paths:
            raise ValueError(f"empty or missing artifact architecture group: {arch}")
    _validate_packages(
        [(path, arch) for arch, paths in groups.items() for path in paths]
    )


def _validate_packages(packages: list[tuple[Path, str]]) -> None:
    seen: dict[tuple[str, str, str], str] = {}
    for original, arch in packages:
        try:
            path = original.resolve()
            if path.suffix != ".deb" or not path.is_file():
                raise ValueError("expected a regular .deb file")
            with path.open("rb") as source:
                sha256 = hashlib.file_digest(source, "sha256").hexdigest()
            metadata = {}
            for field in FIELDS:
                value = run_tool(["dpkg-deb", "--field", str(path), field])
                metadata[field] = value.decode("utf-8").strip()
                if not metadata[field]:
                    raise ValueError(f"missing required metadata: {field}")
                if field != "Description" and "\n" in metadata[field]:
                    raise ValueError(f"multiline metadata: {field}")
            name, version, actual_arch = (metadata[field] for field in FIELDS[:3])
            if re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", name) is None:
                raise ValueError(f"invalid Package name: {name}")
            run_tool(["dpkg", "--validate-version", version])
            if actual_arch not in (arch, "all"):
                raise ValueError(
                    f"unexpected Architecture: {actual_arch} (wanted {arch} or all)"
                )
            _, sums = read_archive(path, "--ctrl-tarfile")
            digests, _ = read_archive(path, "--fsys-tarfile")
            if sums is not None:
                verify_checksums(sums, digests)
            identity = (name, version, actual_arch)
            if identity in seen and seen[identity] != sha256:
                raise ValueError(
                    f"differing SHA256 bytes for package identity {identity}"
                )
            seen[identity] = sha256
        except (OSError, ValueError, tarfile.TarError) as error:
            raise ValueError(f"{original}: {error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--arch", choices=("amd64", "arm64"))
    mode.add_argument("--artifacts", type=Path, metavar="DIRECTORY")
    parser.add_argument("packages", type=Path, nargs="*")
    args = parser.parse_args(argv)
    if args.artifacts is not None and args.packages:
        parser.error("--artifacts cannot be combined with package paths")
    if args.arch is not None and not args.packages:
        parser.error("--arch requires at least one package path")
    try:
        if args.artifacts is not None:
            validate_artifacts(args.artifacts)
        else:
            validate_packages(args.packages, args.arch)
    except ValueError as error:
        print(f"validate_packages: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
