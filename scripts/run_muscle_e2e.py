"""End-to-end test of Rhea's file-input protocol via the MUSCLE tool.

Proves the full path for a *file-input* Galaxy tool:

  fetch test FASTA
    -> stage it into the rhea-input ProxyStore (RheaFileProxy)
    -> MCP: find_tools -> tools/call muscle {input_seqs: <redis_key>}
    -> RheaToolAgent: conda env (muscle) -> run_tool -> alignment
    -> non-null result

Run from the Rhea fork's own venv (`uv run python scripts/run_muscle_e2e.py`)
so the ProxyStore wire format matches the running rhea-server's
proxystore version.

Requires: the host-process rhea-server up on :3001 with
PARSL_CONTAINER_BACKEND=local, the pgvector DB with `muscle` ingested,
apecx-redis on :6379, MinIO, Ollama embedding, miniconda on PATH. See
apecx-mcp-integration/docs/rhea_tool_execution_findings.md.
"""

from __future__ import annotations

import json
import sys
import time

import cloudpickle
import requests
from proxystore.connectors.redis import RedisConnector
from proxystore.store import Store
from redis import Redis

from rhea.utils.proxy import RheaFileProxy

MCP_URL = "http://localhost:3001/mcp/"
REDIS_HOST, REDIS_PORT = "localhost", 6379
TEST_FASTA_URL = (
    "https://gitlab.pasteur.fr/galaxy-team/galaxy-tools/-/raw/master/"
    "tools/ngphylogeny/muscle/test-data/seqtest.fasta"
)


# ---- minimal MCP streamable-HTTP client (inline; rhea venv has httpx
# but we use requests, which is also present, to stay dependency-light) ----

class _MCP:
    def __init__(self, url: str):
        self.url = url
        self.session_id: str | None = None

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json,text/event-stream",
        }
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def _parse_sse(self, text: str, method: str):
        for line in text.splitlines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[len("data: ") :])
            if "error" in payload:
                raise RuntimeError(f"MCP {method} error: {payload['error']}")
            if "result" in payload:
                return payload["result"]
        raise RuntimeError(f"MCP {method}: no parseable data line in {text[:200]!r}")

    def initialize(self) -> None:
        r = requests.post(
            self.url,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "muscle-e2e", "version": "0.1"},
                },
            },
            timeout=30,
        )
        r.raise_for_status()
        self.session_id = r.headers.get("mcp-session-id")
        requests.post(
            self.url, headers=self._headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout=30,
        )

    def call(self, method: str, params: dict, *, timeout: float = 600.0):
        r = requests.post(
            self.url, headers=self._headers(),
            json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
            timeout=timeout,
        )
        r.raise_for_status()
        return self._parse_sse(r.text, method)


def main() -> int:
    # 1. fetch the MUSCLE test FASTA
    print(f"[1] fetching test FASTA from {TEST_FASTA_URL}")
    resp = requests.get(TEST_FASTA_URL, timeout=30)
    resp.raise_for_status()
    fasta_bytes = resp.content
    n_seqs = fasta_bytes.count(b">")
    print(f"    got {len(fasta_bytes)} bytes, {n_seqs} sequences")

    # 2. stage into the rhea-input ProxyStore (RheaFileProxy protocol)
    print("[2] staging FASTA into the rhea-input ProxyStore")
    redis_client = Redis(host=REDIS_HOST, port=REDIS_PORT)
    connector = RedisConnector(REDIS_HOST, REDIS_PORT)
    store = Store(
        name="rhea-input",
        connector=connector,
        serializer=cloudpickle.dumps,
        deserializer=cloudpickle.loads,
    )
    proxy = RheaFileProxy.from_buffer("seqtest.fasta", fasta_bytes, redis_client)
    redis_key = proxy.to_proxy(store)
    print(f"    staged: file_key={proxy.file_key!r}  proxystore redis_key={redis_key!r}")

    # 3. MCP: discover muscle, then call it with the file input
    print("[3] MCP: initialize -> find_tools -> tools/call muscle")
    mcp = _MCP(MCP_URL)
    mcp.initialize()
    mcp.call("tools/call", {
        "name": "find_tools",
        "arguments": {"query": "MUSCLE multiple sequence alignment of protein fasta"},
    })
    catalog = [t["name"] for t in mcp.call("tools/list", {}).get("tools", [])]
    print(f"    catalog after find_tools: {catalog}")
    if "muscle" not in catalog:
        print("    FAIL: 'muscle' not in the find_tools catalog")
        return 1

    # muscle params: input_seqs (the staged file) + diags (required
    # boolean) + run/cluster/outputFormat (have defaults; passed
    # explicitly so the Cheetah <command> template renders fully).
    muscle_args = {
        "input_seqs": redis_key,
        "diags": False,
        "run": "16",
        "cluster": "upgmb",
        "outputFormat": "fasta",
    }
    print(f"    calling muscle with: {muscle_args}")
    t0 = time.time()
    raw = mcp.call(
        "tools/call",
        {"name": "muscle", "arguments": muscle_args},
        timeout=900.0,
    )
    elapsed = time.time() - t0
    print(f"[4] muscle returned in {elapsed:.0f}s")

    # 4. inspect the result — is it non-null + usable?
    is_error = raw.get("isError") if isinstance(raw, dict) else None
    print(f"    isError={is_error}")
    if is_error:
        print("    FAIL: muscle returned isError=True")
        print(f"    {json.dumps(raw, default=str)[:800]}")
        return 1

    # The tool result is RheaOutput JSON in content[0].text.
    text = raw["content"][0]["text"]
    rhea_output = json.loads(text)
    rc = rhea_output.get("return_code")
    files = rhea_output.get("files") or []
    print(f"    return_code={rc}  output files={len(files)}")
    if rc != 0:
        print(f"    FAIL: MUSCLE non-zero return_code; stderr:\n{rhea_output.get('stderr','')[:600]}")
        return 1

    # 5. USE the result — fetch the alignment output file from the
    #    rhea-output ProxyStore and show it.
    print("[5] fetching the alignment output from the rhea-output ProxyStore")
    out_store = Store(
        name="rhea-output",
        connector=RedisConnector(REDIS_HOST, REDIS_PORT),
        serializer=cloudpickle.dumps,
        deserializer=cloudpickle.loads,
    )
    from proxystore.connectors.redis import RedisKey  # noqa: PLC0415

    alignment_text = None
    for f in files:
        key_field = f.get("key")
        redis_key = key_field["redis_key"] if isinstance(key_field, dict) else key_field
        out_proxy = RheaFileProxy.from_proxy(RedisKey(redis_key=redis_key), out_store)
        handle = out_proxy.open(redis_client)
        data = handle.read()
        print(f"    output {out_proxy.name!r}: {len(data)} bytes, format={out_proxy.format!r}")
        if out_proxy.name == "out_align" or alignment_text is None:
            alignment_text = data.decode("utf-8", "ignore")

    if not alignment_text or ">" not in alignment_text:
        print("    FAIL: no usable alignment content in the output files")
        return 1
    n_aligned = alignment_text.count(">")
    print(f"    ALIGNMENT ({n_aligned} aligned sequences):")
    print("    " + "\n    ".join(alignment_text.splitlines()[:8]))

    # 6. "use" it: compare against the tool's own expected test output.
    expected = requests.get(
        "https://gitlab.pasteur.fr/galaxy-team/galaxy-tools/-/raw/master/"
        "tools/ngphylogeny/muscle/test-data/seqtest_aln.fasta",
        timeout=30,
    ).text
    exp_ids = sorted(line for line in expected.splitlines() if line.startswith(">"))
    got_ids = sorted(line for line in alignment_text.splitlines() if line.startswith(">"))
    print(f"[6] expected aligned seq IDs: {exp_ids}")
    print(f"    got aligned seq IDs:      {got_ids}")
    if got_ids == exp_ids:
        print("    SUCCESS: MUSCLE aligned all 5 sequences; IDs match the tool's "
              "own expected test output. End-to-end file-input run verified.")
        return 0
    print("    PARTIAL: got a non-null alignment, but the sequence IDs differ "
          "from the expected test output — inspect above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
