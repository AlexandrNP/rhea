"""Cross-process handle plumbing.

Rhea's launch-agent path pickles the agent handle to Redis under
``agent_handle:<run_id>-<tool_id>`` so other MCP-server processes can
fetch + bind it without re-launching the agent. This module owns the
fetch side.

Academy-py 0.4 migration
------------------------
In academy-py 0.2 the wire types were split:

* ``RemoteHandle`` — bound to a specific exchange client (publishable).
* ``UnboundRemoteHandle`` — what pickle.loads returned in a fresh
  process; had to be ``.bind_to_client(client)``-ed.

In academy-py 0.4 the two were unified into a single ``Handle`` class
with an optional ``exchange`` field. A pickle round-trip across
processes deposits a ``Handle`` whose ``exchange`` is unset; the
consumer rebinds by constructing a new ``Handle(agent_id,
exchange=client)``. The unbound-vs-bound distinction is now just
"whether the exchange field is populated."
"""

import asyncio
import pickle
import time

from academy.handle import Handle
from redis import Redis


async def get_handle_from_redis(
    tool_id: str, run_id: str, r: Redis, timeout: float = 30.0
) -> Handle | None:
    """Poll Redis for the agent handle written by ``launch_agent``.

    Returns an exchange-less ``Handle`` (the cross-process pickled
    state). The caller binds it to its own exchange client via
    ``Handle(returned.agent_id, exchange=client)``.

    Returns ``None`` if the handle didn't appear within ``timeout``
    seconds — the caller should fall through to a launch path.
    """
    interval = 0.1
    deadline = time.time() + timeout
    while True:
        data = r.get(f"agent_handle:{run_id}-{tool_id}")
        if data is not None:
            result: Handle = pickle.loads(data)  # type: ignore[assignment]
            return result
        if time.time() > deadline:
            return None
        await asyncio.sleep(interval)
