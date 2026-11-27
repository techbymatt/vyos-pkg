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
    from .checked_edits import CheckedEdits
    from .package_catalog import ARCHITECTURES, GROUPS, validate_name
    from .workflow_utils import cli_error
except ImportError:
    from checked_edits import CheckedEdits
    from package_catalog import ARCHITECTURES, GROUPS, validate_name
    from workflow_utils import cli_error

__all__ = ["prepare", "prepare_extra"]


@dataclass(frozen=True)
class BinaryCommand:
    """An audited command and its exact occurrence count within one recipe file."""

    command: str
    path: str = "package.toml"
    count: int = 1


# The core Debian rules architecture-split repair shared by the
# udp-broadcast-relay patch rewrite and the standalone vyatta-biosdevname
# source repair. Each repair adds only its own trailing replacements and
# target validation; the core anchors and rewrites are identical.
RULES_CORE_REPLACEMENTS = (
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
            "# This is an architecture independent package\n"
            "# so; we have nothing to do by default.\n"
        ),
        "# No architecture-independent packages are produced.\nbinary-indep:\n",
    ),
)

RULES_ARCH_TARGETS = ("build", "build-arch", "build-indep", "binary-arch", "binary-indep")


def apply_rules_replacements(text: str, replacements: tuple, context: object) -> str:
    """Apply audited rules replacements, requiring exactly one occurrence each."""
    for old, _ in replacements:
        if text.count(old) != 1:
            raise ValueError(f"{context}: expected exactly one audited rules block {old!r}")
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def validate_rules_targets(text: str, targets: tuple[str, ...], context: object) -> None:
    """Require the audited debian/rules target layout before rewriting it."""
    for target in targets:
        expected = 0 if target in ("build-arch", "build-indep") else 1
        if len(re.findall(rf"^{target}\s*:", text, re.MULTILINE)) != expected:
            raise ValueError(f"{context}: unexpected {target} target layout")


# A new overridden Debian build usually needs just one entry here, not another
# branch in preparation or workflow YAML. More complex tools use the hook below.
BINARY_COMMANDS = {
    "dropbear": BinaryCommand("dpkg-buildpackage -us -uc -tc -b"),
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


def stage_git_clones(root: Path, package: str, edits: CheckedEdits) -> None:
    """Route the shared and kernel builders' audited clones through Git retries."""
    builders = [(root / "build.py", "package['scm_url']")]
    if package == "linux-kernel":
        builders.append((root / package / "build.py", "scm_url"))
    for builder, url in builders:
        edits.replace(
            builder,
            f"run(['git', 'clone', {url}, str(repo_dir)], check=True)",
            f"clone_repository(repo_dir, {url})",
        )
        edits.replace(
            builder,
            "from subprocess import run, CalledProcessError",
            "from subprocess import run, CalledProcessError\n"
            "from git_network import clone_repository",
        )


def prepare_kernel(
    directory: Path, edits: CheckedEdits, _arch: str, _binary: str
) -> None:
    """Retry key discovery and abort before building kernel-dependent packages."""
    builder = directory / "build.py"
    edits.replace(
        builder,
        "from subprocess import run, CalledProcessError",
        "from subprocess import run, CalledProcessError\n"
        "from kernel_keys import locate_kernel_keys",
    )
    # The outer package loop catches CalledProcessError and tries dependent
    # builds anyway. Fail here on key lookup failure, without touching --verify.
    edits.replace(
        builder,
        "\n        run(['gpg2', '--locate-keys', 'torvalds@kernel.org', "
        "'gregkh@kernel.org'], check=True)\n",
        "\n        try:\n"
        "            locate_kernel_keys('torvalds@kernel.org', 'gregkh@kernel.org')\n"
        "        except (OSError, subprocess.SubprocessError) as e:\n"
        '            sys.exit(f"Failed to locate kernel signing keys: {e}")\n',
    )


def prepare_frr(directory: Path, edits: CheckedEdits, arch: str, binary: str) -> None:
    """Retain FRR source builds and render libyang with the native binary mode."""
    # Upstream uses source builds to catch patch failures; keep those checks.
    mode = "full" if arch == "amd64" else "source,any"
    command = (
        "dpkg-buildpackage -us -uc -tc -F "
        "--source-option=--tar-ignore=.git "
        "--source-option=--tar-ignore=.github "
        "--source-option=-i "
        r"--source-option=--extend-diff-ignore='^\\.github(?:/.*)?$' "
        "-Ppkg.frr.rtrlib,pkg.frr.lua"
    )
    edits.replace(
        directory / "package.toml",
        command,
        command.replace(" -F ", f" --build={mode} "),
    )
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
    validate_rules_targets(
        rules, ("build", "build-arch", "build-indep", "binary", "binary-arch", "binary-indep"), path
    )
    replacements = (
        *RULES_CORE_REPLACEMENTS,
        ("binary: binary-indep\n", "binary: binary-arch binary-indep\n"),
        (
            ".PHONY: build clean binary-indep binary install\n",
            ".PHONY: build build-arch build-indep clean binary-arch binary-indep binary install\n",
        ),
    )
    rules = apply_rules_replacements(rules, replacements, path)
    added = "".join("+" + line for line in rules.splitlines(keepends=True))
    hunk = f"{match[1]}@@ -0,0 +1,{len(rules.splitlines())} @@\n{added}"
    edits.set(path, text[: match.start()] + hunk + text[match.end() :])


RECIPE_HOOKS = {
    "frr": prepare_frr,
    "linux-kernel": prepare_kernel,
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
        *RULES_CORE_REPLACEMENTS,
        (
            ".PHONY: build clean binary-indep binary-arch binary install",
            ".PHONY: build build-arch build-indep clean binary-indep binary-arch binary install",
        ),
    )
    # Unexpected extra targets must not silently combine with the added rules.
    validate_rules_targets(text, RULES_ARCH_TARGETS, path)
    text = apply_rules_replacements(text, replacements, path)
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
        return cli_error("prepare_package_build", error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
