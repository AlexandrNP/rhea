"""Rhea → UTD producer (T-RH-02 minimum).

Converts Rhea's MCP tool registry (FastMCP ``Tool`` shapes) into
Unified Tool Descriptor dicts that match the wire format consumed by
``nanobrain.core.unified_tool_descriptor.UnifiedToolDescriptor.from_dict``.

Wire format only — this module does NOT import nanobrain. The dict
schema is the cross-framework contract; both sides agree on it.

Per ``apecx-mcp-integration/docs/tool_descriptor_contract.md §2`` and
``apecx-mcp-integration/docs/external_tool_integration.md``.

## Usage

    from rhea.server.rhea_fastmcp import RheaFastMCP
    from rhea.extensions.apecx_utd_extension.utd_producer import (
        rhea_tool_to_utd_dict,
        rhea_tools_to_utd_dicts,
    )

    # Inside a Rhea route handler that has the tool registry:
    tool_dicts = rhea_tools_to_utd_dicts(
        await fastmcp.list_tools(),
        backend="rhea",
        version="1.10.1",
    )

    # Each dict in tool_dicts is a UTD-shaped object that an apecx-side
    # consumer can pass to UnifiedToolDescriptor.from_dict() to get a
    # validated UTD instance.

## Honest scope (T-RH-02 minimum)

- One MCP Tool → one UTD. Output spec is a single ``"return"`` slot
  carrying the tool's outputSchema (or "Any" when unset).
- ``descriptor_id`` derived from the tool name. Sanitized to match
  the UTD grammar ``[a-z][a-z0-9_.]*``.
- ``provenance_pin.class_path`` is the Rhea-side plumbing
  ``rhea.extensions.apecx_utd_extension.dispatchers.RheaMCPDispatcher``
  (a sibling module that does the actual MCP call). The class doesn't
  exist yet — that's T-RH-03+ scope. The path is recorded so the
  apecx-side ``ToolBase.from_descriptor`` resolution will FAIL-FAST
  with a clear message until it's wired.
- No ``cost_estimate`` / ``failure_modes`` / ``version_history`` —
  Rhea doesn't carry these natively. Authors can override per-tool
  via the ``overrides`` parameter.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


_DESCRIPTOR_ID_TOOL_ID_RE = re.compile(r"[^a-z0-9_.]")


def _sanitize_tool_id(raw: str) -> str:
    """Conform to the UTD ``tool_id`` grammar: ``[a-z][a-z0-9_.]*``.

    Lowercases the input, replaces disallowed chars with ``_``, and
    prepends ``rhea_`` if the result doesn't start with ``[a-z]``.
    """
    lowered = raw.lower()
    sanitized = _DESCRIPTOR_ID_TOOL_ID_RE.sub("_", lowered)
    if not sanitized or not sanitized[0].isalpha():
        sanitized = "rhea_" + sanitized.lstrip("_.")
    return sanitized


def _input_specs_from_json_schema(
    input_schema: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert a JSON-Schema object (the MCP standard) to a list of
    UTD input specs.

    Honest limit: only handles ``"type": "object"`` schemas with a
    ``"properties"`` map. More elaborate schemas (oneOf / allOf /
    refs) collapse to a single ``"input"`` slot of type ``"Any"``.
    """
    if not isinstance(input_schema, dict):
        return []
    properties = input_schema.get("properties")
    if not isinstance(properties, dict) or input_schema.get("type") != "object":
        # Schema we don't fully understand → opaque single input.
        return [{"name": "input", "type": "Any", "description": "",
                 "required": True, "default": None}]
    required_set = set(input_schema.get("required", []) or [])
    specs: List[Dict[str, Any]] = []
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        type_name = prop_schema.get("type", "Any")
        # JSON-schema type names map cleanly:
        # string/integer/number/boolean/array/object/null. UTD uses
        # the JSON-schema vocabulary directly.
        specs.append({
            "name": prop_name,
            "type": type_name,
            "description": prop_schema.get("description", ""),
            "required": prop_name in required_set,
            "default": prop_schema.get("default"),
        })
    return specs


def _output_specs_from_json_schema(
    output_schema: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One ``"return"`` output slot whose type comes from the
    output schema's top-level ``"type"`` field."""
    if not isinstance(output_schema, dict):
        return [{"name": "return", "type": "Any", "description": ""}]
    type_name = output_schema.get("type", "Any")
    return [{"name": "return", "type": type_name,
             "description": output_schema.get("description", "")}]


def rhea_tool_to_utd_dict(
    mcp_tool: Any,
    *,
    backend: str = "rhea",
    version: str = "1.0.0",
    dispatcher_class_path: Optional[str] = None,
    side_effects: str = "none",
    determinism: str = "R3",
    resource_class: str = "cpu_light",
    **overrides: Any,
) -> Dict[str, Any]:
    """Convert ONE FastMCP ``Tool`` (or any object with ``name`` /
    ``description`` / ``inputSchema`` / ``outputSchema`` attributes)
    into a UTD-shaped dict.

    Args:
        mcp_tool: Any object with the FastMCP/MCP Tool attribute shape.
        backend: ``backend`` segment of the descriptor_id. Defaults
            to ``"rhea"`` since this is the rhea-side producer.
        version: Tool version segment of the descriptor_id. The
            T-RH-02 minimum doesn't track per-tool versions; the
            caller passes the Rhea server version (or the tool's own
            version if known).
        dispatcher_class_path: Dotted-path of the apecx-side dispatcher
            class that ``ToolBase.from_descriptor`` resolves. When
            None, defaults to the placeholder
            ``rhea.extensions.apecx_utd_extension.dispatchers.RheaMCPDispatcher``
            (T-RH-03 will ship that class).
        side_effects, determinism, resource_class: UTD operational
            classifications. Defaults are conservative; caller can
            override per-tool via ``**overrides`` or by passing
            different values here.
        **overrides: Any UTD field can be overridden — e.g. pass
            ``cost_estimate={...}``, ``failure_modes=[...]``, etc.

    Returns:
        A dict consumable by ``UnifiedToolDescriptor.from_dict``.
    """
    name = getattr(mcp_tool, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError(
            f"FAIL-FAST: rhea_tool_to_utd_dict: input has no usable "
            f"`name` attribute (got {type(mcp_tool).__name__!r})"
        )

    description = getattr(mcp_tool, "description", "") or ""
    title = getattr(mcp_tool, "title", "") or name
    input_schema = getattr(mcp_tool, "inputSchema", None)
    output_schema = getattr(mcp_tool, "outputSchema", None)

    tool_id = _sanitize_tool_id(name)
    if dispatcher_class_path is None:
        dispatcher_class_path = (
            "rhea.extensions.apecx_utd_extension.dispatchers.RheaMCPDispatcher"
        )

    # Split description into summary + long_description on first newline.
    description = description.strip()
    if description:
        parts = description.split("\n", 1)
        summary = parts[0].strip()
        long_description = parts[1].strip() if len(parts) > 1 else ""
    else:
        summary = title
        long_description = ""

    data: Dict[str, Any] = {
        "descriptor_id": f"{backend}:{tool_id}@{version}",
        "display_name": title,
        "summary": summary,
        "long_description": long_description,
        "inputs": _input_specs_from_json_schema(input_schema),
        "outputs": _output_specs_from_json_schema(output_schema),
        "side_effects": side_effects,
        "determinism": determinism,
        "resource_class": resource_class,
        "provenance_pin": {
            "class_path": dispatcher_class_path,
        },
    }
    data.update(overrides)
    return data


def rhea_tools_to_utd_dicts(
    mcp_tools: List[Any],
    **shared: Any,
) -> List[Dict[str, Any]]:
    """Bulk version: convert every tool in ``mcp_tools`` to UTD dicts.

    All ``**shared`` kwargs are forwarded per-tool to
    :func:`rhea_tool_to_utd_dict`. Convenient for emitting a full
    catalogue from ``await fastmcp.list_tools()``.
    """
    return [rhea_tool_to_utd_dict(t, **shared) for t in mcp_tools]
