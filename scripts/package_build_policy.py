"""Plan and enforce amd64 ownership of architecture-independent packages.

The policy classifies source recipes, not binary package names. Output metadata
is checked on both fresh and restored builds to detect stale classifications.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

POLICY_PATH = Path(__file__).with_name("package_build_policy.json")


def independent_only(group: str, package: str) -> bool:
    policy = json.loads(POLICY_PATH.read_text())
    return package in policy["independent_only"][group]


def architectures(group: str, package: str) -> list[str]:
    policy = json.loads(POLICY_PATH.read_text())
    if any(package in policy[kind][group] for kind in policy):
        return ["amd64"]
    return ["amd64", "arm64"]


def matrix(group: str, packages: list[str], deps: list[str]) -> dict:
    entries = []
    for package in packages:
        for arch in architectures(group, package):
            entry = {
                "package": package,
                "arch": arch,
                "runner_label": "ubuntu-24.04"
                if arch == "amd64"
                else "ubuntu-24.04-arm",
            }
            if group == "build-extra":
                entry["deps"] = " ".join(deps)
            entries.append(entry)
    return {"include": entries}


def check_outputs(group: str, package: str, arch: str, directory: Path) -> None:
    if arch not in architectures(group, package):
        raise ValueError(f"{group}/{package}: no {arch} build is planned")
    paths = sorted(directory.glob("*.deb"))
    if not paths:
        raise ValueError(f"{directory}: no .deb outputs")
    for path in paths:
        actual = subprocess.check_output(
            ["dpkg-deb", "--field", str(path), "Architecture"], text=True
        ).strip()
        allowed = {"all"} if independent_only(group, package) else {arch}
        if arch == "amd64":
            allowed.add("all")
        if actual not in allowed:
            raise ValueError(
                f"{path}: Architecture {actual!r}, expected {sorted(allowed)}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    arches = sub.add_parser("architectures")
    check = sub.add_parser("check")
    for command in (arches, check):
        command.add_argument("--group", choices=("build", "build-extra"), required=True)
        command.add_argument("--package", required=True)
    check.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    check.add_argument("--directory", type=Path, required=True)
    plan = sub.add_parser("matrix")
    plan.add_argument("--group", choices=("build", "build-extra"), required=True)
    plan.add_argument("--packages", required=True, help="JSON array of source recipes")
    plan.add_argument("--deps", default="[]", help="JSON array of extra dependencies")
    args = parser.parse_args()
    try:
        if args.command == "architectures":
            print(" ".join(architectures(args.group, args.package)))
        elif args.command == "matrix":
            print(
                json.dumps(
                    matrix(args.group, json.loads(args.packages), json.loads(args.deps))
                )
            )
        else:
            check_outputs(args.group, args.package, args.arch, args.directory)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"package_build_policy: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
