"""Apply audited build-mode adaptations after the downstream VyOS patches.

These exact, checked replacements deliberately fail when an upstream command
changes. They do not rewrite arbitrary shell commands or intercept dpkg globally.
Custom arch-only builders (including the kernel) keep their own build targets.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    """Rewrite the file, failing unless the audited command occurs exactly once."""
    text = path.read_text()
    if text.count(old) != 1:
        raise ValueError(f"{path}: expected exactly one audited command {old!r}")
    path.write_text(text.replace(old, new))


def prepare(root: Path, package: str, arch: str) -> None:
    """Apply audited build-mode adaptations to one package recipe and builders."""
    # The default builder includes source packages; retain that behavior.
    mode = "full" if arch == "amd64" else "source,any"
    replace_once(
        root / "build.py",
        "dpkg-buildpackage -uc -us -tc -F --source-option",
        f"dpkg-buildpackage -uc -us -tc --build={mode} --source-option",
    )
    recipe = root / package / "package.toml"
    if package == "udp-broadcast-relay":
        prepare_udp_packaging(root / package)
    binary = "binary" if arch == "amd64" else "any"
    # Only known command fragments in explicitly audited recipes are changed.
    commands = {
        "dropbear": ["dpkg-buildpackage -us -uc -tc -b"],
        "frr": ["dpkg-buildpackage -us -uc -tc -b -Ppkg.frr.rtrlib,pkg.frr.lua"],
        "net-snmp": ["dpkg-buildpackage -us -uc -tc -b || true"],
        "netfilter": ["dpkg-buildpackage -uc -us -tc -b"],
        "openssl": ["dpkg-buildpackage -us -uc -tc -b"],
        "openvpn": ["dpkg-buildpackage -uc -us -tc -b"],
        "strongswan": ["dpkg-buildpackage -uc -us -tc -b -d"],
        "tacacs": [],
        "udp-broadcast-relay": ["dpkg-buildpackage -uc -us -tc -b -d"],
        "xen-guest-agent": ["dpkg-buildpackage -b -us -uc"],
    }
    for command in commands.get(package, []):
        replacement = command.replace(" -b", f" --build={binary}")
        replace_once(recipe, command, replacement)
    if package == "tacacs":
        # Three separate sources in a dependency chain, all using this command.
        text = recipe.read_text()
        command = "dpkg-buildpackage -us -uc -tc -b"
        if text.count(command) != 3:
            raise ValueError(
                "tacacs: expected three audited dpkg-buildpackage commands"
            )
        recipe.write_text(
            text.replace(command, f"dpkg-buildpackage -us -uc -tc --build={binary}")
        )
    if package == "hostap":
        replace_once(
            root / package / "build.sh",
            "dpkg-buildpackage -us -uc -tc -b -Ppkg.wpa.nogui,noudeb",
            f"dpkg-buildpackage -us -uc -tc --build={binary} -Ppkg.wpa.nogui,noudeb",
        )
    if package == "strongswan" and arch == "arm64":
        replace_once(
            recipe, "cd ..; ./build-vici.sh", ": # python3-vici is built by amd64 only"
        )
    if package == "frr":
        # apkg has no binary build-type option. Retain its dependency installation
        # and source-template rendering, then build the rendered source explicitly.
        replace_once(
            recipe,
            "pipx run apkg build -i && find pkg/pkgs -type f -name *.deb -exec mv -t .. {} +",
            "pipx run apkg build-dep && "
            "pipx run apkg srcpkg --result-dir apkg-source && "
            "dpkg-source -x apkg-source/*.dsc ../libyang-build && "
            f"(cd ../libyang-build && dpkg-buildpackage --build={binary} -us -uc -tc)",
        )


def prepare_udp_packaging(directory: Path) -> None:
    """Repair the rules in the patch applied later by the recipe's git am loop."""
    path = directory / "patches/udp-broadcast-relay/0001-Add-Debian-packaging.patch"
    text = path.read_text()
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
    path.write_text(text[: match.start()] + hunk + text[match.end() :])


def prepare_extra(root: Path, package: str) -> None:
    """Repair the legacy binary target split in the standalone source checkout.

    vyatta-biosdevname 7fbb031 declares one Architecture: any package but puts
    dh_builddeb under binary-indep. Keep the packaging commands intact, moving
    their ownership to binary-arch. Validate all anchors before writing anything.
    """
    if package != "vyatta-biosdevname":
        return
    debian = root / package / "debian"
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
    path.write_text(text)


def main() -> int:
    """Parse --root, --package, --arch and --group, then apply the adaptations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--group", choices=("build", "build-extra"), default="build")
    args = parser.parse_args()
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
