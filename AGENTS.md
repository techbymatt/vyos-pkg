# Repository guidance

`CLAUDE.md` symlinks to this file; keep one instruction source.

## Build boundaries

- Recipes live in the `vyos-build` submodule of `techbymatt/tbm-vyos-patch`, not this checkout. Execution order: downstream patches → `scripts/prepare_package_build.py` → `scripts/package_sources.py pin` (Publish) → build.
- For additions, follow [README.md: Adding packages](README.md#adding-packages). `scripts/package_catalog.json` lists **source** names: `build` for recipes, `build-extra` for standalone `vyos/<name>` repositories on `rolling`, not necessarily binary package names. Membership/dependencies are also pinned in `scripts/test_package_catalog.py`.
- Manual **Test** uses `package` for recipes and `package-extra` for standalone sources; uncatalogued names default to both architectures. Supply standalone dependencies through comma-separated `deps`; catalog dependencies are ignored. Test disables caching.

## Package and CI contracts

- Catalog `architecture` defaults to `dual`; mixed sources need both native builds. Use `independent_only` for all-independent outputs and `amd64_only` for amd64-only sources; both schedule only amd64.
- amd64 owns every `Architecture: all` output. Skip independent sub-builds on arm64 before execution, never by deleting duplicate artifacts afterward.
- Audit custom builders, nested hooks, and packaging tools in `scripts/prepare_package_build.py` (build modes) and `scripts/package_sources.py` (discovery/pinning). Checked replacements must fail on upstream drift. Publish must pin every clone to its planned full commit; retain original ref labels for version metadata.
- Use `.github/actions/package-paths` and `.github/actions/package-artifacts` for fresh and restored outputs. Preserve ordered cache paths and gzip compatibility for legacy archives.
- Keep Verify downloads unmerged in `deb-<source>-<arch>` directories. `scripts/validate_packages.py --artifacts` infers architecture from these names and checks identities across all producers.
- Both callers must pass `scripts/plan_builds.py`'s `verify-plan` to Verify as `expected-artifacts`: restore jobs tolerate cache misses, so this catches evictions/missing producers. It does not prove every expected binary was built.
- Verification never installs packages or executes maintainer scripts. Lintian findings are advisory, but failed/timed-out/unscanned packages fail Verify; keep `--total-timeout 3600` within its 60-minute step.
- Workflow/action YAML is tested as text in `scripts/test_test_workflow.py`: preserve cache policy, explicit App secrets only for recipe calls, `persist-credentials: false`, Publish repository guards, and Verify wiring. Keep eight restore slots synchronized with `RESTORE_BATCH_SIZE`.
- Planner stdout is only `GITHUB_OUTPUT` assignments; diagnostics go to stderr. Declare new planner outputs in the producing job's `outputs:` block.

## Cache and publication

- `scripts/package_sources.py` fingerprints patched recipe folders, every Git repository actually built, and audited consumer-specific inputs; standalone sources use their `rolling` commit. Kernel defaults/certificates affect only the kernel.
- The kernel also consumes the sibling VPP recipe and its two Git sources through accel-PPP. Preserve nested discovery/pinning and the checked Intel reset-to-HEAD adaptation; a cleanup script must not override a planned checkout with `origin/main`. Cache metadata on inaccessible refs is diagnostic only, never a restore candidate.
- Source caches exclude shared builders, build images, local workflows/helpers, unrelated recipes/patches/data, and catalog dependencies. Run **Repository** with `force_rebuild` to apply local build-policy/tooling changes or refresh unlocked APT/toolchains; ordinary republishing can reuse cached binaries. Also force publication after signing-credential rotation.
- `scripts/legacy_package_cache.py` proves recipe caches from producer manifests, historical patched trees, and successful build-step checkout evidence; standalone caches use recorded repository commits. Missing/ambiguous evidence means rebuilding, never substituting today's refs or workflow submodule pins. Metadata-service outages fail planning. Save migrated source-key caches only after restore/output checks.
- Publication tracking (`scripts/publish_manifest.py`) is separate from compilation. Only deployed Pages `input-manifest.json` advances the baseline, not local candidates. New manifests use schema v2; preserve v1 readability for legacy proofs.
- Do not run `scripts/build_repo.sh` as a local test: it moves `packages/rolling` into `_site/deb` and signs the repository after Jekyll. Use `scripts.test_build_repo` fixtures instead.

## Focused checks

- Python 3.11+; helpers/tests use only the standard library. Fixtures need Git, Bash, gzip, and bzip2; repository-assembly fixtures fake Debian scanners, checksum tools, and GPG.
- All tests: `python3 -m unittest discover -s scripts -v`.
- One module: `python3 -m unittest scripts.test_package_build_policy -v`; append `.ClassName.test_method` for one case.
- Scoped hooks: `pre-commit run --files <changed-files>`. Changes under `scripts/`, `.github/workflows/`, or `.github/actions/` trigger the entire test suite.
- Workflow validation: `actionlint .github/workflows/*.yaml`.
- Known baseline lint findings, do not "fix": actionlint's unknown `ubuntu-26.04` label; Ruff's two `TRY004`s in `scripts/package_catalog.py` and `scripts/publish_manifest.py` (retain the `ValueError` validation contract).
- Debian build-mode tests skip without `dpkg-buildpackage` and `make`; macOS success does not exercise real Debian builds. Static package validation needs `dpkg` and `dpkg-deb`.
- Upstream audits: `VYOS_BUILD_ROOT=/path/to/patched/vyos-build/scripts/package-build python3 -m unittest scripts.test_prepare_package_build.UpstreamRecipeTests scripts.test_package_sources.UpstreamSourceTests -v`. Temporary copies only, no upstream compilation; skips without `VYOS_BUILD_ROOT`.
