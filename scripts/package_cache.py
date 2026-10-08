"""Indexed cache selection; existence never overrides workflow ref visibility."""

from __future__ import annotations

import re

try:
    from .package_catalog import SOURCE_PATTERN
except ImportError:
    from package_catalog import SOURCE_PATTERN

SOURCE_KEY = re.compile(
    rf"cache-v3-build(?:-extra)?-{SOURCE_PATTERN}-(?:amd64|arm64)-[0-9a-f]{{64}}-"
)


def cache_prefix(record: dict) -> str:
    """Identify one source/architecture independently of local build tooling."""
    return (
        f"cache-v3-{record['group']}-{record['package']}-{record['arch']}-"
        f"{record['source_digest']}-"
    )


class CacheIndex:
    """Index newest-first visible keys once, rather than scanning for every source."""

    def __init__(self, keys: list[str]) -> None:
        """Keep the first exact source prefix; ignore unrelated cache formats."""
        self.sources: dict[str, str] = {}
        for key in keys:
            match = SOURCE_KEY.match(key)
            if match is not None:
                self.sources.setdefault(match[0], key)

    def source_hit(self, record: dict) -> str | None:
        """Return the newest visible matching source key, if any."""
        return self.sources.get(cache_prefix(record))


def visible_cache_keys(caches: list[dict], ref: str, default_branch: str) -> list[str]:
    """Newest first across only the current ref and the default branch."""
    visible = [c for c in caches if c["ref"] in (ref, f"refs/heads/{default_branch}")]
    return [
        c["key"] for c in sorted(visible, key=lambda c: c["created_at"], reverse=True)
    ]
