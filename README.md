# vyos-pkg

Independent package repository for VyOS builds.

> [!CAUTION]
> This project is an **independent fork of VyOS®**.
> It is **not affiliated with, endorsed by, or sponsored by VyOS Networks Corporation** by any means.
> VyOS® is a registered trademark of VyOS Networks Corporation.

## Publishing

The scheduled publish workflow compares its recorded inputs with `input-manifest.json` in the deployed Pages site. The manifest records this repository's revision (including workflow/site configuration), the patch repository revision (including its pinned submodules), the resolved build-image digest, each package/architecture's source fingerprint and resolved external revisions, its dependency list, and a hash of the public signing-key file. Matching inputs skip builds, cache restoration, verification, and publication. A missing, unreadable, or invalid published manifest causes the workflow to proceed normally.

The manifest is deployed together with the repository, so a failed build or deployment cannot advance the published baseline. Runs are serialized to avoid overlapping publications. Publication planning is separate from compilation: workflow, site, signing-key, or build-image changes can republish restored packages without rebuilding their unchanged sources.

Package caches are **source-specific**, keyed by build group, source package, architecture, and a fingerprint calculated by `scripts/package_sources.py`:

- Recipe packages track the complete `vyos-build/scripts/package-build/<name>/` tree after applying the downstream patches, plus the resolved commits of every Git repository the recipe builds. The `vyos-build` revision remains the one pinned by `techbymatt/tbm-vyos-patch`.
- Nested source clones are tracked too: for example, FRR builds both libyang and FRR, Kea also clones its packaging repository, and the kernel's accel-PPP build consumes both VPP and `vyos-vpp-patches` from the sibling VPP recipe.
- Audited out-of-folder inputs affect only their consumer. The kernel tracks its effective `kernel_version`/`kernel_flavor`, the public certificates it embeds, and the consumed VPP recipe tree, rather than the entire upstream `data/` or package-build trees.
- Standalone packages track their external repository's `rolling` commit.

The planner resolves branches, tags (including annotated tags), and abbreviated commits to full commits. Recipe builds use those exact planned revisions, even if a moving ref advances before compilation. Original ref labels are retained for package-version metadata. Changes to unrelated recipes, shared builders, build images, local workflows/helpers, or catalog prerequisites do not invalidate a package's source cache. A new or evicted source cache requires a build; `force_rebuild` remains an explicit override. CI logs report each source's build/restore decision.

Kernel source pinning also adapts its nested VPP builder and prevents Intel driver cleanup from resetting a planned tag checkout to `origin/main`. Adding these previously untracked VPP inputs changes only kernel fingerprints and requires one initial kernel build; other matching source caches remain reusable.

Existing namespaced caches are migrated conservatively by `scripts/legacy_package_cache.py`. It verifies the producer's inputs, historical patched recipe and scoped configuration, and recorded source checkouts before reuse. Verified archives are restored, checked, and saved under their source-specific key. Sources whose old checkouts cannot be proven (for example, moving branches whose commits were never logged) need an initial build. Schema-v1 producer manifests remain readable; new publications use schema v2 with complete source descriptors. Metadata-service outages fail planning instead of scheduling mass rebuilds.

Cache visibility is separate from cache existence: workflows can restore their own branch's caches and default-branch caches, but the default branch cannot restore caches saved only on a feature branch. The planner preserves this isolation and explains inaccessible matching caches, legacy migration bootstrap builds, and actual source changes (including changed versions and checkout SHAs) in its final decisions.

Live APT repositories and downloaded toolchains are not locked by source fingerprints. To refresh those inputs, or deliberately apply local build-policy/tooling changes to unchanged sources, run **Repository** manually with **force_rebuild** enabled. This bypasses all package caches and republishes after verification; later runs reuse the refreshed caches. Also use this option after rotating signing credentials, and update the checked-in public key when changing signing identity. Moving recipe Git refs are detected automatically.

Cache hits are restored in batches of up to eight packages per runner. Publication is gated on static validation of every built and restored `.deb`: required control metadata, target architecture (or `all`), readable control and payload archives, and payload checksums when `md5sums` is supplied. Archive members are read without extracting files or following symlinks on the host. Different bytes for the same package/version/architecture are rejected; identical duplicates are permitted. These checks detect structural corruption and internal inconsistencies, not authenticity or runtime correctness.

Lintian runs without installing the built packages. Its output is advisory only and is saved in a `lintian-report` artifact. Both architectures are checked on a single runner without a privileged build container. Installation and maintainer-script testing belong to the separate VyOS image-build repository; publication verification does not execute package scripts or resolve dependencies.

### Architecture-independent packages

The amd64 jobs own all `Architecture: all` packages. Mixed sources build both amd64 and independent binaries on amd64, and only architecture-specific binaries on arm64. Independent-only sources are classified in `scripts/package_catalog.json` and omitted from the arm64 build and restore matrices. Both Publish and Test use this policy; verification requires only the architectures planned for that run.

`scripts/prepare_package_build.py` applies checked, recipe-specific adaptations after the downstream VyOS patches. It retains source builds for the shared builder, changes binary build modes for overridden recipes, skips the separate VICI build on arm64, and uses apkg to prepare libyang's source before invoking dpkg with an explicit binary build mode. An upstream change to an audited command fails preparation and requires reviewing the adaptation. Custom architecture-specific builders, including the kernel and its architecture-dependent firmware, retain their native build targets.

Fresh and restored outputs are checked using Debian control metadata before upload. Arm64 may not emit `Architecture: all`, and independent-only sources may not emit architecture-specific binaries. Verify also rejects independent packages from arm64 artifacts, even if their bytes match an amd64 copy. Restored packages must satisfy the current output policy. When adding or updating a recipe, review all its binary outputs and nested build commands, update the policy/adaptations as needed, and test both native architectures. Use an explicit forced rebuild when changing local policy/adaptations requires different outputs from unchanged sources.

## Adding packages

### 1. Choose the build path

Add an entry with the **source recipe/repository name**, which may differ from the names of the `.deb` packages it produces, to the appropriate group in `scripts/package_catalog.json`:

| Build path                 | Catalog group | Source and build command                                            | Dependency configuration                                   |
| -------------------------- | ------------- | ------------------------------------------------------------------- | ---------------------------------------------------------- |
| VyOS build recipe          | `build`       | `vyos-build/scripts/package-build/<name>/`; runs `python3 build.py` | The recipe's dependency declarations and preparation steps |
| Standalone VyOS repository | `build-extra` | `vyos/<name>` on its `rolling` branch; runs `dpkg-buildpackage`     | The catalog entry's `deps` array                           |

For a VyOS build recipe, ensure the recipe is present in the `vyos-build` revision pinned by `techbymatt/tbm-vyos-patch`, including any required downstream patches. Adding a catalog entry does not create the upstream recipe.

For a standalone repository, ensure it has working Debian packaging and a `rolling` branch. Declare additional packages to install in its catalog entry. For example:

```json
{ "name": "my-package", "deps": ["libexample-dev", "pkg-config"] }
```

Keep required build dependencies declared in the source's Debian packaging too. The catalog installs prerequisites; it does not replace `debian/control`.

### 2. Classify all binary outputs

Inspect `debian/control`, generated control files, and any nested packaging scripts. Classify the entire source recipe, including documentation, Python modules, and other secondary packages:

| Outputs                                                             | Required policy change                                                     |
| ------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Architecture-specific packages for both amd64 and arm64             | None; both architectures are planned by default.                           |
| A mixture of architecture-specific and `Architecture: all` packages | Use the default `dual` classification and the build modes described below. |
| Only `Architecture: all` packages                                   | Set `"architecture": "independent_only"` on the source entry.              |
| Architecture-specific packages supported only on amd64              | Set `"architecture": "amd64_only"` on the source entry.                    |

For example, `{"name": "my-package", "architecture": "independent_only"}` belongs in `build-extra` for an independent-only standalone repository. Omitting `architecture` defaults to `dual`. Arm64-only sources require extending the planner; the current policy supports dual-architecture and amd64-only scheduling.

### 3. Check the build command

Standard recipes using the shared builder's default command, and standalone repositories using the workflow's direct `dpkg-buildpackage` command, already receive the correct build mode. A mixed source builds its independent packages on amd64 and its architecture-specific packages on both architectures.

If the recipe overrides `build_cmd`, calls another packaging script, or uses another packaging tool, review that path explicitly. If it can produce independent packages:

- Add a checked, recipe-specific adaptation to `scripts/prepare_package_build.py`.
- Select `--build=binary` on amd64 and `--build=any` on arm64 for binary-only Debian builds. Preserve source output where needed, as the shared builder does with `full` and `source,any`.
- Skip independent-only sub-builds on arm64 before they execute. Do not build duplicate packages and then delete them before upload.
- Preserve architecture-specific build steps and dependencies between sub-builds.

Use the existing net-snmp, FRR/libyang, and strongSwan adaptations as examples. Standalone builds call the same helper with `--group build-extra --root packages`; its `prepare_extra` function repairs package-specific Debian rules before building. For example, vyatta-biosdevname's legacy rules put its architecture-specific packaging commands under `binary-indep`, so the adaptation moves them to `binary-arch` and adds the missing build targets. Exact replacements intentionally fail if the expected upstream layout changes; review and update the adaptation when upgrading that recipe.

Audit source discovery as well as compilation. Repositories declared with `scm_url`/`commit_id` in `package.toml` are tracked automatically. If a custom builder or nested hook clones another repository, add audited discovery and pinning support in `scripts/package_sources.py`; Kea's packaging clone is an example. Add any out-of-folder source/configuration inputs only to the consuming recipe's descriptor. Source pinning also uses checked replacements and fails on upstream command drift.

### 4. Validate and enable publication

1. Add focused tests for new classifications, custom adaptations, and source tracking in `scripts/test_package_catalog.py`, `scripts/test_package_build_policy.py`, `scripts/test_prepare_package_build.py`, and `scripts/test_package_sources.py` as appropriate. Include new adapted recipes in the optional upstream-checkout checks.
2. Run the [local checks](#local-checks). To check adaptations against actual recipes, set `VYOS_BUILD_ROOT` to a VyOS checkout's `scripts/package-build` directory after applying the downstream patches.
3. Run the **Test** workflow from the branch containing your changes:
   - For a VyOS recipe, set `package` to its source recipe name.
   - For a standalone repository, set `package-extra` to its repository name and supply any additional dependencies through the comma-separated `deps` input. Test uses that input rather than the catalog dependencies.
   - Leave the unused package input empty. Multiple names can be supplied as a comma-separated list. The shared policy automatically selects the architectures to test.
4. Inspect the artifacts for the complete expected set of binary packages. Mixed sources should have their `all` packages only in the amd64 artifact; independent-only sources should have no arm64 job. The output checks enforce architecture ownership and nonempty outputs, but do not prove that every expected binary package was produced.
5. Once the changes are merged, run **Repository** or let its schedule publish them. New or changed package sources build while matching source caches restore. Use `force_rebuild` when deliberately bypassing caches, such as to refresh live dependencies or apply local build-policy changes to unchanged sources.

The Test workflow accepts package names directly, including sources not yet in the catalog. Unknown sources default to both architectures.

## Workflow structure

- `scripts/plan_builds.py` handles input parsing, catalog expansion, cache selection, restore batching, and publication planning. It emits job outputs directly; there is no intermediate TSV or jq matrix filter.
- `scripts/package_sources.py` discovers scoped source inputs, fingerprints patched recipes, resolves Git refs, and pins recipe checkouts. `scripts/legacy_package_cache.py` proves old cache inputs during migration.
- `build-recipe.yaml` and `build-standalone.yaml` own execution for both Publish and Test. Publish supplies pinned revisions and enables caching; Test disables caching.
- The `package-paths` and `package-artifacts` actions share cache/upload paths and output checks with `restore-package`.
- `verify-packages.yaml` validates unmerged artifacts together, using the expected architecture set. Lintian produces one advisory report for the run.
- `scripts/build_repo.sh` assembles and signs `rolling/main` after Jekyll. Missing binary inputs and index-generation/signing failures stop publication. Its fixture tests use disposable directories and fake signing tools.

## Local checks

Run the local checks with:

```sh
python3 -m unittest discover -s scripts -v
pre-commit run --all-files
actionlint .github/workflows/*.yaml
```

The Python tools and tests use Python 3.11+ and the standard library. Source fixtures also need Git. Repository-assembly fixture tests additionally use Bash, GNU checksum tools, gzip, and bzip2. Static validation requires `dpkg` and `dpkg-deb`; the verification runners provide these tools. On Debian hosts with `dpkg-dev` and `make`, the tests also build small source packages to check the actual binary build modes. Set `VYOS_BUILD_ROOT` to a patched VyOS checkout's `scripts/package-build` directory to test build-mode adaptations and source pinning against that checkout; the tests operate on temporary copies without compiling upstream packages.
