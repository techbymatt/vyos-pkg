# Repository guidance

## Build boundaries

- This repository orchestrates external package builds; the VyOS recipes are in the `vyos-build` submodule of `techbymatt/tbm-vyos-patch`, not this checkout. Apply downstream patches before `scripts/prepare_package_build.py` adaptations.
- For package additions, follow [README.md: Adding packages](README.md#adding-packages). `scripts/package_catalog.json` contains source names, not necessarily binary package names, grouped under `build` and `build-extra`.
- The manual **Test** workflow accepts source names directly. For standalone packages, its `deps` input must be supplied separately; it does not read the catalog's Publish dependencies.

## Architecture and verification

- amd64 owns every `Architecture: all` output. Independent-only sources use `architecture: independent_only` in `scripts/package_catalog.json`; mixed sources still need both native builds. Skip independent sub-builds on arm64 before execution, rather than filtering their artifacts afterward.
- Custom recipe commands can bypass the shared builder. Audit nested scripts and packaging tools when changing `scripts/prepare_package_build.py`; its exact replacements intentionally fail on upstream command drift.
- Keep Verify downloads in separate `deb-<source>-<arch>` directories. `validate_packages.py --artifacts` derives producer architecture from those names and checks identities across both groups before Publish merges files.
- Completeness is enforced end to end: `plan_builds.py` emits a `verify-plan` list that both callers pass to Verify as `expected-artifacts`, so a producer that contributes no artifact directory fails verification. Restore jobs tolerate cache misses and rely on that check to catch evictions.
- Verification reads package archives without installing packages or executing maintainer scripts. Lintian findings are advisory and never fail a run, but an incomplete scan does: `report_lintian.py` exits 1 when packages timed out, failed, or went unscanned (the verify step runs it with `--total-timeout 3600` inside a 60-minute step).

## Cache and publication coupling

- `scripts/package_sources.py` fingerprints each patched recipe folder, every Git ref it actually builds, and audited package-specific source/configuration inputs. Standalone sources use their `rolling` commit. Shared builders, build images, local workflows/helpers, unrelated patches/data, and catalog prerequisites do not change source cache identities. Deliberately refreshing those build inputs requires `force_rebuild`.
- Resolve and pin every recipe clone, including custom builders and nested hooks, to the same full commits used during planning. Add audited source discovery/pinning for new custom paths; exact replacements intentionally fail on upstream command drift. Keep original ref labels for package-version metadata. Kernel defaults and certificates affect only the kernel.
- `scripts/legacy_package_cache.py` proves old source inputs from producer manifests, historical patched trees, and successful build-step checkout logs. Missing or ambiguous checkout evidence requires an initial build of that entry; never infer moving source revisions from workflow submodule pins or today's refs. Verified old archives are saved under source keys only after restoration/output checks. Preserve cache path order and gzip compatibility throughout migration. Metadata-service outages fail planning rather than bypassing caches.
- Shared build workflows and restore jobs use `.github/actions/package-paths` and `package-artifacts`. Preserve this common path contract and enforce the same output policy for fresh and restored builds.
- `scripts/test_test_workflow.py` pins workflow and action YAML as text (cache policy, explicit App-secret mapping on build-recipe calls and no secrets on build-standalone, `persist-credentials: false`, repository guards, verify wiring, the eight restore slots vs `RESTORE_BATCH_SIZE`). Update it whenever editing workflows or actions. New planner outputs must also be declared in the producing job's `outputs:` block or actionlint reports `[expression]` errors.
- `scripts/publish_manifest.py` controls publication skipping separately from package caching. Schema v2 records source descriptors/fingerprints, including resolved moving recipe refs; v1 remains readable for producer proofs. The manifest advances only with the Pages deployment. Live APT/toolchains remain unlocked; `force_rebuild` refreshes those inputs and republishes.
- `scripts/build_repo.sh` consumes/moves `packages/rolling` into `_site/deb` and signs the repository. In CI it runs after Jekyll; it is not a local test command.

## Focused checks

- Use Python 3.11+; the Python helpers/tests use the standard library, with no Python package-manager bootstrap. Repository assembly fixtures also need Bash, GNU checksum tools, gzip, and bzip2.
- One test module: `python3 -m unittest scripts.test_package_build_policy -v` (append `.ClassName.test_method` for one case).
- All tool tests: `python3 -m unittest discover -s scripts -v`. The pre-commit hook runs this entire suite for changes under `scripts/`, workflows, or actions.
- Workflow validation: `actionlint .github/workflows/*.yaml`.
- Known-good lint findings, do not "fix": actionlint flags the `ubuntu-26.04` runner label (its label table lags the chosen runners), and ruff reports two `TRY004`s in `package_catalog.py` and `publish_manifest.py` (`ValueError` is asserted by tests there).
- Scoped hooks: `pre-commit run --files <changed-files>`.
- Debian build-mode tests skip unless `dpkg-buildpackage` and `make` exist; macOS-only test success does not exercise real Debian builds. Static artifact validation also needs `dpkg` and `dpkg-deb`.
- Upstream adaptation check: `VYOS_BUILD_ROOT=/path/to/patched/vyos-build/scripts/package-build python3 -m unittest scripts.test_prepare_package_build.UpstreamRecipeTests scripts.test_package_sources.UpstreamSourceTests -v`. It uses temporary copies and does not compile packages; without that variable the checks skip.
