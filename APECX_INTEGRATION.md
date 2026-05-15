# APECx Integration Fork — Notes

This is a fork of `chrisagrams/rhea` maintained as part of the APECx
project. The fork adds a `rhea/extensions/apecx_utd_extension/` package
that exposes Rhea's tools through the Unified Tool Descriptor (UTD)
contract defined in `apecx-mcp-integration/docs/tool_descriptor_contract.md`.

## Branches

- `main` — tracks `upstream/main` (chrisagrams/rhea). Periodic mechanical
  merges from upstream land here. Do NOT add APECx code to `main`.
- `apecx-integration` — the development branch for the UTD extension.
  All APECx-side work happens here. PRs from feature branches land here.

## Upstream sync protocol

```bash
git checkout main
git pull upstream main
git push origin main
git checkout apecx-integration
git merge main
# Resolve conflicts; apecx-side files only live under
# rhea/extensions/apecx_utd_extension/, so conflicts should be rare.
```

## Ownership boundary

- **Files we add** live under `rhea/extensions/apecx_utd_extension/`.
- **Files we never touch:** anything under `rhea/agent/`, `rhea/manager/`,
  `rhea/server/` (Rhea proper), `rhea/preprocess/`, `rhea/utils/`.
  Modifying upstream Rhea code makes upstream merges painful and
  forces the apecx fork into permanent divergence — avoid.
- **One exception:** `rhea/server/app.py` (or wherever route registration
  happens) MAY need a single line added to register the apecx routes.
  Document the diff carefully if so; aim to upstream this one-liner.

### Authorized upstream-file modifications

These touch "files we never touch" — done under explicit user
authorization (2026-05-14: *"you are free to modify it"*; *"Rhea
should be using Galaxy toolshed, it should be configurable from the
same fork"*; *"we need to fix the ingestion path … fetch and use
correctly a tool"*; *"attempt to fix the issue while still using
parsl"*; *"test muscle tool … ensure that you can successfully run
it, get the results, and use them"*). They are kept small +
backward-compatible (optional kwargs, new Settings with prior-value
defaults, an additive Literal value, a session-lookup added *before*
the existing global lookup, and two narrow bug fixes to Galaxy
command-template rendering) so an upstream merge should not conflict.
Aim to upstream them.

- `rhea/server/schema.py` — `Settings` gains `galaxy_toolshed_url`
  (default `https://toolshed.g2.bx.psu.edu`); `parsl_container_backend`
  Literal gains `"local"`.
- `rhea/preprocess/utils/fetch.py` — `get_galaxy_repositories` /
  `get_tool_repository_tar` gain an optional `toolshed_url` kwarg;
  new `get_tool_xmls_from_repo` fetches tool XML from a repo's GitHub
  mirror (the ToolShed's own anonymous file endpoints are now
  auth-gated — 403). See "Configurable Galaxy ToolShed" below.
- `rhea/preprocess/update_tools.py` — **rewritten** to actually
  *ingest* tools (discover → fetch content → `Tool.from_xml` →
  embed → upsert `GalaxyTool`), bounded by `$RHEA_INGEST_LIMIT`,
  FAIL-LOUD on zero ingested. Before, `main()` only computed a
  new-tool set and stopped.
- `rhea/manager/parsl_config.py` — `generate_parsl_config` gains a
  `backend="local"` mode: a `LocalProvider` with the default
  `SingleNodeLauncher` (no container `WrappedLauncher`). The Parsl
  worker runs as a local subprocess of the server — required on
  Docker Desktop for Mac, where a sibling worker container cannot
  reach the interchange ("Never received handle from Parsl worker").
- `rhea/server/rhea_fastmcp.py` — `RheaToolManager.call_tool` now
  checks the **session-scoped** tool bucket before the global one,
  mirroring `list_tools`. Without this a `find_tools`-discovered tool
  is listable but not callable ("Unknown tool").
- `rhea/agent/tool.py` — `expand_galaxy_if`'s dotted-variable
  resolution loop no longer injects an empty `_nested` placeholder
  (`set_nested(key, "")`) when the `GalaxyVar` already wraps a real
  scalar value. The placeholder shadowed the real value at render
  time — e.g. `${outputFormat.value}` rendered empty, turning
  MUSCLE's `-${outputFormat.value}out` into an invalid `-out`.
- `rhea/agent/schema.py` — `GalaxyVar` now honors the Galaxy Cheetah
  idiom *"`$param.value` IS `$param` for a scalar param"* across the
  **mapping protocol** (`__getitem__`, `__contains__`, `get`), not
  just `__getattr__`. Cheetah's NameMapper resolves `.value` on a
  mapping-like object via `__getitem__`, so the `__getattr__`-only
  handling was never reached; `__getitem__("value")` fell through to
  an empty `GalaxyVar` and rendered `{}`.
- `rhea/agent/utils.py` — `install_conda_env` now (a) tears down a
  stale named env BEFORE `conda create` (conda silently no-ops on
  pre-existing env names + a `-y` flag — exit 0, env empty); (b) logs
  a loud warning when the strict pin fails and the relaxed `>=` spec
  is tried; (c) **always** verifies the env via `conda list --json`
  after install — refusing to keep an env where the requested package
  is missing OR its major version differs from the Galaxy XML
  request. This closes the MUSCLE 3.x → 5.x silent-failure: bioconda's
  default `muscle` is now v5, whose CLI is incompatible with the
  Galaxy tool XML's command template; before this fix, the env
  silently installed v5 and the workflow returned `Invalid command
  line / Unknown option in` errors at dispatch.
- `rhea/server/mcp_server.py` — the shutdown handler used to call
  `parsl.dfk().cleanup()` unconditionally; if startup failed BEFORE
  `parsl.load()` (port conflict, bad config, etc.) the cleanup itself
  raised `NoDataFlowKernelError("Must first load config")`, masking
  the original error. Now: guarded with a `try/except
  NoDataFlowKernelError` so the operator sees the actual startup
  error instead of the cascading red herring.
- `rhea/manager/parsl_config.py` — for the `local` backend the
  `HighThroughputExecutor`'s `interchange_launch_cmd` is now resolved
  to an ABSOLUTE path derived from `sys.executable`, not the default
  `['interchange.py']` (which goes through PATH). A stale Anaconda
  install at `/opt/anaconda3/bin/interchange.py` wins PATH ahead of
  Rhea's uv venv and ships an older incompatible script
  (`TypeError: Interchange.__init__() got an unexpected keyword
  argument 'worker_ports'`). The absolute path closes that PATH-
  leakage failure mode.

Full arc + the working host-process recipe + the verified MUSCLE
end-to-end run:
`apecx-mcp-integration/docs/rhea_tool_execution_findings.md`.

## Configurable Galaxy ToolShed (2026-05-14)

Rhea's tool registry is sourced from the **Galaxy ToolShed**.
Discovery uses the ToolShed `/api/repositories` catalog
(`get_galaxy_repositories`). **Tool content** is fetched from each
repo's GitHub mirror (`get_tool_xmls_from_repo`) — the ToolShed's own
anonymous `/repos/<owner>/<name>/` file endpoints (`/archive/` and
`/raw-file/`) now return `403 — Authentication required`, but ~76% of
ToolShed repos carry a GitHub `remote_repository_url`. The legacy
`get_tool_repository_tar` (the auth-gated tarball path) is retained
for ToolShed deployments that still allow anonymous archive access.

The ToolShed base URL was hardcoded; it is now **configurable**:

- **`$GALAXY_TOOLSHED_URL`** (or the `.env` file) — read by both
  `Settings.galaxy_toolshed_url` (the server's config surface) and
  `fetch.py` directly (so the `update_tools.py` preprocess script
  honors it without importing the server's `Settings`).
- Per-call override: `get_galaxy_repositories(toolshed_url=...)`.
- Resolution order: explicit arg → `$GALAXY_TOOLSHED_URL` → the
  default `https://toolshed.g2.bx.psu.edu`.

Point it at the Galaxy **test** ToolShed
(`https://testtoolshed.g2.bx.psu.edu`), a private mirror, or an
internal ToolShed without a code change.

**Status**: COMPLETE end-to-end. The ingestion path is FINISHED and
verified — real Galaxy tools ingested, `find_tools` semantic search
works, the Parsl worker-connectivity issue is fixed, the file-input
ProxyStore protocol works, and a real Galaxy tool (**MUSCLE**) was
fetched → discovered → called with a real FASTA file input →
executed → produced a non-null, *correct* alignment (a 1980-byte
FASTA whose 5 aligned sequence IDs match the tool's own expected
`seqtest_aln.fasta` test data). The end-to-end test is
`rhea/scripts/run_muscle_e2e.py`. Galaxy's `<repeat>` input element
and the `process_conditional_inputs` stub remain unimplemented (not
needed for MUSCLE — its one `<conditional>` works). Full analysis +
the reproducible host-process recipe + the verified MUSCLE run:
`apecx-mcp-integration/docs/rhea_tool_execution_findings.md`.

## Task tracking

Implementation tasks for this fork are in
`apecx-mcp-integration/docs/implementation_task_graph.md` Track C
(`T-RH-00` through `T-RH-09`). Cite the task ID in PR titles.

## Status (2026-05-09)

- T-RH-00 (fork creation, this commit): COMPLETE
- T-RH-01 (extension scaffold): COMPLETE — empty package created
- T-RH-02 (UTD producer): TODO — depends on G15 UTD primitive shipping
  in nanobrain or on a hand-rolled local UTD model
- T-RH-03 (discovery endpoint): TODO
- T-RH-04 (invoke endpoint): TODO
- T-RH-05 (ProxyStore namespace cooperation): TODO — depends on G13
- T-RH-06 (provenance record cooperation): TODO — depends on G4
- T-RH-07 (operator UTD overlay): TODO
- T-RH-08 (CI for UTD tests): TODO
- T-RH-09 (e2e integration test): TODO

The remaining tasks need Rhea's runtime infrastructure (Postgres,
Redis, MinIO, Parsl pool) to integration-test honestly. They will land
once the apecx-mcp-integration side is ready to consume them.
