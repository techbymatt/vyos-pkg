# Repository guidance

## Build boundaries

- This repository orchestrates external package builds; the VyOS recipes are in the `vyos-build` submodule of `techbymatt/tbm-vyos-patch`, not this checkout. Apply downstream patches before `scripts/prepare_package_build.py` adaptations.
- For package additions, follow [README.md: Adding packages](README.md#adding-packages). Publish's `packages` and `extra_packages` arrays contain source names, not necessarily binary package names.
- The manual **Test** workflow accepts source names directly. For standalone packages, its `deps` input must be supplied separately; it does not read Publish's `scripts/package_dependencies.sh` mapping.

## Architecture and verification

- amd64 owns every `Architecture: all` output. Independent-only sources belong in `scripts/package_build_policy.json`; mixed sources still need both native builds. Skip independent sub-builds on arm64 before execution, rather than filtering their artifacts afterward.
- Custom recipe commands can bypass the shared builder. Audit nested scripts and packaging tools when changing `scripts/prepare_package_build.py`; its exact replacements intentionally fail on upstream command drift.
- Keep Verify downloads in separate `deb-<source>-<arch>` directories. `validate_packages.py --artifacts` derives producer architecture from those names and checks identities across both groups before Publish merges files.
- Verification reads package archives without installing packages or executing maintainer scripts. Lintian is advisory. Architecture/nonempty-output checks do not establish that all expected binary packages were produced.

## Cache and publication coupling

- `scripts/cache_namespace.py` hashes selected build inputs, not the whole repository. Add new build-affecting helpers/policy files to its input surface and tests; verification-only edits should not invalidate package caches.
- Keep cache paths and artifact handling aligned across the `build` and `build-extra` jobs and `.github/actions/restore-package/action.yaml`. Restore-only jobs must enforce the same output policy as fresh builds.
- `scripts/publish_manifest.py` controls publication skipping separately from package caching. The manifest advances only with the Pages deployment; live APT and moving recipe refs are not locked. `force_rebuild` refreshes those inputs and republishes.
- `scripts/build_repo.sh` consumes/moves `packages/rolling` into `_site/deb` and signs the repository. In CI it runs after Jekyll; it is not a local test command.

## Focused checks

- Use Python 3.11+ and `jq`; the Python helpers/tests use the standard library, with no Python package-manager bootstrap.
- One test module: `python3 -m unittest scripts.test_package_build_policy -v` (append `.ClassName.test_method` for one case).
- All tool tests: `python3 -m unittest discover -s scripts -v`. The pre-commit hook runs this entire suite for changes under `scripts/` or either workflow/restore action.
- Workflow validation: `actionlint .github/workflows/publish.yaml .github/workflows/test.yaml`.
- Scoped hooks: `pre-commit run --files <changed-files>`.
- Debian build-mode tests skip unless `dpkg-buildpackage` and `make` exist; macOS-only test success does not exercise real Debian builds. Static artifact validation also needs `dpkg` and `dpkg-deb`.
- Upstream adaptation check: `VYOS_BUILD_ROOT=/path/to/patched/vyos-build/scripts/package-build python3 -m unittest scripts.test_prepare_package_build.UpstreamRecipeTests -v`. It uses temporary copies and does not compile packages; without that variable the check skips.
