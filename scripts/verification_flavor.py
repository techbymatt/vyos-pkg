#!/usr/bin/env python3
"""Prepare generic-image flavor metadata for package installation checks."""

from __future__ import annotations

import argparse
import json
import re
import runpy
import sys
from pathlib import Path

import tomllib


def string_table(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError("boot_settings must be a table")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError("boot_settings keys and values must be strings")
        result[key] = item
    return result


def load_config(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def config_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value) is None
    ):
        raise ValueError("invalid build configuration name")
    return value


def create_flavor(source: Path, arch: str, flavor: str) -> dict[str, str]:
    arch = config_name(arch)
    flavor = config_name(flavor)
    # Load only defaults.py, not the image builder and its system dependencies.
    defaults = runpy.run_path(str(source / "scripts/image-build/defaults.py"))
    settings = string_table(defaults.get("boot_settings"))
    config = load_config(source / "data/defaults.toml")
    flavor_config = load_config(source / f"data/build-flavors/{flavor}.toml")
    build_type = config_name(flavor_config.get("build_type", config.get("build_type")))
    # Match the image builder's scalar boot-setting precedence.
    for layer in (
        config,
        load_config(source / f"data/build-types/{build_type}.toml"),
        load_config(source / f"data/architectures/{arch}.toml"),
        flavor_config,
    ):
        settings.update(string_table(layer.get("boot_settings", {})))
    console_type = settings.get("console_type", "")
    console_num = settings.get("console_num", "")
    console_speed = settings.get("console_speed", "")
    if re.fullmatch(r"tty[A-Za-z]*", console_type) is None:
        raise ValueError("invalid console_type")
    if re.fullmatch(r"[0-9]+", console_num) is None:
        raise ValueError("invalid console_num")
    if re.fullmatch(r"[0-9]+", console_speed) is None or int(console_speed) == 0:
        raise ValueError("invalid console_speed")
    return {
        "flavor": flavor,
        "console_type": console_type,
        "console_num": console_num,
        "console_speed": console_speed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--flavor", default="generic")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        metadata = create_flavor(args.source, args.arch, args.flavor)
        args.output.write_text(
            json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError, TypeError) as error:
        print(f"verification_flavor: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
