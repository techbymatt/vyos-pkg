# Repository guidance

`CLAUDE.md` symlinks here; keep one instruction source. Package additions: [README checklist](README.md#adding-packages). Module boundaries and extension points: [architecture guide](doc/architecture.md).

## Build boundaries

- `build` recipes live in `techbymatt/tbm-vyos-patch`'s pinned `vyos-build/scripts/package-build/<name>/`, not this checkout. `build-extra` sources are standalone `vyos/<name>` repositories on `rolling`.
- `scripts/package_catalog.json` owns **source**, not binary, names and build settings: `architecture`, standalone `deps`, execution-only `go`/`priority`/`timeout_minutes`. Add via `python3 scripts/package_catalog.py add`; also update pinned membership/deps in `scripts/test_package_catalog.py`. Do not add workflow package lists.
- Manual **Test** uses comma-separated `package` (recipes), `package-extra` (standalone), and explicit standalone `deps` (catalog deps are ignored). It disables caching. Unknown sources default to both architectures; unknown recipes retain Go, catalogued recipes need `go: true`.
- Recipe order: downstream patches → `scripts/prepare_package_build.py` → `scripts/package_sources.py pin` (Publish) → build. Pin every built clone to its planned full commit; retain original ref labels for version metadata.
- Audit custom/nested build commands in `scripts/prepare_package_build.py` and source discovery/pinning in `scripts/package_sources.py`. Stage adaptations with `scripts/checked_edits.py`; upstream drift must fail before writing. Keep `package_sources.checkout_source` importable by adapted upstream builders.
- Keep `scripts/build_matrix.py` and `scripts/source_identity.py` free of network/CLI lookups. Preserve canonical source encoding so refactors do not invalidate cache keys.

## Package and CI contracts

- `architecture` defaults to `dual`, including mixed outputs. `independent_only` and `amd64_only` schedule amd64 only; no arm64-only policy exists. amd64 owns every `Architecture: all` binary: skip independent sub-builds on arm64 before execution, never delete duplicates afterward.
- Fresh/restored outputs share `.github/actions/package-paths`, `package-artifacts`, and `package-cache-compression`. `scripts/package_layout.py` owns cache path order/metadata scope and file-only cleanup; preserve gzip compatibility and never remove source directories or external symlink targets.
- Verify downloads stay unmerged in `deb-<source>-<arch>` directories. Both callers pass planner `verify-plan` as `expected-artifacts` and `verify-arches` as `architectures`; this catches restore misses, not missing binaries within a producer. Review the expected binary set in native Test artifacts.
- Verification is static: never install packages, run maintainer scripts, or extract payloads onto the host. Lintian findings are advisory; failed/timed-out/unscanned packages fail Verify. Keep `--total-timeout 3600` within its 60-minute step.
- Workflow/action YAML is tested as text in `scripts/test_test_workflow.py`. Preserve shared reusable builds, Test/Publish cache policy, explicit App secrets for recipe calls only, `persist-credentials: false`, and serialized/repository-gated publication after successful Verify. Keep eight restore slots aligned with `build_matrix.RESTORE_BATCH_SIZE`.
- Planner stdout is `GITHUB_OUTPUT` assignments only; diagnostics go to stderr. Declare new outputs in the producing job's `outputs:` block.

## Cache and publication

- `scripts/package_sources.py` fingerprints patched recipe trees, every built Git repository, and scoped consumer inputs. Kernel-only inputs include defaults/certificates and the sibling VPP recipe/repos through accel-PPP; Intel cleanup must reset to planned `HEAD`, never `origin/main`.
- Source keys exclude shared builders, images, local helpers/workflows, unrelated inputs, catalog deps and execution settings. Run **Repository** with `force_rebuild` for local policy/tooling changes, unlocked APT/toolchain refreshes, and after signing-credential rotation; republishing alone can reuse binaries.
- `scripts/legacy_package_cache.py` requires producer manifests, historical patched inputs and successful-build checkout evidence for recipes; standalone hits use recorded commits. Never substitute today's refs/submodule pins. Missing/ambiguous proof rebuilds; metadata outages fail planning. Inaccessible-ref caches are diagnostic only. Save migrations after exact-hit/output checks.
- Only deployed Pages `input-manifest.json` advances the publication baseline, never a local candidate. `scripts/publish_manifest.py` writes schema v2; retain v1 reads for legacy proofs.
- Do not use `scripts/build_repo.sh` as a local test: it moves `packages/rolling` inputs into `_site/deb` and signs after Jekyll. Use `python3 -m unittest scripts.test_build_repo -v` fixtures instead.

## Focused checks

- Python 3.11+, stdlib only. Fixtures need Git, Bash, gzip and bzip2; repository-assembly tests fake Debian scanners, checksum tools and GPG.
- Catalog: `python3 scripts/package_catalog.py validate`. Suite: `python3 -m unittest discover -s scripts -v`.
- Module: `python3 -m unittest scripts.test_package_build_policy -v`; one case: append `.PolicyTests.test_empty_outputs_fail` to the module name.
- Hooks: `pre-commit run --files <changed-files>`; changes under `scripts/` or `.github/{workflows,actions}/` trigger the full suite. YAML: `actionlint .github/workflows/*.yaml`.
- Known baseline lint findings, do not "fix": actionlint's unknown `ubuntu-26.04` label and two Ruff `TRY004`s in `scripts/package_catalog.py`/`scripts/publish_manifest.py`; retain their `ValueError` contracts.
- Debian build-mode tests skip without `dpkg-buildpackage`/`make`; macOS success is not real-build coverage. Static validation needs `dpkg` and `dpkg-deb`.
- Upstream audits: `VYOS_BUILD_ROOT=/path/to/patched/vyos-build/scripts/package-build python3 -m unittest scripts.test_prepare_package_build.UpstreamRecipeTests scripts.test_package_sources.UpstreamSourceTests -v`. Temporary copies, no upstream compilation; skip when the variable is unset.
