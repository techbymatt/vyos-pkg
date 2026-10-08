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
| `scripts/package_sources.py` | Patched/historical recipe readers, bounded Git resolution, checked source pinning and its CLI. |
| `scripts/package_cache.py` | Source-key prefixes and newest-first visible key indexing. |
| `scripts/legacy_package_cache.py` | Conservative v2-cache proofs from producer artifacts, historical recipes and successful build-step checkout evidence. |
| `scripts/prepare_package_build.py` | Audited binary command registry and recipe/standalone preparation hooks. |
| `scripts/checked_edits.py` | Validate/stage every exact replacement before writing upstream files. |
| `scripts/package_build_policy.py` | Enforce a source's binary-output ownership using Debian control metadata. |
| `scripts/package_layout.py` | Artifact names, ordered paths, composite-action output encoding and file-only cleanup. |
| `scripts/validate_packages.py` | Static metadata/archive/checksum validation and global binary-identity checks. |
| `scripts/report_lintian.py` | Bounded parallel scans, process-group cleanup, ordered reporting and completeness status. |
| `scripts/workflow_utils.py` | Canonical JSON, bounded noninteractive commands, atomic writes and safe job-output encoding. |
| `scripts/build_repo.sh` | Assemble/sign the repository only after Jekyll and successful Verify. |

The existing command entry points remain usable directly or through `python3 -m
scripts.<module>`. `package_sources.py` retains the checkout helper imported by
adapted upstream builders; do not break that import when moving implementation
code. Manifests continue to use schema v2 and remain able to read v1 proofs.

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

Nested clones require both **discovery and pinning** in `package_sources.py`.
Declared `scm_url`/`commit_id` repositories are automatic; shell/custom clones
are not. Audit their exact commands and fail on drift. Scope external files only
to their consumer. The kernel's VPP recipe/repos and certificates/defaults, Kea's
packaging clone, and Intel's cleanup reset are existing examples.

Execution must remain: downstream patches → build-mode preparation → source
pinning (Publish) → build. Keep original ref labels for version metadata; checkout
only the planned full commits. Never compile duplicate independent outputs on
arm64 and delete them afterward.

## CI efficiency and limits

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
verification. App secrets are explicitly passed only to recipe workflow calls,
and untrusted source checkouts must not persist credentials.
