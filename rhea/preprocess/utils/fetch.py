import requests
import json
import os
import re
from typing import List, Dict, Optional, Tuple
import io
import logging
import tarfile

logger = logging.getLogger(__name__)

# https://github.com/<owner>/<repo>/tree/<branch>/<path...>
_GITHUB_TREE_RE = re.compile(
    r"github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+?)/?$"
)

# https://<gitlab-host>/<group(/subgroup...)>/<project>/-/tree/<branch>/<path...>
# The "namespace" is everything between the host and "/-/tree/" — it can
# contain subgroups, so it is captured greedily and split later.
_GITLAB_TREE_RE = re.compile(
    r"https?://([^/]+)/(.+?)/-/tree/([^/]+)/(.+?)/?$"
)

# The default Galaxy ToolShed. The base URL is configurable so the fork
# can point at a private / mirror / test ToolShed without a code change.
# Resolution order for every fetch: explicit ``toolshed_url`` arg ->
# ``$GALAXY_TOOLSHED_URL`` -> this default. Mirrors the
# ``Settings.galaxy_toolshed_url`` field in rhea/server/schema.py.
_DEFAULT_TOOLSHED_URL = "https://toolshed.g2.bx.psu.edu"


def _resolve_toolshed_url(toolshed_url: str | None) -> str:
    """Resolve the ToolShed base URL (arg -> env -> default), no trailing slash."""
    resolved = toolshed_url or os.environ.get("GALAXY_TOOLSHED_URL") or _DEFAULT_TOOLSHED_URL
    return resolved.rstrip("/")


def get_galaxy_repositories(toolshed_url: str | None = None) -> List[Dict]:
    base = _resolve_toolshed_url(toolshed_url)
    url = f"{base}/api/repositories"
    logger.info(f"Fetching Galaxy repositories from {url}")

    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        logger.info(f"Successfully fetched {len(data)} repositories")
        return data
    except requests.RequestException as e:
        logger.error(f"Failed to fetch Galaxy repositories: {e}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise


def _github_tool_xmls(
    m: "re.Match", github_token: Optional[str]
) -> List[Tuple[str, bytes]]:
    """Fetch ``.xml`` files from a GitHub ``/tree/`` URL match."""
    gh_owner, gh_repo, branch, path = m.group(1), m.group(2), m.group(3), m.group(4)
    token = github_token or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    contents_url = (
        f"https://api.github.com/repos/{gh_owner}/{gh_repo}/contents/{path}"
        f"?ref={branch}"
    )
    logger.info("Listing tool XMLs from %s", contents_url)
    resp = requests.get(contents_url, headers=headers, timeout=30)
    resp.raise_for_status()
    entries = resp.json()
    if not isinstance(entries, list):
        entries = [entries]  # single-file path returns a dict

    xmls: List[Tuple[str, bytes]] = []
    for entry in entries:
        if entry.get("type") != "file" or not entry.get("name", "").endswith(".xml"):
            continue
        download_url = entry.get("download_url")
        if not download_url:
            continue
        raw = requests.get(download_url, headers=headers, timeout=30)
        raw.raise_for_status()
        xmls.append((entry["name"], raw.content))
    logger.info(
        "Fetched %d .xml file(s) from github %s/%s:%s/%s",
        len(xmls), gh_owner, gh_repo, branch, path,
    )
    return xmls


def _gitlab_tool_xmls(m: "re.Match") -> List[Tuple[str, bytes]]:
    """Fetch ``.xml`` files from a GitLab ``/-/tree/`` URL match.

    Uses the public GitLab API v4 ``repository/tree`` endpoint to list,
    and the public ``/-/raw/`` URL for content. No token needed for
    public projects. The namespace may include subgroups.
    """
    host, namespace, branch, path = m.group(1), m.group(2), m.group(3), m.group(4)
    # GitLab API wants the project path URL-encoded ("group%2Fsubgroup%2Fproject").
    from urllib.parse import quote

    project_enc = quote(namespace, safe="")
    tree_url = (
        f"https://{host}/api/v4/projects/{project_enc}/repository/tree"
        f"?path={path}&ref={branch}&per_page=100"
    )
    logger.info("Listing tool XMLs from %s", tree_url)
    resp = requests.get(tree_url, timeout=30)
    resp.raise_for_status()
    entries = resp.json()

    xmls: List[Tuple[str, bytes]] = []
    for entry in entries:
        if entry.get("type") != "blob" or not entry.get("name", "").endswith(".xml"):
            continue
        # Public raw URL: https://<host>/<namespace>/-/raw/<branch>/<filepath>
        raw_url = f"https://{host}/{namespace}/-/raw/{branch}/{entry['path']}"
        raw = requests.get(raw_url, timeout=30)
        raw.raise_for_status()
        xmls.append((entry["name"], raw.content))
    logger.info(
        "Fetched %d .xml file(s) from gitlab %s:%s/%s",
        len(xmls), namespace, branch, path,
    )
    return xmls


def get_tool_xmls_from_repo(
    remote_repository_url: str,
    *,
    github_token: Optional[str] = None,
) -> List[Tuple[str, bytes]]:
    """Fetch a ToolShed repo's tool XML files from its source-forge mirror.

    Why not the ToolShed directly: the Galaxy ToolShed's own
    ``/repos/<owner>/<name>/`` file endpoints (both ``/archive/`` and
    ``/raw-file/``) now return ``403 Forbidden — Authentication is
    required``. Anonymous file access is locked down. But most ToolShed
    repositories carry a ``remote_repository_url`` (surfaced by the
    ToolShed ``/api/repositories`` catalog) pointing at a public forge.
    So discovery stays ToolShed-driven; only the raw content comes from
    the forge.

    Supports **GitHub** (``github.com/<owner>/<repo>/tree/<ref>/<path>``)
    and **GitLab** (``<host>/<namespace>/-/tree/<ref>/<path>``, incl.
    self-hosted GitLab with subgroups). ``remote_repository_url`` is the
    value from a ToolShed repository record.

    Returns ``[(filename, xml_bytes), ...]`` for every ``.xml`` file in
    the repo's tool directory. Returns ``[]`` (with a logged warning,
    NOT an exception) when the URL is neither a recognized GitHub nor
    GitLab ``/tree/`` URL — that repo simply cannot be ingested via this
    path; the caller skips it. A network / API failure DOES raise.

    Pass ``github_token`` (or set ``$GITHUB_TOKEN``) to lift the GitHub
    API rate limit from 60/hr (anonymous) to 5000/hr.
    """
    url = remote_repository_url or ""
    gh = _GITHUB_TREE_RE.search(url)
    if gh:
        return _github_tool_xmls(gh, github_token)
    gl = _GITLAB_TREE_RE.search(url)
    if gl and "/-/tree/" in url:
        return _gitlab_tool_xmls(gl)
    logger.warning(
        "remote_repository_url %r is not a recognized GitHub or GitLab "
        "/tree/ URL — cannot ingest this repo's content via the forge path",
        remote_repository_url,
    )
    return []


def get_tool_repository_tar(
    owner: str, name: str, toolshed_url: str | None = None
) -> io.BytesIO | None:
    """Download a ToolShed repo archive tarball.

    NOTE (2026-05-14): the ToolShed's anonymous ``/archive/`` endpoint
    now returns 403 ("Authentication is required"). This function is
    retained for ToolShed deployments that still allow anonymous
    archive access (or where the caller supplies auth); the working
    content path for the public ToolShed is :func:`get_tool_xmls_from_repo`.
    """
    base = _resolve_toolshed_url(toolshed_url)
    repo_url = f"{base}/repos/{owner}/{name}"
    logger.info(f"Downloading repository {owner}/{name} from {repo_url}")

    try:
        # Download the repository archive directly via HTTP
        archive_url = f"{repo_url}/archive/tip.tar.gz"
        logger.debug(f"Downloading archive from {archive_url}")

        response = requests.get(archive_url)
        response.raise_for_status()

        # Return the repository data as BytesIO
        repo_data = io.BytesIO(response.content)
        logger.info(
            f"Successfully downloaded repository {owner}/{name} ({len(response.content)} bytes)"
        )
        return repo_data

    except requests.RequestException as e:
        logger.error(f"Failed to download repository {owner}/{name}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error downloading repository {owner}/{name}: {e}")
        return None


def cleanup_hg_repo(buffer: io.BytesIO) -> io.BytesIO:
    """
    Remove the .hg/ directory and all of its contents from a tar.gz archive.

    Args:
        buffer: BytesIO containing the original tar.gz archive

    Returns:
        BytesIO containing the cleaned tar.gz archive without .hg/ directory
    """
    logger.info("Starting cleanup of .hg directory from repository archive")

    try:
        buffer.seek(0)

        with tarfile.open(fileobj=buffer, mode="r:gz") as original_tar:
            cleaned_buffer = io.BytesIO()

            with tarfile.open(fileobj=cleaned_buffer, mode="w:gz") as cleaned_tar:
                # Iterate through all members in the original archive
                for member in original_tar.getmembers():
                    # Check if the member is in the .hg directory
                    if (
                        "/.hg/" in member.name
                        or member.name.endswith("/.hg")
                        or member.name == ".hg"
                        or member.name.endswith(".hg_archival.txt")
                        or member.name.startswith(".hg/")
                    ):
                        logger.debug(f"Skipping Mercurial file: {member.name}")
                        continue

                    # For non-.hg files, copy them to the new archive
                    if member.isfile():
                        # Extract file data from original archive
                        file_data = original_tar.extractfile(member)
                        if file_data:
                            # Add the file to the cleaned archive
                            cleaned_tar.addfile(member, file_data)
                    else:
                        # For directories and other types, add them without data
                        cleaned_tar.addfile(member)

                logger.info("Successfully cleaned .hg directory from archive")

            cleaned_buffer.seek(0)
            return cleaned_buffer

    except tarfile.TarError as e:
        logger.error(f"Failed to process tar archive: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error during .hg cleanup: {e}")
        raise
