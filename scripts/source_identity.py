"""Pure source descriptors shared by fingerprints, manifests and cache proofs.

No network, checkout, builder or catalog build setting is part of this model.
Keep its canonical encoding stable so refactors do not invalidate source keys.
"""

from __future__ import annotations

import hashlib
import re

try:
    from .package_catalog import validate_name
    from .workflow_utils import canonical_json
except ImportError:
    from package_catalog import validate_name
    from workflow_utils import canonical_json

REVISION = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"


def revision(value: object) -> str:
    """Require a full, lowercase Git object ID."""
    if not isinstance(value, str) or re.fullmatch(REVISION, value) is None:
        raise ValueError(f"invalid source revision: {value!r}")
    return value


def source_text(value: object, field: str) -> str:
    """Require nonempty single-line text that cannot be a command-line option."""
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("-")
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"invalid source {field}: {value!r}")
    return value


def validate_repositories(value: object) -> list[dict]:
    """Validate and sort resolved repositories without changing ref labels."""
    if not isinstance(value, list):
        raise TypeError("source repositories must be an array")
    result = []
    seen = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "url",
            "ref",
            "commit",
        }:
            raise ValueError("source repository requires name/url/ref/commit")
        name = validate_name(entry["name"])
        if name in seen:
            raise ValueError(f"duplicate source repository: {name}")
        seen.add(name)
        result.append(
            {
                "name": name,
                "url": source_text(entry["url"], "URL"),
                "ref": source_text(entry["ref"], "ref"),
                "commit": revision(entry["commit"]),
            }
        )
    return sorted(result, key=lambda entry: entry["name"])


def validate_source(value: object) -> dict:
    """Validate the canonical per-source descriptor used by caches/manifests."""
    if not isinstance(value, dict) or set(value) != {
        "recipe_tree",
        "inputs",
        "repositories",
    }:
        raise ValueError("source requires recipe_tree/inputs/repositories")
    tree = value["recipe_tree"]
    if tree is not None:
        revision(tree)
    inputs = value["inputs"]
    if not isinstance(inputs, dict):
        raise TypeError("source inputs must be an object")
    for key, text in inputs.items():
        if not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]*", key) is None:
            raise ValueError("invalid scoped source input name")
        source_text(text, f"input {key}")
    repositories = validate_repositories(value["repositories"])
    if tree is None and (inputs or len(repositories) != 1):
        raise ValueError(
            "standalone sources require exactly one repository and no inputs"
        )
    return {"recipe_tree": tree, "inputs": dict(inputs), "repositories": repositories}


def fingerprint(source: dict) -> str:
    """Hash only validated source inputs, never orchestration or a global namespace."""
    return hashlib.sha256(canonical_json(validate_source(source))).hexdigest()
