# Package pipeline architecture

This checkout orchestrates builds; it does **not** own VyOS recipes. Recipes live
in `techbymatt/tbm-vyos-patch`'s pinned `vyos-build` submodule. Standalone sources
are `vyos/<name>` repositories on `rolling`.

## Data flow

```text
package_catalog.json
        │
        ├── Test inputs ──────────────┐
        │                            │
        └── patched recipe discovery │
                │                    │
        parallel Git ref resolution  │
                │                    │
        canonical source descriptors │
                │                    │
        deployed manifest comparison │
                │                    │
        visible cache / legacy proof │
                └──────────┬─────────┘
                           │
                    build_matrix.py
                 ┌─────────┴─────────┐
        native reusable builds   batched cache restore
                 └─────────┬─────────┘
                  shared output policy
                           │
                  unmerged deb artifacts
                           │
             static validation + advisory Lintian
                           │
             repository assembly / signing / Pages
                           │
               deployed input-manifest.json
```

Only the deployed manifest advances the publication baseline. A locally written
candidate is not evidence of a successful deployment. Test never accesses
package caches or publishes; a forced Publish never selects a cache hit.

## Module boundaries

| Module | Responsibility |
| --- | --- |
| `scripts/package_catalog.py` | Strict JSON validation, membership, immutable `Source` definitions, CLI scaffolding. |
| `scripts/build_matrix.py` | Deterministic native matrices, execution settings, build priorities, restore batches and complete producer plans. |
| `scripts/plan_builds.py` | Git/GitHub/Pages boundaries, manifest comparison, diagnostics and `GITHUB_OUTPUT`. |
| `scripts/source_identity.py` | Pure descriptor validation and canonical source hashing. No cloning/build policy. |
| `scripts/recipe_inputs.py` | Patched/historical recipe readers and scoped source discovery; no network access. |
| `scripts/source_resolver.py` | Bounded Git ref resolution: one ls-remote snapshot per URL, commit-API expansion for abbreviated hashes, concurrency-limited. |
| `scripts/source_pinning.py` | Audited checkouts and pins applied to planned full commits without changing version labels. |
| `scripts/package_sources.py` | Compatibility facade and CLI (`pin`, `retry-clones`, `checkout`); re-exports the historical API for upstream builders. |
| `scripts/network_retry.py` | Shared bounded backoff with tool-specific transient error checks. |
| `scripts/git_network.py` | Scoped Git transport timeouts and retries; clean staging for clone attempts. |
| `scripts/kernel_keys.py` | Retry lookup of the kernel recipe's public signing keys. |
| `scripts/package_cache.py` | Source-key prefixes and newest-first visible key indexing. |
| `scripts/legacy_package_cache.py` | Conservative v2-cache proofs from producer artifacts, historical recipes and successful build-step checkout evidence. |
| `scripts/prepare_package_build.py` | Audited binary command registry and recipe/standalone preparation hooks. |
| `scripts/checked_edits.py` | Validate/stage every exact replacement before writing upstream files. |
| `scripts/package_build_policy.py` | Enforce a source's binary-output ownership using Debian control metadata. |
| `scripts/package_layout.py` | Artifact names, ordered paths, composite-action output encoding and file-only cleanup. |
| `scripts/validate_packages.py` | Static metadata/archive/checksum validation and global binary-identity checks. |
| `scripts/report_lintian.py` | Bounded parallel scans, process-group cleanup, ordered reporting and completeness status. |
| `scripts/workflow_utils.py` | Canonical JSON, bounded noninteractive commands, atomic writes, safe job-output encoding and the shared CLI error boundary. |
| `scripts/build_repo.sh` | Assemble/sign the repository only after Jekyll and successful Verify. Release entries hash each file's size and digests in one sorted traversal. |

The existing command entry points remain usable directly or through `python3 -m
scripts.<module>`. `package_sources.py` is the compatibility facade: the
implementation lives in `recipe_inputs.py`, `source_resolver.py` and
`source_pinning.py`. It retains the checkout helper imported by adapted upstream
builders (`from package_sources import checkout_source`); do not break that
import. Manifests continue to use schema v2 and remain able to read v1 proofs.

## Adding a standard source

1. Ensure its patched recipe or standalone Debian repository already exists.
2. Add a catalog entry manually or with `package_catalog.py add`.
3. Classify **all** binary outputs. The default `dual` schedules amd64 and arm64;
   `independent_only` and `amd64_only` schedule amd64 only. There is no arm64-only
   policy yet.
4. For recipes needing Go, set `go: true`. Other known recipes skip setup-go.
   Override `timeout_minutes` (1–360) when necessary; use `priority` (0–100) for
   costly Publish builds. Equal-priority builds preserve catalog order; Test
   preserves requested source order.
5. Pin membership/dependencies and add focused tests. Run local checks, optional
   upstream audits, then the native Test workflow.

No package-specific YAML matrix or duplicate architecture list is needed.
Catalog dependencies install standalone prerequisites; they do not replace
`debian/control`. Manual Test instead uses its explicit comma-separated `deps`
input, even for catalogued sources. Unknown Test sources default to dual, and
unknown recipes retain Go setup for compatibility with the previous environment.

See [README.md's full checklist](../README.md#adding-packages).

## Extending custom builders

For a simple overridden Debian command, register a `BinaryCommand` in
`BINARY_COMMANDS` in `scripts/prepare_package_build.py`:

```python
"my-recipe": BinaryCommand("dpkg-buildpackage -us -uc -tc -b"),
```

Its `path` defaults to `package.toml`; its `count` defaults to one. Nondefault
paths and repeated commands must be explicit. The registry supplies the native
binary mode without modifying unrelated command fragments. More complex tools
use `RECIPE_HOOKS` or `EXTRA_HOOKS`, with edits staged through `CheckedEdits`.
The optional preparation audits enumerate the registry rather than maintain a
second package list.

Nested clones require both **discovery** (`recipe_inputs.py`) and **pinning**
(`source_pinning.py`).
Declared `scm_url`/`commit_id` repositories are automatic; shell/custom clones
are not. Audit their exact commands and fail on drift. Scope external files only
to their consumer. The kernel's VPP recipe/repos and certificates/defaults, Kea's
packaging clone, and Intel's cleanup reset are existing examples.

Execution must remain: downstream patches → build-mode preparation → source
pinning (Publish) → build. Keep original ref labels for version metadata; checkout
only the planned full commits. Never compile duplicate independent outputs on
arm64 and delete them afterward.

Both Test and Publish route audited recipe clones (including the kernel, nested
VPP, and Kea) through `git_network.py`. Publish also uses it for Git ref lookups,
metadata clones, and fetches of missing pinned commits. Git aborts stalled HTTP
transfers after 60s below 1 byte/s; each command also has a wall-clock limit.
While preparing the kernel recipe, the GPG key lookup is also adapted to retry
transient Web Key Directory (WKD) failures, with a 90s limit per attempt. Both
helpers use up to three attempts, waiting 5s and 15s between attempts. A failed
key lookup stops the kernel build; tarball downloads and signature verification
remain unchanged. Retries do not change Git URLs/revisions or the GPG key source.

## CI efficiency and limits

### Build network diagnostics

**Probe build network** (`probe-build-network.yaml`) checks direct IPv4 access
from the native build containers by default. Enable `probe-ipv6` when manually
running **Probe build network** to test both IPv4 and IPv6. Publish and pull
request probes are IPv4-only. Publish calls it once after the cache check, in
parallel with builds/restores, using the resolved build image and patch
revision. Unchanged runs skip diagnostics; failed planning still triggers them,
falling back to the rolling image or patch default branch if those inputs could
not be resolved. Builds, Verify and Publish do not depend on the probe result.
These are separate runners, not measurements from each package build's runner.

The reusable workflow declares `image` and `build-ref` inputs and accepts only
the two named App secrets. It remains manually dispatchable and runs on
same-repository pull requests that change the probe. The inventory reads the pinned,
patched recipes and submodule URLs, plus the catalog's standalone repos. It does
not resolve external source refs. The private checkout and its dedicated probe
use read-only App tokens scoped to the patch repository; other probes are
anonymous. If the patch checkout fails, the jobs still test the independently
known targets and flag the missing recipe inventory.

The probe requests Git's ref advertisement, WKD policies and public keys on both
hosts, the kernel signature, APT index URLs from the container, and the
endpoints listed in `scripts/build_probe_resources.json`. Large archives use
HEAD. Keep the audited Boost, kernel-driver and VPP download URLs in that file
in sync with the recipes; registry URLs are reachability samples, not dependency
locks. The probe does not fetch Git objects or recursively resolve dependencies
of Cargo, Go, pip, RubyGems or APT, and cannot diagnose registry pulls or GitHub
Actions downloads that fail before the jobs start. A 401, 403 or 405 still shows
the network path answered; it does not prove the build can use that resource.

The run summary records attempted and connected IPs, exit codes, HTTP responses,
DNS/TCP/TLS timings and redirects, with cloud/region taken from the jobs' setup
logs across all API pages, including reusable-workflow job name prefixes.
Results are diagnostic only and do not change build or publication gates.

### Build and publication limits

- Source discovery and isolated-index patching are serial/memoized. Up to eight
  independent URL lookups run concurrently; refs for each URL share one snapshot
  and clone. All lookups must finish successfully before cache routing.
- Unchanged publications and forced rebuilds skip cache-list API requests.
  Metadata-service outages otherwise fail planning, never cause mass rebuilding.
- Source-cache keys are indexed once, newest first. Inaccessible ref metadata is
  diagnostic only. Legacy archives need independent proof before reuse.
- Restore entries contain only the fields actually consumed by the actions.
  Up to eight entries share a native runner. The fixed YAML slots and
  `RESTORE_BATCH_SIZE` are checked together; do not change only one side.
- Publish uses four concurrent jobs per build group, Test eight. Only selected Go
  recipes install the toolchain, and cache hits bypass preparation/build steps.
- Verify uses four archive-validation workers and four Lintian workers on one
  unprivileged runner. Validation uses one required-field query per `.deb` and
  compares identities across every producer. Lintian schedules large archives
  first but preserves input/report order. Findings are advisory; missing,
  failed, timed-out or unscanned packages are failures.
- Four disjoint repository indexes scan/compress concurrently. Every worker is
  reaped and must succeed before release hashing/signing.
- Relevant PR/push checks run fixtures without an upstream build image or
  secrets. Cancel stale checks, not serialized publication runs.
- Matrices fail explicitly if they exceed GitHub's 256-job limit. Larger catalogs
  will need workflow sharding, not silent truncation of required producers.

These are structural reductions in redundant work, not guaranteed wall-clock
speedups. Source build cost, APT/network performance and runner contention still
determine end-to-end duration.

## Compatibility and safety contracts

Source fingerprints cover patched recipe files, every actual Git source and
audited consumer-specific inputs. They exclude build images, local helpers,
shared builders, dependencies, Go setup, priority and timeouts. Existing v3 keys
remain usable. Use `force_rebuild` to apply changed local policy/toolchains to
unchanged sources or refresh live APT inputs.

Cache path **order and scope** are part of the GitHub cache version. Keep binary
paths first and the historical recipe metadata wildcard intact. Builders and
restore jobs share the gzip-compatibility action; do not independently choose
compression in one workflow. Save migrated keys only after exact-hit/output
checks. Restore cleanup removes matching files only, never source trees or
external symlink targets.

Verify downloads remain unmerged in `deb-<source>-<arch>` directories. Both
callers pass the exact producer list and architecture set from the planner.
Cache misses during restoration therefore cannot disappear behind another
successful producer. These checks do not prove every expected binary package
was built; review that set during the native Test workflow.

Verification neither installs packages nor executes maintainer scripts. Payload
and control members are read as archive data, not extracted onto the host.
Identical binary identities may repeat only when their SHA256 bytes agree;
`Architecture: all` belongs exclusively to amd64 producers.

Do not run `build_repo.sh` against this checkout as a local test. Its fixtures
use disposable inputs and fake Debian scanners/checksum/signing tools. Production
publication remains repository-gated, serialized, and conditional on successful
verification. App secrets are explicitly passed to recipe and network-diagnostic
workflow calls, never standalone builds. Untrusted source checkouts must not
persist credentials.
