"""Deterministic Test/Publish matrices, independent of GitHub and network IO."""

from __future__ import annotations

import re

try:
    from . import package_catalog as catalog
    from . import publish_manifest as manifest
    from . import source_identity
    from .package_cache import CacheIndex, cache_prefix
    from .package_layout import artifact_name
except ImportError:
    import package_catalog as catalog
    import publish_manifest as manifest
    import source_identity
    from package_cache import CacheIndex, cache_prefix
    from package_layout import artifact_name

RESTORE_BATCH_SIZE = 8
MAX_MATRIX_JOBS = 256
RUNNERS = {"amd64": "ubuntu-26.04", "arm64": "ubuntu-26.04-arm"}


def runner_label(arch: str) -> str:
    """Use the native runner for a supported producer architecture."""
    if arch not in RUNNERS:
        raise ValueError(f"invalid producer architecture: {arch}")
    return RUNNERS[arch]


def parse_input(raw: str, *, dependency: bool = False) -> list[str]:
    """Normalize comma/whitespace inputs, preserving first occurrence order."""
    return list(
        dict.fromkeys(
            catalog.validate_name(token, dependency=dependency)
            for token in re.split(r"[,\s]+", raw.strip())
            if token
        )
    )


def verification_plan(records: list[dict]) -> dict:
    """Describe every expected producer, even when its cache may be evicted later."""
    names = [artifact_name(row["package"], row["arch"]) for row in records]
    if len(set(names)) != len(names):
        raise ValueError("duplicate package producers in build plan")
    return {
        "verify-arches": sorted({row["arch"] for row in records}),
        "verify-plan": sorted(names),
    }


def check_matrix_sizes(result: dict) -> None:
    """Fail planning explicitly instead of emitting an unusable Actions matrix."""
    for name, matrix in result.items():
        if name.endswith("-matrix") and len(matrix["include"]) > MAX_MATRIX_JOBS:
            raise ValueError(
                f"{name} exceeds GitHub's {MAX_MATRIX_JOBS}-job matrix limit"
            )


def plan_test(
    packages: str, extra_packages: str, deps: str, sources: dict | None = None
) -> dict:
    """Plan uncached native Test builds; explicit deps override catalog deps."""
    sources = catalog.load_catalog() if sources is None else sources
    dependencies = " ".join(parse_input(deps, dependency=True))
    selected = {
        "build": parse_input(packages),
        "build-extra": parse_input(extra_packages),
    }
    if set(selected["build"]) & set(selected["build-extra"]):
        raise ValueError("a Test source cannot appear in both build groups")
    result = {}
    producers = []
    for group, names in selected.items():
        entries = []
        for name in names:
            definition = catalog.find_source(group, name, sources)
            for arch in definition.architectures:
                entry = {
                    "package": name,
                    "arch": arch,
                    "runner_label": runner_label(arch),
                    **definition.build_settings(),
                }
                if group == "build-extra":
                    entry["deps"] = dependencies
                entries.append(entry)
        result[group + "-matrix"] = {"include": entries}
        producers.extend(entries)
    result.update(verification_plan(producers))
    check_matrix_sizes(result)
    return result


def source_records(sources: dict, revisions: dict[tuple[str, str], dict]) -> list[dict]:
    """Expand resolved fingerprints using the catalog's source/architecture policy."""
    records = []
    for definition in catalog.iter_sources(sources):
        group, name = definition.group, definition.name
        resolved = revisions[group, name]
        commit = manifest.require_pattern(
            resolved["commit"], manifest.REVISION, f"{group}/{name} revision"
        )
        inputs = source_identity.validate_source(resolved["source"])
        digest = source_identity.fingerprint(inputs)
        if resolved["source_digest"] != digest:
            raise ValueError(f"{group}/{name} source fingerprint does not match inputs")
        for arch in definition.architectures:
            records.append(
                {
                    "group": group,
                    "package": name,
                    "arch": arch,
                    "commit": commit,
                    "deps": " ".join(definition.deps),
                    "source": inputs,
                    "source_digest": digest,
                }
            )
    return records


def plan_publish(
    records: list[dict],
    cached_keys: list[str],
    run: str,
    *,
    changed: bool = True,
    force_rebuild: bool = False,
    legacy_matches: dict[tuple[str, str, str], str] | None = None,
    sources: dict | None = None,
) -> dict:
    """Route builds and lean restore batches without mixing native runners."""
    result = {
        name: {"include": []}
        for name in ("build-matrix", "build-extra-matrix", "restore-matrix")
    }
    if not changed and not force_rebuild:
        result.update(verification_plan([]))
        return result
    sources = catalog.load_catalog() if sources is None else sources
    index = CacheIndex([] if force_rebuild else cached_keys)
    definitions = {}
    hits: dict[str, list[dict]] = {}
    for record in records:
        prefix = cache_prefix(record)
        key = index.source_hit(record)
        if key is None and not force_rebuild:
            key = (legacy_matches or {}).get(manifest.package_identity(record))
        if key:
            # Restore/upload needs no cloned-repository descriptor, dependencies
            # or recipe revision. Keep job outputs small as the catalog grows.
            entry = {field: record[field] for field in ("group", "package", "arch")}
            entry["cache_key"] = key
            if key.startswith("cache-v2-"):
                entry["save_cache_key"] = prefix + run
            hits.setdefault(runner_label(record["arch"]), []).append(entry)
        else:
            identity = record["group"], record["package"]
            if identity not in definitions:
                definitions[identity] = catalog.find_source(*identity, sources)
            definition = definitions[identity]
            entry = dict(
                record,
                runner_label=runner_label(record["arch"]),
                cache_key=prefix + run,
                **definition.build_settings(),
            )
            if not entry["deps"]:
                del entry["deps"]
            result[record["group"] + "-matrix"]["include"].append(entry)
    for group in catalog.GROUPS:
        # Queue expensive sources first to avoid a kernel-sized tail at width 4.
        # Stable sorting keeps catalog/architecture order for equal priorities.
        result[group + "-matrix"]["include"].sort(
            key=lambda entry: -definitions[group, entry["package"]].priority
        )
    for runner, entries in sorted(hits.items()):
        for start in range(0, len(entries), RESTORE_BATCH_SIZE):
            result["restore-matrix"]["include"].append(
                {
                    "runner_label": runner,
                    "entries": entries[start : start + RESTORE_BATCH_SIZE],
                }
            )
    result.update(verification_plan(records))
    check_matrix_sizes(result)
    return result
