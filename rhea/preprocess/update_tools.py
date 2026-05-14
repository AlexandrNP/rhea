"""update_tools.py — ingest Galaxy ToolShed tools into Rhea's registry.

Before 2026-05-14 this module only *computed* the new-tool set and
stopped — the actual ingestion (fetch -> parse -> embed -> insert) was
never wired, so Rhea's ``galaxytools`` table stayed empty and
``find_tools`` had nothing to find. This is the finished ingestion
path.

Pipeline
--------

1. **Discover** — ``get_galaxy_repositories()`` queries the configured
   Galaxy ToolShed's ``/api/repositories`` catalog. (ToolShed URL is
   configurable via ``$GALAXY_TOOLSHED_URL`` — see fetch.py.)
2. **Fetch content** — for each repo, ``get_tool_xmls_from_repo()``
   pulls the tool XML files. The ToolShed's own anonymous file
   endpoints are auth-gated (403); ~76% of repos carry a GitHub
   ``remote_repository_url`` and the content is fetched from there.
   Repos without a GitHub remote URL are SKIPPED (logged, counted).
3. **Parse** — ``classify_xml_type`` filters to ``tool`` XML;
   ``Tool.from_xml`` parses each into a typed ``Tool``.
4. **Embed** — ``generate_tool_documentation_embedding`` produces a
   1024-dim vector via the configured embedding service
   (``$EMBEDDING_URL`` / ``$MODEL`` — OpenAI-compatible, so Ollama
   with a 1024-dim model like ``mxbai-embed-large`` works).
5. **Insert** — upsert a ``GalaxyTool`` row (``session.merge``, so a
   re-run is idempotent).

Bounded by ``$RHEA_INGEST_LIMIT`` (default 25) so a run is fast and
predictable; raise it (or set 0 = no limit) for a full catalog
ingest. FAIL-LOUD if zero tools were ingested — an empty registry
after an ingestion run is a silent-failure shape (``find_tools``
would return nothing with no error).

Config (env vars, same names ``Settings`` reads):
  DATABASE_URL, EMBEDDING_URL, EMBEDDING_KEY, MODEL,
  GALAXY_TOOLSHED_URL, GITHUB_TOKEN (optional, lifts the GitHub API
  rate limit), RHEA_INGEST_LIMIT.
"""

import os
import asyncio
import logging
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional

from openai import OpenAI
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)

from rhea.preprocess.utils.fetch import (
    get_galaxy_repositories,
    get_tool_xmls_from_repo,
)
from rhea.preprocess.utils.process_xml import classify_xml_type
from rhea.utils.schema import Tool
from rhea.utils.models import GalaxyTool, get_all_tool_ids
from rhea.utils.embedding import generate_tool_documentation_embedding

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_DEFAULT_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/rhea"
_DEFAULT_EMBEDDING_URL = "http://localhost:8000/v1"
_DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"


def _has_supported_forge(repo: Dict) -> bool:
    """True iff the repo's remote points at a forge fetch.py can read.

    fetch.py::get_tool_xmls_from_repo supports GitHub ``/tree/`` and
    GitLab ``/-/tree/`` URLs (incl. self-hosted GitLab). The ToolShed's
    own anonymous file endpoints are auth-gated, so a repo with no
    supported forge mirror cannot be ingested.
    """
    url = repo.get("remote_repository_url") or ""
    return "github.com" in url or "/-/tree/" in url


def get_candidate_repos(only: Optional[set] = None) -> List[Dict]:
    """Return ToolShed repos eligible for ingestion.

    Filters out suite definitions and deprecated repos, dedupes by
    (name, description) keeping the most-downloaded, and keeps only
    repos with a supported forge mirror (GitHub / GitLab — see
    fetch.py). Sorted by downloads desc so a bounded run ingests the
    most-used tools first.

    ``only`` — when given, restrict to repos whose ``name`` is in this
    set (case-insensitive). Lets a run target specific tools (e.g.
    ``RHEA_INGEST_ONLY=muscle``) regardless of download rank.
    """
    only_lc = {n.lower() for n in only} if only else None
    upstream: List[Dict] = get_galaxy_repositories()
    deduped: Dict = {}
    for repo in upstream:
        if repo.get("type") == "repository_suite_definition" or repo.get("deprecated"):
            continue
        if only_lc is not None and (repo.get("name") or "").lower() not in only_lc:
            continue
        if not _has_supported_forge(repo):
            continue
        key = (repo.get("name"), repo.get("description"))
        td = repo.get("times_downloaded", 0)
        if key not in deduped or td > deduped[key].get("times_downloaded", 0):
            deduped[key] = repo
    repos = sorted(
        deduped.values(),
        key=lambda r: r.get("times_downloaded", 0),
        reverse=True,
    )
    logger.info(
        "%d candidate repos (non-deprecated, non-suite, github-mirrored)",
        len(repos),
    )
    return repos


async def get_update_list(db_session: AsyncSession) -> set[str]:
    """Tool IDs present upstream but not yet in the local DB.

    An EMPTY local DB is treated as "nothing ingested yet" (every
    upstream tool is new) — NOT as an error. The prior version raised
    ``RuntimeError`` on an empty DB, which made a fresh-DB ingestion
    impossible. (This helper is informational; the ingestion loop in
    ``main`` upserts via ``session.merge`` so it is correct regardless.)
    """
    upstream_ids = {r["id"] for r in get_candidate_repos()}
    local_tool_list: List[str] | None = await get_all_tool_ids(db_session)
    local_ids = set(local_tool_list or [])
    new_ids = upstream_ids - local_ids
    logger.info("%d new repo IDs upstream vs %d local", len(new_ids), len(local_ids))
    return new_ids


async def ingest_repo(
    repo: Dict,
    db_session: AsyncSession,
    embedding_client: OpenAI,
    model: str,
) -> int:
    """Ingest every tool XML in one ToolShed repo. Returns # tools ingested.

    A per-tool failure (bad XML, embedding error) is logged and
    skipped — it does not abort the repo or the run. The caller
    FAIL-LOUDs only if the WHOLE run ingested zero tools.
    """
    remote_url = repo.get("remote_repository_url") or ""
    try:
        xmls = get_tool_xmls_from_repo(remote_url)
    except Exception as e:  # noqa: BLE001 — network/GitHub failures: log + skip repo
        logger.warning("repo %s: content fetch failed (%s); skipping", repo.get("name"), e)
        return 0

    ingested = 0
    for fname, xml_bytes in xmls:
        if classify_xml_type(xml_bytes) != "tool":
            continue
        try:
            tool: Tool = Tool.from_xml(ET.fromstring(xml_bytes.decode("utf-8", "ignore")))
        except Exception as e:  # noqa: BLE001
            logger.warning("  %s/%s: Tool.from_xml failed (%s); skipping", repo.get("name"), fname, e)
            continue
        try:
            embedding = generate_tool_documentation_embedding(tool, embedding_client, model)
        except Exception as e:  # noqa: BLE001
            logger.warning("  %s: embedding failed (%s); skipping", tool.id, e)
            continue

        gt = GalaxyTool(
            id=tool.id,
            name=tool.name or tool.user_provided_name,
            user_provided_name=tool.user_provided_name,
            description=tool.description,
            long_description=tool.long_description,
            documentation=tool.documentation,
            embedding=embedding,
        )
        gt.definition = tool  # setter -> _definition (JSONB)
        await db_session.merge(gt)  # upsert by PK (tool id) — idempotent re-run
        ingested += 1
        logger.info("  ingested tool %r (from repo %s)", tool.id, repo.get("name"))
    return ingested


async def main() -> None:
    database_url = os.environ.get("DATABASE_URL", _DEFAULT_DB_URL)
    embedding_url = os.environ.get("EMBEDDING_URL", _DEFAULT_EMBEDDING_URL)
    # OpenAI client rejects an empty api_key; local endpoints ignore it.
    embedding_key = os.environ.get("EMBEDDING_KEY") or "EMPTY"
    model = os.environ.get("MODEL", _DEFAULT_MODEL)
    try:
        limit = int(os.environ.get("RHEA_INGEST_LIMIT", "25"))
    except ValueError:
        limit = 25

    # RHEA_INGEST_ONLY — comma-separated repo names to target specifically
    # (e.g. "muscle"). Ignores the download-rank limit so a specific tool
    # can be ingested regardless of how popular it is.
    only_raw = os.environ.get("RHEA_INGEST_ONLY", "").strip()
    only = {n.strip() for n in only_raw.split(",") if n.strip()} or None

    engine: AsyncEngine = create_async_engine(database_url, echo=False, future=True)
    session_local: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    embedding_client = OpenAI(base_url=embedding_url, api_key=embedding_key)

    repos = get_candidate_repos(only=only)
    if only is None and limit > 0:
        repos = repos[:limit]
    logger.info(
        "Ingesting from %d repo(s) (only=%s limit=%s); embedding via %s model=%s",
        len(repos), sorted(only) if only else None, limit or "none",
        embedding_url, model,
    )

    total = 0
    async with session_local() as db_session:
        for repo in repos:
            total += await ingest_repo(repo, db_session, embedding_client, model)
        await db_session.commit()

    await engine.dispose()

    if total == 0:
        raise RuntimeError(
            "FAIL-LOUD: update_tools ingested ZERO tools. An empty registry "
            "after an ingestion run is a silent-failure shape — find_tools "
            "would return nothing with no error. Check: the ToolShed catalog "
            "fetch, the GitHub content fetch (rate limit? set $GITHUB_TOKEN), "
            "the embedding service at $EMBEDDING_URL, and the DB at "
            "$DATABASE_URL."
        )
    logger.info("update_tools: ingested %d tool(s) total.", total)


if __name__ == "__main__":
    asyncio.run(main())
