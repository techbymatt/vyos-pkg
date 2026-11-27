"""Fingerprint package sources and pin recipe checkouts to planned commits.

Compatibility facade and CLI. The implementation lives in recipe_inputs.py
(patched-recipe readers, no network), source_resolver.py (bounded Git ref
resolution) and source_pinning.py (audited checkouts and pins). This module
re-exports the historical package_sources API unchanged — upstream builders do
`from package_sources import checkout_source` — and keeps the
`python3 -m scripts.package_sources` / `python3 scripts/package_sources.py`
entry points used by the build workflows.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    from . import package_catalog as catalog
    from .checked_edits import CheckedEdits
    from .git_network import clone_repository, remote_git
    from .package_cache import cache_prefix
    from .prepare_package_build import stage_git_clones
    from .recipe_inputs import (
        KERNEL_VPP_APT,
        RecipeReader,
        audit_kernel_vpp_build,
        discover_sources,
        kea_repository,
        kernel_uses_vpp,
        recipe_repositories,
    )
    from .source_identity import (
        REVISION,
        fingerprint,
        revision,
        source_text,
        validate_repositories,
        validate_source,
    )
    from .source_pinning import (
        KEA_CLONE,
        PIN_ENV,
        checkout_source,
        pin_recipe,
        retry_recipe_clones,
        stage_kea_clone,
        stage_recipe_pins,
    )
    from .source_resolver import (
        GITLAB_HOSTS,
        SHORT_REVISION,
        SOURCE_JOBS,
        Resolver,
        resolve_sources,
    )
    from .workflow_utils import canonical_json as canonical_bytes
    from .workflow_utils import cli_error, command_output, git
except ImportError:
    import package_catalog as catalog
    from checked_edits import CheckedEdits
    from git_network import clone_repository, remote_git
    from package_cache import cache_prefix
    from prepare_package_build import stage_git_clones
    from recipe_inputs import (
        KERNEL_VPP_APT,
        RecipeReader,
        audit_kernel_vpp_build,
        discover_sources,
        kea_repository,
        kernel_uses_vpp,
        recipe_repositories,
    )
    from source_identity import (
        REVISION,
        fingerprint,
        revision,
        source_text,
        validate_repositories,
        validate_source,
    )
    from source_pinning import (
        KEA_CLONE,
        PIN_ENV,
        checkout_source,
        pin_recipe,
        retry_recipe_clones,
        stage_kea_clone,
        stage_recipe_pins,
    )
    from source_resolver import (
        GITLAB_HOSTS,
        SHORT_REVISION,
        SOURCE_JOBS,
        Resolver,
        resolve_sources,
    )
    from workflow_utils import canonical_json as canonical_bytes
    from workflow_utils import cli_error, command_output, git

__all__ = [
    "GITLAB_HOSTS",
    "KEA_CLONE",
    "KERNEL_VPP_APT",
    "PIN_ENV",
    "REVISION",
    "SHORT_REVISION",
    "SOURCE_JOBS",
    "CheckedEdits",
    "RecipeReader",
    "Resolver",
    "audit_kernel_vpp_build",
    "cache_prefix",
    "canonical_bytes",
    "catalog",
    "checkout_source",
    "clone_repository",
    "command_output",
    "discover_sources",
    "fingerprint",
    "git",
    "kea_repository",
    "kernel_uses_vpp",
    "pin_recipe",
    "recipe_repositories",
    "remote_git",
    "resolve_sources",
    "retry_recipe_clones",
    "revision",
    "source_text",
    "stage_git_clones",
    "stage_kea_clone",
    "stage_recipe_pins",
    "validate_repositories",
    "validate_source",
]


def main(argv: list[str] | None = None) -> int:
    """Adapt recipe commands or perform an audited nested clone."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pin = commands.add_parser("pin")
    pin.add_argument("--root", type=Path, required=True)
    pin.add_argument("--package", required=True)
    pin.add_argument("--sources", required=True)
    retry = commands.add_parser("retry-clones")
    retry.add_argument("--root", type=Path, required=True)
    retry.add_argument("--package", required=True)
    checkout = commands.add_parser("checkout")
    checkout.add_argument("--directory", type=Path, required=True)
    checkout.add_argument("--url", required=True)
    checkout.add_argument("--ref", required=True)
    checkout.add_argument("--unplanned", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "pin":
            pin_recipe(args.root, args.package, json.loads(args.sources))
        elif args.command == "retry-clones":
            retry_recipe_clones(args.root, args.package)
        else:
            source_text(args.url, "URL")
            if args.unplanned:
                clone_repository(args.directory, args.url, "--branch", args.ref)
            else:
                if not args.directory.exists():
                    clone_repository(args.directory, args.url, timeout=300)
                checkout_source(args.directory, args.url, args.ref)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        return cli_error("package_sources", error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
