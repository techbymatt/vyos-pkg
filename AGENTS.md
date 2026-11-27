# Repository guidance

`CLAUDE.md` symlinks here. See the [package checklist](README.md#adding-packages) and [pipeline map](doc/architecture.md).

When committing, use Conventional Commit subjects (e.g. `fix(ci): retry source lookups`).

## Build sources

- `build` recipes live in `techbymatt/tbm-vyos-patch`'s pinned `vyos-build/scripts/package-build/<name>/`; `build-extra` sources are `vyos/<name>` repositories on `rolling`.
- `scripts/package_catalog.json` names **sources**, not binaries. Add with `python3 scripts/package_catalog.py add`; update membership/deps in `scripts/test_package_catalog.py`, not YAML package lists.
- Run **Test** from your branch with `package=<recipe>` or `package-extra=<repo>` (comma-separated lists accepted). Test skips caches and Publish pins; recipes use the patch repo's default branch, and standalone `deps` comes from the input, not the catalog. Unknown sources build on both arches; Go is automatic for unknown recipes, opt-in for catalogued ones.
- Default `dual` includes mixed outputs; `independent_only` and `amd64_only` run only on amd64. No arm64-only policy exists. Skip `Architecture: all` sub-builds on arm64; don't delete duplicates afterward.
- Recipe order: downstream patches → `prepare_package_build.py` → `package_sources.py pin` (Publish) or `package_sources.py retry-clones` (Test) → build. Keep `package_sources.checkout_source` importable via `workflow/scripts`. Pin full commits without changing version labels; Intel cleanup must reset to `HEAD`, not `origin/main`.
- Audit custom build commands in `prepare_package_build.py` and nested clones in both discovery (`recipe_inputs.py`) and pinning (`source_pinning.py`). Use `CheckedEdits` so upstream drift fails before writing. Retry transport, not whole builds or signature verification. Exhausted kernel key lookups must abort: the upstream package loop catches `CalledProcessError` and otherwise continues.

## Local checks

- Match CI: `python3 scripts/package_catalog.py validate`, then `python3 -m unittest discover -s scripts -v` (Python 3.11+, stdlib). Inspect one matrix offline with `python3 scripts/plan_builds.py test --packages linux-kernel`.
- Focused: `python3 -m unittest -v scripts.test_package_build_policy`; append `.PolicyTests.test_empty_outputs_fail` to the module for one case. `pre-commit run --files <changed-files>` runs the full suite for script or workflow/action changes.
- Fixtures need Git, Bash, gzip and bzip2; assembly fakes scanners, checksums and GPG. Debian build-mode tests skip without `dpkg-buildpackage` and `make`; validation needs `dpkg`/`dpkg-deb`.
- Upstream audit (copies only, no compilation): `VYOS_BUILD_ROOT=/path/to/patched/vyos-build/scripts/package-build python3 -m unittest scripts.test_prepare_package_build.UpstreamRecipeTests scripts.test_package_sources.UpstreamSourceTests -v`.
- **Don't smoke-test `scripts/build_repo.sh` in this checkout**: it moves `packages/rolling` into `_site/deb` and signs after Jekyll. Use `python3 -m unittest scripts.test_build_repo -v`.
- `actionlint .github/workflows/*.yaml` may flag `ubuntu-26.04`; keep the runner labels. Existing Ruff findings include `FURB162` (legacy timestamps) and `TRY004` (catalog/manifest validators must keep raising `ValueError`).

## CI and publication contracts

- `plan_builds.py` orchestrates lookups; keep `build_matrix.py` and `source_identity.py` pure. Planner stdout is `GITHUB_OUTPUT` assignments only. Log to stderr and declare outputs on the producing job.
- Fingerprint patched recipe trees, actual Git commits and scoped inputs (kernel defaults, certificates, and historical sibling VPP via accel-PPP when built from source). Local tooling/policy, images and catalog deps don't change source keys. Use **Repository** with `force_rebuild` after signing rotation or when local policy/toolchain or live APT changes must reach the binaries.
- Legacy proofs use producer manifests and historical inputs, never today's refs. `legacy_package_cache.py` depends on checkout logs inside the successful `Build the package` step for recipes; standalone hits use recorded commits. Unproven hits rebuild; cache metadata outages fail planning. Inaccessible caches are diagnostic only.
- Fresh/restored builds share the path, artifact and gzip actions. Preserve `package_layout.py`'s ordered paths and historical recipe metadata glob; clean files only, never source trees or external symlink targets. Save migrations only after an exact hit and output checks. Keep eight restore slots aligned with `build_matrix.RESTORE_BATCH_SIZE`.
- Keep Verify downloads unmerged as `deb-<source>-<arch>`. Both callers pass `verify-plan`/`verify-arches` as `expected-artifacts`/`architectures`. This catches missing producers, not missing binaries; inspect native Test artifacts.
- Verify is static: never install, run maintainer scripts or extract onto the host. Lintian findings are advisory; failed/incomplete scans fail Verify. Keep its `--total-timeout 3600` within the 60-minute step.
- YAML contracts live in `scripts/test_test_workflow.py`. Keep reusable builds, explicit App secrets for recipe calls only and `persist-credentials: false`. Publication stays serialized and gated on `techbymatt/vyos-pkg`, `rolling` and successful Verify.
- Only deployed Pages `input-manifest.json` advances the baseline. Preserve canonical source encoding/cache keys and schema-v1 reads for legacy proofs; write schema v2.
