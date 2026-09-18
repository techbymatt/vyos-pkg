# vyos-pkg

Independent package repository for VyOS builds.

> [!CAUTION]
> This project is an **independent fork of VyOS®**.
> It is **not affiliated with, endorsed by, or sponsored by VyOS Networks Corporation** by any means.
> VyOS® is a registered trademark of VyOS Networks Corporation.

## Publishing

The scheduled publish workflow compares its recorded inputs with `input-manifest.json` in the deployed Pages site. The manifest records this repository's revision (including workflow/site configuration), the patch repository revision (including its pinned submodules), the resolved build-image digest, each package/architecture revision and dependency list, and a hash of the public signing-key file. Matching inputs skip builds, cache restoration, verification, and publication. A missing, unreadable, or invalid published manifest causes the workflow to proceed normally.

The manifest is deployed together with the repository, so a failed build or deployment cannot advance the published baseline. Runs are serialized to avoid overlapping publications. The first run after these changes rebuilds packages under the new cache-key namespace.

Package cache keys include a namespace covering only build-affecting inputs: the build-image digest, the patch tree, shared upstream build inputs, upstream build data, the build and build-extra jobs, the restore-package composite, `scripts/package_dependencies.sh`, and the package build policy and recipe adaptations. Edits to verification or publication logic do not change the namespace, so they no longer rebuild packages.

Live APT repositories and moving source refs inside upstream build recipes are not locked by this manifest. To refresh those inputs, run **Repository** manually with **force_rebuild** enabled. This bypasses all package caches and republishes after verification; later runs can reuse the refreshed caches. Also use this option after rotating signing credentials, and update the checked-in public key when changing signing identity.

Cache hits are restored in batches of up to eight packages per runner. Publication is gated on static validation of every built and restored `.deb`: required control metadata, target architecture (or `all`), readable control and payload archives, and payload checksums when `md5sums` is supplied. Archive members are read without extracting files or following symlinks on the host. Different bytes for the same package/version/architecture are rejected; identical duplicates are permitted. These checks detect structural corruption and internal inconsistencies, not authenticity or runtime correctness.

Lintian runs without installing the built packages. Its output is advisory only and is saved in a `lintian-report` artifact. Both architectures are checked on a single runner without a privileged build container. Installation and maintainer-script testing belong to the separate VyOS image-build repository; publication verification does not execute package scripts or resolve dependencies.

### Architecture-independent packages

The amd64 jobs own all `Architecture: all` packages. Mixed sources build both amd64 and independent binaries on amd64, and only architecture-specific binaries on arm64. Independent-only sources are listed in `scripts/package_build_policy.json` and omitted from the arm64 build and restore matrices. Both Publish and Test use this policy; Test skips verification for architectures with no planned builds.

`scripts/prepare_package_build.py` applies checked, recipe-specific adaptations after the downstream VyOS patches. It retains source builds for the shared builder, changes binary build modes for overridden recipes, skips the separate VICI build on arm64, and uses apkg to prepare libyang's source before invoking dpkg with an explicit binary build mode. An upstream change to an audited command fails preparation and requires reviewing the adaptation. Custom architecture-specific builders, including the kernel and its architecture-dependent firmware, retain their native build targets.

Fresh and restored outputs are checked using Debian control metadata before upload. Arm64 may not emit `Architecture: all`, and independent-only sources may not emit architecture-specific binaries. Verify also rejects independent packages from arm64 artifacts, even if their bytes match an amd64 copy. Policy/helper changes invalidate caches, preventing reuse of old dual-producer artifacts. When adding or updating a recipe, review all its binary outputs and nested build commands, update the policy/adaptations as needed, and test both native architectures.

## Adding packages

### 1. Choose the build path

Add the **source recipe/repository name**, which may differ from the names of the `.deb` packages it produces, to the appropriate array in the **Plan builds from the cache state** step of `.github/workflows/publish.yaml`:

| Build path                 | Publish array    | Source and build command                                            | Dependency configuration                                   |
| -------------------------- | ---------------- | ------------------------------------------------------------------- | ---------------------------------------------------------- |
| VyOS build recipe          | `packages`       | `vyos-build/scripts/package-build/<name>/`; runs `python3 build.py` | The recipe's dependency declarations and preparation steps |
| Standalone VyOS repository | `extra_packages` | `vyos/<name>` on its `rolling` branch; runs `dpkg-buildpackage`     | `scripts/package_dependencies.sh`                          |

For a VyOS build recipe, ensure the recipe is present in the `vyos-build` revision pinned by `techbymatt/tbm-vyos-patch`, including any required downstream patches. Adding a name to this repository's array does not create the upstream recipe.

For a standalone repository, ensure it has working Debian packaging and a `rolling` branch. Add a case to `scripts/package_dependencies.sh` if the build needs additional packages installed. For example:

```sh
  my-package) echo "libexample-dev pkg-config" ;;
```

Keep required build dependencies declared in the source's Debian packaging too. The mapping installs prerequisites; it does not replace `debian/control`.

### 2. Classify all binary outputs

Inspect `debian/control`, generated control files, and any nested packaging scripts. Classify the entire source recipe, including documentation, Python modules, and other secondary packages:

| Outputs                                                             | Required policy change                                                                                                    |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Architecture-specific packages for both amd64 and arm64             | None; both architectures are planned by default.                                                                          |
| A mixture of architecture-specific and `Architecture: all` packages | None in the JSON policy; ensure the builder honors the build modes described below.                                       |
| Only `Architecture: all` packages                                   | Add the source name to `independent_only.build` or `independent_only.build-extra` in `scripts/package_build_policy.json`. |
| Architecture-specific packages supported only on amd64              | Add the source name to `amd64_only.build` or `amd64_only.build-extra`.                                                    |

For example, an independent-only standalone repository belongs in `independent_only.build-extra`, alongside `live-boot` and `vyos-live-build`. The policy keys are literal JSON keys, and their values are arrays of source names. Arm64-only sources require extending the planner; the current policy supports dual-architecture and amd64-only scheduling.

### 3. Check the build command

Standard recipes using the shared builder's default command, and standalone repositories using the workflow's direct `dpkg-buildpackage` command, already receive the correct build mode. A mixed source builds its independent packages on amd64 and its architecture-specific packages on both architectures.

If the recipe overrides `build_cmd`, calls another packaging script, or uses another packaging tool, review that path explicitly. If it can produce independent packages:

- Add a checked, recipe-specific adaptation to `scripts/prepare_package_build.py`.
- Select `--build=binary` on amd64 and `--build=any` on arm64 for binary-only Debian builds. Preserve source output where needed, as the shared builder does with `full` and `source,any`.
- Skip independent-only sub-builds on arm64 before they execute. Do not build duplicate packages and then delete them before upload.
- Preserve architecture-specific build steps and dependencies between sub-builds.

Use the existing net-snmp, FRR/libyang, and strongSwan adaptations as examples. Standalone builds call the same helper with `--group build-extra --root packages`; its `prepare_extra` function repairs package-specific Debian rules before building. For example, vyatta-biosdevname's legacy rules put its architecture-specific packaging commands under `binary-indep`, so the adaptation moves them to `binary-arch` and adds the missing build targets. Exact replacements intentionally fail if the expected upstream layout changes; review and update the adaptation when upgrading that recipe.

### 4. Validate and enable publication

1. Add focused tests for new policy entries and custom adaptations in `scripts/test_package_build_policy.py` and `scripts/test_prepare_package_build.py`. Include new adapted recipes in the optional upstream-checkout test's package list.
2. Run the [local checks](#local-checks). To check adaptations against actual recipes, set `VYOS_BUILD_ROOT` to a VyOS checkout's `scripts/package-build` directory after applying the downstream patches.
3. Run the **Test** workflow from the branch containing your changes:
   - For a VyOS recipe, set `package` to its source recipe name.
   - For a standalone repository, set `package-extra` to its repository name and supply any additional dependencies through the comma-separated `deps` input. Test uses that input rather than the Publish dependency mapping.
   - Leave the unused package input empty. Multiple names can be supplied as a comma-separated list. The shared policy automatically selects the architectures to test.
4. Inspect the artifacts for the complete expected set of binary packages. Mixed sources should have their `all` packages only in the amd64 artifact; independent-only sources should have no arm64 job. The output checks enforce architecture ownership and nonempty outputs, but do not prove that every expected binary package was produced.
5. Once the changes are merged, run **Repository** or let its schedule publish them. The new input manifest includes the package, and policy/helper changes automatically invalidate incompatible caches. `force_rebuild` is only needed when deliberately bypassing caches, such as to refresh moving upstream inputs.

The Test workflow accepts package names directly, so adding a package does not require maintaining a second static package list there.

## Local checks

Run the local checks with:

```sh
python3 -m unittest discover -s scripts -v
pre-commit run --all-files
actionlint .github/workflows/publish.yaml .github/workflows/test.yaml
```

The tests use Python 3.11+ and the workflow's existing `jq` tool. Static validation requires `dpkg` and `dpkg-deb`; the verification runners provide these tools. On Debian hosts with `dpkg-dev` and `make`, the tests also build a small mixed source package to check the actual binary build modes. Set `VYOS_BUILD_ROOT` to a patched VyOS checkout's `scripts/package-build` directory to test the recipe adaptations against that checkout; the tests operate on temporary copies.
