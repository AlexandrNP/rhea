"""Unit tests for the apecx determinism/provenance annotations (E2-R P2).

Pure — constructs ``rhea.utils.schema.Tool`` objects in memory; needs no
DB / server / network. Verifies the block a nanobrain discovery client
reads to build an HONEST UTD: real version, container refs,
version_command, and the file-vs-JSON ``file_input_args`` discriminator.

The matching nanobrain-side reader test is
``nanobrain/tests/unit/test_rhea_discovery.py`` (the
``test_discover_reads_provenance_*`` cases). The live wire round-trip is
covered by the gated integration test
``nanobrain/tests/integration/test_rhea_synthesize_step_live.py``
(skipped unless ``$RHEA_MCP_URL`` is set).
"""

from __future__ import annotations

from rhea.extensions.apecx_utd_extension.provenance_annotations import (
    APECX_PROVENANCE_SCHEMA,
    build_apecx_provenance,
    file_input_args,
)
from rhea.utils.schema import (
    Command,
    Conditional,
    Container,
    Inputs,
    Macros,
    Outputs,
    Param,
    Requirement,
    Requirements,
    Section,
    Stdio,
    Tests as ToolTests,  # aliased: avoids pytest collecting the schema class
    Tool,
    When,
    Xrefs,
)


def _make_tool(
    *,
    version: str = "5.1.0",
    version_command: str = "muscle -version",
    requirements: list[Requirement] | None = None,
    containers: list[Container] | None = None,
    inputs: Inputs | None = None,
) -> Tool:
    return Tool(
        id="muscle",
        user_provided_name="MUSCLE",
        version=version,
        profile="21.05",
        description="Multiple sequence alignment",
        macros=Macros(),
        xrefs=Xrefs(xrefs=[]),
        requirements=Requirements(
            requirements=requirements
            if requirements is not None
            else [Requirement(type="package", version="5.1", value="muscle")],
            containers=containers
            if containers is not None
            else [
                Container(
                    type="docker",
                    value="quay.io/biocontainers/muscle:5.1--h9948957_0",
                )
            ],
        ),
        stdio=Stdio(regex=[]),
        version_command=version_command,
        command=Command(command="muscle"),
        inputs=inputs
        if inputs is not None
        else Inputs(
            params=[
                Param(name="input_seqs", type="data", format="fasta"),
                Param(name="perm", type="select", value="all"),
            ]
        ),
        outputs=Outputs(),
        tests=ToolTests(tests=[]),
    )


def test_build_surfaces_real_version_and_command():
    block = build_apecx_provenance(_make_tool())
    assert block["schema"] == APECX_PROVENANCE_SCHEMA
    assert block["tool_version"] == "5.1.0"
    assert block["version_command"] == "muscle -version"


def test_build_surfaces_requirements_and_containers():
    block = build_apecx_provenance(_make_tool())
    assert block["requirements"] == [
        {"type": "package", "name": "muscle", "version": "5.1"}
    ]
    assert block["containers"] == [
        {
            "type": "docker",
            "value": "quay.io/biocontainers/muscle:5.1--h9948957_0",
        }
    ]


def test_file_input_args_identifies_data_params():
    """A type='data' param IS the file-vs-JSON discriminator; a
    type='select'/'text' param is NOT."""
    block = build_apecx_provenance(_make_tool())
    assert block["file_input_args"] == ["input_seqs"]


def test_pure_json_tool_has_empty_file_input_args():
    """A tool with no data params reports an EMPTY list (explicitly a
    JSON tool), never an absent / fabricated value."""
    inputs = Inputs(
        params=[
            Param(name="query", type="text"),
            Param(name="limit", type="integer", value="5"),
        ]
    )
    block = build_apecx_provenance(_make_tool(inputs=inputs))
    assert block["file_input_args"] == []


def test_unpinned_tool_reports_empty_version_not_a_default():
    """A tool with no version surfaces '' so the reader can emit
    '@unpinned' rather than a false '@1.0.0'."""
    block = build_apecx_provenance(_make_tool(version="", version_command=""))
    assert block["tool_version"] == ""
    assert block["version_command"] == ""


def test_file_inputs_found_in_conditionals_and_sections():
    """``process_user_inputs`` stages data params from conditionals and
    sections too, so they must appear in ``file_input_args``."""
    inputs = Inputs(
        params=[Param(name="main_in", type="data")],
        conditionals=[
            Conditional(
                name="mode",
                param=Param(name="mode", type="select", value="advanced"),
                whens=[
                    When(
                        value="advanced",
                        params=[Param(name="extra_ref", type="data")],
                    )
                ],
            )
        ],
        sections=[
            Section(
                name="opts",
                title="Options",
                params=[Param(name="bg_model", type="data")],
            )
        ],
    )
    names = file_input_args(_make_tool(inputs=inputs))
    assert set(names) == {"main_in", "extra_ref", "bg_model"}


def test_argument_form_param_name_is_normalized():
    """A param declared via ``argument='--in_file'`` (no name) resolves to
    the same key ``Param.to_python_parameter`` / the MCP inputSchema use:
    ``argument`` with the leading ``--`` stripped."""
    inputs = Inputs(params=[Param(argument="--in_file", type="data")])
    assert file_input_args(_make_tool(inputs=inputs)) == ["in_file"]
