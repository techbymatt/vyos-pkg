"""Apply audited build-mode adaptations after the downstream VyOS patches.

These exact, checked replacements deliberately fail when an upstream command
changes. They do not rewrite arbitrary shell commands or intercept dpkg globally.
Custom arch-only builders (including the kernel) keep their own build targets.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if text.count(old) != 1:
        raise ValueError(f"{path}: expected exactly one audited command {old!r}")
    path.write_text(text.replace(old, new))


def prepare(root: Path, package: str, arch: str) -> None:
    # The default builder includes source packages; retain that behavior.
    mode = "full" if arch == "amd64" else "source,any"
    replace_once(
        root / "build.py",
        "dpkg-buildpackage -uc -us -tc -F --source-option",
        f"dpkg-buildpackage -uc -us -tc --build={mode} --source-option",
    )
    recipe = root / package / "package.toml"
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()
    try:
        prepare(args.root, args.package, args.arch)
    except (OSError, ValueError) as error:
        print(f"prepare_package_build: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
