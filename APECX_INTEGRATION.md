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
