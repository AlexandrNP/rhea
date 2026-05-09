"""APECx Unified Tool Descriptor (UTD) extension for Rhea.

**Scope:** Produces UTDs for tools registered in Rhea's catalogue, exposes
them through a new `/apecx/utds` endpoint, and accepts UTD-keyed
invocations through `/apecx/invoke`. Provenance + ProxyStore namespace
cooperation are sibling modules.

**Status:** Scaffold only. The full implementation is broken into nine
tasks (T-RH-01 through T-RH-09) per
`apecx-mcp-integration/docs/implementation_task_graph.md` Track C.
The first three (UTD producer, discovery endpoint, invoke endpoint)
are the minimum viable integration with apecx-mcp.

**Design contract:** see
`apecx-mcp-integration/docs/tool_descriptor_contract.md` (UTD schema)
and `apecx-mcp-integration/docs/external_tool_integration.md` (Rhea
integration architecture).

**Why a separate extensions/ package:** to keep the apecx-side code
clearly separated from upstream Rhea so periodic merges from
`upstream/main` are mechanical. No upstream Rhea code is modified by
this fork; we only add new files under `rhea/extensions/apecx_utd_extension/`
plus (eventually) two new HTTP routes registered through Rhea's existing
extension hook (TBD — needs investigation in T-RH-03).
"""
