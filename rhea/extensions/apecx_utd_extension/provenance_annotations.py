"""Rhea → apecx determinism/provenance annotations (E2-R Priority 2).

Surfaces the determinism-pinning metadata a Galaxy ``Tool`` already
carries — but which the base MCP ``tools/list`` payload DROPS — into a
single structured ``apecx_provenance`` block that rides on the MCP
``ToolAnnotations`` field (``extra="allow"``, so this does NOT break the
MCP schema). A nanobrain-side discovery client
(``nanobrain.library.tools.rhea_discovery.RheaMCPDiscovery``) reads the
block back to build an HONEST Unified Tool Descriptor: the real tool
version (not a hardcoded ``1.0.0``), the container image ref/digest (not
``None``), and a determinism class derived from real evidence (not a
blanket ``R3``).

Why a separate block instead of mapping onto the standard
``ToolAnnotations`` hints (readOnlyHint / openWorldHint / ...): those are
coarse booleans with no place for a version string, a container ref, or
the file-input-param list. We keep them free for their intended use and
namespace ours under one ``apecx_provenance`` key.

Pure-Python, no nanobrain import — the dict shape is the cross-framework
contract (same discipline as ``utd_producer``). Unit-testable against a
constructed ``rhea.utils.schema.Tool`` with no running server.

### The file-vs-JSON discriminator (load-bearing)

A Galaxy ``<param type="data">`` (a FILE input) serializes into the MCP
``inputSchema`` as an indistinguishable ``{"type": "string"}`` — there is
NO marker in the base inputSchema that says "this string is a file path /
redis key". A downstream synthesizer therefore CANNOT tell a file tool
(needs ``RheaFileToolStep`` + ProxyStore staging) from a JSON tool (needs
``ToolExecutionStep``) from the inputSchema alone. ``file_input_args`` is
that missing discriminator: the list of input parameter names whose
Galaxy type is ``data``. Empty list = a pure JSON tool. Absent block =
the worker predates this change and the synthesizer must FAIL LOUD rather
than guess.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Local import kept lazy-friendly: callers pass a constructed Tool, so the
# only hard dependency is the schema module (already a Rhea dependency).
from rhea.utils.schema import Param, Tool

#: Key under ``ToolAnnotations`` (extra-allow) carrying this block.
APECX_PROVENANCE_KEY = "apecx_provenance"

#: Schema version of the block — lets the reader evolve the contract.
APECX_PROVENANCE_SCHEMA = 1


def _normalized_param_name(param: Param) -> str | None:
    """Mirror ``Param.to_python_parameter``'s name resolution.

    The MCP ``inputSchema`` property keys are the python-parameter names
    (``argument`` stripped of leading hyphens when ``name`` is empty), so
    ``file_input_args`` must use the SAME normalization or the synthesizer
    can't match them against the inputSchema properties.
    """
    name = param.name
    if (not name) and param.argument is not None:
        name = param.argument.replace("--", "")
    if not name:
        return None
    return name.lstrip("-") or None


def _iter_all_params(tool: Tool) -> List[Param]:
    """Every param the tool exposes: top-level, conditional, sectioned.

    ``process_user_inputs`` (rhea/server/utils.py) routes ``type="data"``
    params from all three locations through ProxyStore staging, so all
    three are genuine file inputs and must appear in ``file_input_args``.
    """
    params: List[Param] = list(tool.inputs.params or [])
    for cond in tool.inputs.conditionals or []:
        params.append(cond.param)
        for when in cond.whens or []:
            params.extend(when.params or [])
    for section in tool.inputs.sections or []:
        params.extend(section.params or [])
    return params


def file_input_args(tool: Tool) -> List[str]:
    """Names of every ``type="data"`` (file) input parameter.

    De-duplicated, order-preserving. This is the file-vs-JSON
    discriminator the synthesizer branches on.
    """
    seen: Dict[str, None] = {}
    for param in _iter_all_params(tool):
        if param.type == "data":
            name = _normalized_param_name(param)
            if name and name not in seen:
                seen[name] = None
    return list(seen.keys())


def build_apecx_provenance(tool: Tool) -> Dict[str, Any]:
    """Extract the determinism/provenance block from a Galaxy ``Tool``.

    The returned dict is JSON-serializable and rides on
    ``ToolAnnotations`` (extra-allow). Every field is sourced from the
    tool's own metadata — nothing is fabricated. A field the tool does
    not carry is surfaced as an empty value, NOT a plausible-looking
    default, so the reader can tell "unknown" from "known".
    """
    requirements = []
    containers = []
    reqs = tool.requirements
    if reqs is not None:
        for req in reqs.requirements or []:
            requirements.append(
                {"type": req.type, "name": req.value, "version": req.version}
            )
        for cont in reqs.containers or []:
            containers.append({"type": cont.type, "value": cont.value})

    return {
        "schema": APECX_PROVENANCE_SCHEMA,
        # Real tool version. Empty string => unpinned (the reader emits a
        # descriptor_id with an explicit ``@unpinned`` rather than a false
        # ``@1.0.0``).
        "tool_version": tool.version or "",
        "requirements": requirements,
        "containers": containers,
        "version_command": tool.version_command or "",
        # The file-vs-JSON discriminator. Empty list = pure JSON tool.
        "file_input_args": file_input_args(tool),
        # Galaxy XML carries no determinism flag. Bioinformatics Galaxy
        # tools are overwhelmingly deterministic algorithms, so the
        # honest default is False; a wrapper for a known-stochastic tool
        # (sampling, ML, random seeds) should set this True so the reader
        # classifies it R3 even when containerized. Surfaced explicitly so
        # the contract is visible, never a hidden assumption.
        "stochastic": False,
    }


__all__ = [
    "APECX_PROVENANCE_KEY",
    "APECX_PROVENANCE_SCHEMA",
    "build_apecx_provenance",
    "file_input_args",
]
