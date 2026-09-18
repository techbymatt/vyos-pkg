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

### Local checks

Run the local checks with:

```sh
python3 -m unittest discover -s scripts -v
pre-commit run --all-files
actionlint .github/workflows/publish.yaml .github/workflows/test.yaml
```

The tests use Python 3.11+ and the workflow's existing `jq` tool. Static validation requires `dpkg` and `dpkg-deb`; the verification runners provide these tools. On Debian hosts with `dpkg-dev` and `make`, the tests also build a small mixed source package to check the actual binary build modes. Set `VYOS_BUILD_ROOT` to a patched VyOS checkout's `scripts/package-build` directory to test the recipe adaptations against that checkout; the tests operate on temporary copies.
