"""Apply audited build-mode adaptations after the downstream VyOS patches.

These exact, checked replacements deliberately fail when an upstream command
changes. They do not rewrite arbitrary shell commands or intercept dpkg globally.
Custom arch-only builders (including the kernel) keep their own build targets.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from .checked_edits import CheckedEdits, replace_once
    from .package_catalog import ARCHITECTURES, GROUPS, validate_name
except ImportError:
    from checked_edits import CheckedEdits, replace_once
    from package_catalog import ARCHITECTURES, GROUPS, validate_name

__all__ = ["prepare", "prepare_extra", "prepare_udp_packaging", "replace_once"]


@dataclass(frozen=True)
class BinaryCommand:
    """An audited command and its exact occurrence count within one recipe file."""

    command: str
    path: str = "package.toml"
    count: int = 1


# A new overridden Debian build usually needs just one entry here, not another
# branch in preparation or workflow YAML. More complex tools use the hook below.
BINARY_COMMANDS = {
    "dropbear": BinaryCommand("dpkg-buildpackage -us -uc -tc -b"),
    "frr": BinaryCommand(
        "dpkg-buildpackage -us -uc -tc -b -Ppkg.frr.rtrlib,pkg.frr.lua"
    ),
    "hostap": BinaryCommand(
        "dpkg-buildpackage -us -uc -tc -b -Ppkg.wpa.nogui,noudeb", "build.sh"
    ),
    "net-snmp": BinaryCommand("dpkg-buildpackage -us -uc -tc -b || true"),
    "netfilter": BinaryCommand("dpkg-buildpackage -uc -us -tc -b"),
    "openssl": BinaryCommand("dpkg-buildpackage -us -uc -tc -b"),
    "openvpn": BinaryCommand("dpkg-buildpackage -uc -us -tc -b"),
    "strongswan": BinaryCommand("dpkg-buildpackage -uc -us -tc -b -d"),
    "tacacs": BinaryCommand("dpkg-buildpackage -us -uc -tc -b", count=3),
    "udp-broadcast-relay": BinaryCommand("dpkg-buildpackage -uc -us -tc -b -d"),
    "xen-guest-agent": BinaryCommand("dpkg-buildpackage -b -us -uc"),
}


def prepare(root: Path, package: str, arch: str) -> None:
    """Validate all recipe adaptations, then update its files/shared builder."""
    validate_name(package)
    if arch not in ARCHITECTURES:
        raise ValueError(f"invalid build architecture: {arch}")
    edits = CheckedEdits()
    # The default builder includes source packages; retain that behavior.
    mode = "full" if arch == "amd64" else "source,any"
    edits.replace(
        root / "build.py",
        "dpkg-buildpackage -uc -us -tc -F --source-option",
        f"dpkg-buildpackage -uc -us -tc --build={mode} --source-option",
    )
    binary = "binary" if arch == "amd64" else "any"
    specification = BINARY_COMMANDS.get(package)
    if specification is not None:
        edits.replace(
            root / package / specification.path,
            specification.command,
            specification.command.replace(" -b", f" --build={binary}"),
            count=specification.count,
        )
    hook = RECIPE_HOOKS.get(package)
    if hook is not None:
        hook(root / package, edits, arch, binary)
    edits.commit()


def prepare_frr(directory: Path, edits: CheckedEdits, arch: str, binary: str) -> None:
    """Render apkg's libyang source, then build it with the native Debian mode."""
    edits.replace(
        directory / "package.toml",
        "pipx run apkg build -i && find pkg/pkgs -type f -name *.deb -exec mv -t .. {} +",
        "pipx run apkg build-dep && "
        "pipx run apkg srcpkg --result-dir apkg-source && "
        "dpkg-source -x apkg-source/*.dsc ../libyang-build && "
        f"(cd ../libyang-build && dpkg-buildpackage --build={binary} -us -uc -tc)",
    )


def prepare_strongswan(
    directory: Path, edits: CheckedEdits, arch: str, binary: str
) -> None:
    """Skip independent-only VICI before execution on arm64."""
    if arch == "arm64":
        edits.replace(
            directory / "package.toml",
            "cd ..; ./build-vici.sh",
            ": # python3-vici is built by amd64 only",
        )


def prepare_udp_packaging(directory: Path) -> None:
    """Repair the rules in the patch applied later by the recipe's git am loop."""
    edits = CheckedEdits()
    stage_udp_packaging(directory, edits, "", "")
    edits.commit()


def stage_udp_packaging(
    directory: Path, edits: CheckedEdits, arch: str, binary: str
) -> None:
    """Stage the audited added-file packaging hunk without touching other hunks."""
    path = directory / "patches/udp-broadcast-relay/0001-Add-Debian-packaging.patch"
    text = edits.read(path)
    # This is an added-file patch, not a checkout of the package source yet.
    if re.findall(r"^\+Package: (.+)$", text, re.MULTILINE) != [
        "udp-broadcast-relay"
    ] or re.findall(r"^\+Architecture: (.+)$", text, re.MULTILINE) != ["linux-any"]:
        raise ValueError(f"{path}: expected one Architecture: linux-any package")
    pattern = r"(\+\+\+ b/debian/rules\n)@@ -0,0 \+1,(\d+) @@\n((?:\+[^\n]*\n)+)"
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError(f"{path}: expected one added debian/rules hunk")
    match = matches[0]
    lines = match[3].splitlines(keepends=True)
    if len(lines) != int(match[2]):
        raise ValueError(f"{path}: unexpected debian/rules hunk length")
    rules = "".join(line[1:] for line in lines)
    for target in (
        "build",
        "build-arch",
        "build-indep",
        "binary",
        "binary-arch",
        "binary-indep",
    ):
        expected = 0 if target in ("build-arch", "build-indep") else 1
        if len(re.findall(rf"^{target}\s*:", rules, re.MULTILINE)) != expected:
            raise ValueError(f"{path}: unexpected {target} target layout")
    replacements = (
        (
            "build: build-stamp\n",
            "build: build-arch build-indep\n\nbuild-arch: build-stamp\n\nbuild-indep:\n",
        ),
        (
            "# Build architecture-independent files here.\nbinary-indep: build install\n",
            "# Build architecture-dependent files here.\nbinary-arch: build install\n",
        ),
        (
            (
                "# Build architecture-dependent files here.\nbinary-arch: build install\n"
                "# This is an architecture independent package\n# so; we have nothing to do by default.\n"
            ),
            "# No architecture-independent packages are produced.\nbinary-indep:\n",
        ),
        ("binary: binary-indep\n", "binary: binary-arch binary-indep\n"),
        (
            ".PHONY: build clean binary-indep binary install\n",
            ".PHONY: build build-arch build-indep clean binary-arch binary-indep binary install\n",
        ),
    )
    for old, _ in replacements:
        if rules.count(old) != 1:
            raise ValueError(
                f"{path}: expected exactly one audited rules block {old!r}"
            )
    for old, new in replacements:
        rules = rules.replace(old, new)
    added = "".join("+" + line for line in rules.splitlines(keepends=True))
    hunk = f"{match[1]}@@ -0,0 +1,{len(rules.splitlines())} @@\n{added}"
    edits.set(path, text[: match.start()] + hunk + text[match.end() :])


RECIPE_HOOKS = {
    "frr": prepare_frr,
    "strongswan": prepare_strongswan,
    "udp-broadcast-relay": stage_udp_packaging,
}


def prepare_extra(root: Path, package: str) -> None:
    """Dispatch standalone adaptations; ordinary Debian sources need no hook."""
    validate_name(package)
    hook = EXTRA_HOOKS.get(package)
    if hook is not None:
        hook(root / package)


def prepare_biosdevname(directory: Path) -> None:
    """Repair the legacy binary target split in the standalone source checkout.

    vyatta-biosdevname 7fbb031 declares one Architecture: any package but puts
    dh_builddeb under binary-indep. Keep the packaging commands intact, moving
    their ownership to binary-arch. Validate all anchors before writing anything.
    """
    package = "vyatta-biosdevname"
    debian = directory / "debian"
    control = (debian / "control").read_text()
    if re.findall(r"^Package:\s*(\S+)\s*$", control, re.MULTILINE) != [
        package
    ] or re.findall(r"^Architecture:\s*(\S+)\s*$", control, re.MULTILINE) != ["any"]:
        raise ValueError(
            f"{debian / 'control'}: expected one Architecture: any package"
        )
    path = debian / "rules"
    text = path.read_text()
    replacements = (
        (
            "build: build-stamp\n",
            "build: build-arch build-indep\n\nbuild-arch: build-stamp\n\nbuild-indep:\n",
        ),
        (
            "# Build architecture-independent files here.\nbinary-indep: build install\n",
            "# Build architecture-dependent files here.\nbinary-arch: build install\n",
        ),
        (
            (
                "# Build architecture-dependent files here.\n"
                "binary-arch: build install\n"
                "# This is an architecture independent package\n"
                "# so; we have nothing to do by default.\n"
            ),
            "# No architecture-independent packages are produced.\nbinary-indep:\n",
        ),
        (
            ".PHONY: build clean binary-indep binary-arch binary install",
            ".PHONY: build build-arch build-indep clean binary-indep binary-arch binary install",
        ),
    )
    # Unexpected extra targets must not silently combine with the added rules.
    for target in ("build", "build-arch", "build-indep", "binary-arch", "binary-indep"):
        expected = 0 if target in ("build-arch", "build-indep") else 1
        if len(re.findall(rf"^{target}\s*:", text, re.MULTILINE)) != expected:
            raise ValueError(f"{path}: unexpected {target} target layout")
    for old, _ in replacements:
        if text.count(old) != 1:
            raise ValueError(
                f"{path}: expected exactly one audited rules block {old!r}"
            )
    for old, new in replacements:
        text = text.replace(old, new)
    edits = CheckedEdits()
    edits.set(path, text)
    edits.commit()


EXTRA_HOOKS = {"vyatta-biosdevname": prepare_biosdevname}


def main(argv: list[str] | None = None) -> int:
    """Parse --root, --package, --arch and --group, then apply the adaptations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--arch", choices=ARCHITECTURES, required=True)
    parser.add_argument("--group", choices=GROUPS, default="build")
    args = parser.parse_args(argv)
    try:
        if args.group == "build-extra":
            prepare_extra(args.root, args.package)
        else:
            prepare(args.root, args.package, args.arch)
    except (OSError, ValueError) as error:
        print(f"prepare_package_build: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
