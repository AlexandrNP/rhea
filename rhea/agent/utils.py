import os
import asyncio
from asyncio.subprocess import PIPE
import aiofiles
import logging
import subprocess
from subprocess import CompletedProcess
import conda_pack
import zstandard
import tarfile
import shutil
from rhea.utils.schema import Requirement
from typing import List, Literal
from tempfile import mkdtemp, mktemp
from io import BytesIO
from minio import Minio
from redis import StrictRedis


logger = logging.getLogger(__name__)


def requirements_to_package_list(
    requirements: List[Requirement], strict: bool = True
) -> List[str]:
    """
    Convert a Galaxy-style requirements list into Conda package specifications.

    Args:
        requirements: Galaxy Requirement objects to translate.
        strict: If True, enforce exact version matches; if False, relax version
            constraints when an exact version isn't available in Conda.

    Returns:
        A list of Conda package strings to install.
    """
    packages: List[str] = []
    for requirement in requirements:
        if requirement.type == "package":
            if strict:
                packages.append(f"{requirement.value}={requirement.version}")
            else:
                packages.append(f"{requirement.value}>={requirement.version}")
        else:
            raise NotImplementedError(
                f'Requirement of type "{requirement.type}" not yet implemented.'
            )
    return packages


async def configure_tool_directory(tool_id: str, minio: Minio) -> str:
    """
    Configure the scripts required for the tool.
    Pulls all objects from the repo from object store and places them into a temporary directory
    Returns: A path to the temporary directory containing scripts
    NOTE: Must cleanup after yourself!
    """

    async def _fetch_and_write(
        minio: Minio, bucket: str, obj, dest_dir: str, prefix: str
    ):
        name = obj.object_name
        if not name:
            return

        resp = await asyncio.to_thread(minio.get_object, bucket, name)
        data = await asyncio.to_thread(resp.read)
        await asyncio.to_thread(resp.close)
        await asyncio.to_thread(resp.release_conn)

        local_path = os.path.join(dest_dir, os.path.relpath(name, prefix))
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        async with aiofiles.open(local_path, "wb") as f:
            await f.write(data)

    dest_dir = mkdtemp()
    prefix = f"{tool_id}/"

    objs = await asyncio.to_thread(
        lambda: list(minio.list_objects("dev", prefix=prefix, recursive=True))
    )
    logger.info(f"Pulling {len(objs)} objects.")

    tasks = [
        asyncio.create_task(_fetch_and_write(minio, "dev", obj, dest_dir, prefix))
        for obj in objs
    ]

    await asyncio.gather(*tasks)
    logger.info(f"Objects pulled into {dest_dir}")
    return dest_dir


async def cleanup_tool_directory(dir_path: str) -> None:
    """
    Remove the temporary directory created for a tool.
    """
    try:
        await asyncio.to_thread(shutil.rmtree, dir_path)
        logger.info(f"Cleaned up tool directory: {dir_path}")
    except Exception as e:
        logger.warning(f"Failed to clean up {dir_path}: {e}")


def _conda_binary() -> str:
    """Resolve the conda binary to use for tool-env management.

    Honors ``$CONDA_EXE`` (conda's official env var, set when conda is
    initialized in the shell — the orchestrator sets it explicitly).
    This closes the PATH-leakage failure mode where a stale Anaconda
    install at ``/opt/anaconda3/bin/conda`` wins ahead of the real
    miniconda whose ``envs/`` directory actually carries Rhea's tool
    envs. The metadata-but-no-files conda-pack archive that resulted
    is the silent-failure shape the verification below also guards.
    """
    import os
    return os.environ.get("CONDA_EXE", "conda")


async def install_conda_env(
    env_name: str,
    requirements: List[Requirement],
    r: StrictRedis,
    target_path: str,
    n_threads: int = -1,
) -> List[str]:
    loop = asyncio.get_running_loop()

    # If Conda environment is cached, unpack and return immediately
    exists = await loop.run_in_executor(None, r.hexists, "conda_envs", env_name)
    if exists:
        await loop.run_in_executor(None, unpack_conda_env, env_name, r, target_path)
        return []

    # Create a new environment.
    #
    # Anti-silent-failure: the legacy code fell back from strict
    # (``pkg=ver``) to non-strict (``pkg>=ver``) WITHOUT warning the
    # operator. For a tool whose CLI is not backward-compatible across
    # major versions (MUSCLE 3.x vs 5.x is the canonical example;
    # bioconda's default flipped from 3.8.1551 to 5.x), the silent
    # fallback installs a version the Galaxy tool's <command> template
    # cannot drive — every dispatch later returns "Invalid command
    # line / Unknown option in" with conda's noise in front, and the
    # operator has no idea their pin was relaxed.
    #
    # Now: try strict; if it fails, log a LOUD warning naming the
    # exact failure + the relaxed spec the fallback uses; after the
    # fallback succeeds, verify the actually-installed version matches
    # the requested MAJOR version. A major-version mismatch is a
    # hard FAIL-LOUD — better to leave the env uninstalled than to
    # pretend everything is fine.
    packages: List[str] = []
    used_fallback = False
    # If a previous run left an empty/partial env with this name
    # (conda's `conda create -n X -y` is a silent no-op when X already
    # exists), tear it down first so we genuinely build from scratch.
    # Without this, the verification below catches the empty env but
    # only after wasting time on a no-op create.
    _conda = _conda_binary()
    rm_stale = await asyncio.create_subprocess_exec(
        _conda, "env", "remove", "-n", env_name, "-y",
        stdout=PIPE, stderr=PIPE,
    )
    await rm_stale.communicate()
    # Per-call env overrides (e.g. CONDA_SOLVER=classic) accumulate
    # across retries within this one install_conda_env call so a
    # successful libmamba->classic recovery on the strict attempt
    # carries through to a possible non-strict fallback later.
    _extra_env: dict[str, str] = {}

    # Galaxy-canonical channels. Rhea ingests Galaxy tools; Galaxy's
    # `<requirement type="package">` wrappers assume bioconda
    # (primary) + conda-forge (dependency). Passing them on the
    # command line is additive — operators who already have these
    # channels in `~/.condarc` lose nothing; operators with an empty
    # condarc (or just `pkgs/main` + `pkgs/r` as on a clean Anaconda
    # install) now actually find the tools their galaxytools table
    # asks for. Without this, every fresh-conda operator hits a
    # `PackagesNotFoundError` on first tool install and has no
    # signpost telling them why.
    #
    # Channel priority (left-to-right): bioconda is searched FIRST,
    # which is the Galaxy convention — biology-specific builds win
    # over the generic conda-forge fallback. `RHEA_CONDA_EXTRA_CHANNELS`
    # (comma-separated) prepends additional channels so operators
    # with site-specific mirrors or private indexes can override
    # without forking; the canonical pair always lands after them.
    import os as _os_for_channels
    _default_channels = ["bioconda", "conda-forge"]
    _extra_chan_raw = _os_for_channels.environ.get("RHEA_CONDA_EXTRA_CHANNELS", "")
    _extra_channels = [c.strip() for c in _extra_chan_raw.split(",") if c.strip()]
    _channel_args: list[str] = []
    for ch in [*_extra_channels, *_default_channels]:
        _channel_args.extend(["-c", ch])

    async def _try_create(spec_strict: bool) -> tuple[int, bytes, bytes, list[str]]:
        """Run `conda create` once with the given strictness; return (rc, stdout, stderr, pkgs)."""
        pkgs = requirements_to_package_list(requirements, strict=spec_strict)
        import os as _os_for_env
        sub_env = dict(_os_for_env.environ)
        sub_env.update(_extra_env)
        p = await asyncio.create_subprocess_exec(
            _conda, "create", "-n", env_name, "-y", *_channel_args, *pkgs,
            stdout=PIPE, stderr=PIPE, env=sub_env,
        )
        out, err = await p.communicate()
        return p.returncode, out, err, pkgs

    # Two distinct families of recoverable conda failure with
    # DIFFERENT recovery actions:
    #
    #   (a) Metadata corruption (`Prefix record`, `already exists`,
    #       `Multiple packages found`) — fix is `conda clean --all`
    #       + env remove + retry. Common on macOS when libcxx or
    #       similar gets out of sync between local conda cache and
    #       the env's metadata.
    #   (b) Broken libmamba solver backend (operator's `~/.condarc`
    #       says `solver: libmamba` but conda's libarchive/libmamba
    #       dyld chain is broken — typical on `/opt/anaconda3`
    #       installs after a partial homebrew upgrade). The classic
    #       solver still works; recovery is to retry with
    #       `CONDA_SOLVER=classic` set on the subprocess env. Running
    #       `conda clean` would NOT help — the file is missing, not
    #       cached-wrong.
    _corruption_signatures = (
        b"Prefix record",
        b"already exists",
        b"Multiple packages found",
    )
    _libmamba_signatures = (
        b"libmamba",
        b"libarchive",
        b"solver backend",
        b"libmambapy",
    )

    for strict in (True, False):
        rc, stdout, stderr, packages = await _try_create(strict)

        # Self-heal (a): conda-cache corruption. Run `conda clean
        # --all`, remove any half-baked env, retry SAME strictness
        # before falling back to relaxed. This avoids silently
        # accepting a wider version range when the real problem is
        # operator-side corruption that conda can fix on its own.
        if rc != 0 and strict and any(sig in stderr for sig in _corruption_signatures):
            logger.warning(
                "install_conda_env %r: STRICT pin failed with recoverable "
                "conda corruption (exit %s). Running `conda clean --all -y` "
                "and retrying. Original stderr (first 300 chars): %s",
                env_name,
                rc,
                stderr.decode().strip()[:300],
            )
            clean = await asyncio.create_subprocess_exec(
                _conda, "clean", "--all", "-y",
                stdout=PIPE, stderr=PIPE,
            )
            await clean.communicate()
            # Also re-remove the env in case the failed create left a
            # half-baked prefix lying around.
            rm_again = await asyncio.create_subprocess_exec(
                _conda, "env", "remove", "-n", env_name, "-y",
                stdout=PIPE, stderr=PIPE,
            )
            await rm_again.communicate()
            rc, stdout, stderr, packages = await _try_create(True)

        # Self-heal (b): broken libmamba solver backend. Re-arm the
        # `_extra_env` with `CONDA_SOLVER=classic` and retry. We
        # gate this with `CONDA_SOLVER not in _extra_env` so we
        # don't loop endlessly if the classic solver is ALSO broken
        # (in which case the operator has a deeper conda install
        # problem we cannot paper over).
        if (
            rc != 0
            and strict
            and "CONDA_SOLVER" not in _extra_env
            and any(sig in stderr for sig in _libmamba_signatures)
        ):
            logger.warning(
                "install_conda_env %r: STRICT pin failed because the "
                "configured libmamba solver backend is broken on this "
                "host (exit %s). Retrying with CONDA_SOLVER=classic so "
                "the classic resolver overrides the operator's ~/.condarc "
                "for this install only. To fix permanently, repair the "
                "conda installation (typically reinstalling "
                "conda-libmamba-solver + libarchive). Original stderr "
                "(first 300 chars): %s",
                env_name,
                rc,
                stderr.decode().strip()[:300],
            )
            _extra_env["CONDA_SOLVER"] = "classic"
            # Also remove any partial env the failed solver may have
            # half-built, so the retry starts clean.
            rm_lm = await asyncio.create_subprocess_exec(
                _conda, "env", "remove", "-n", env_name, "-y",
                stdout=PIPE, stderr=PIPE,
            )
            await rm_lm.communicate()
            rc, stdout, stderr, packages = await _try_create(True)

        if rc == 0:
            if not strict:
                used_fallback = True
            break
        if not strict:
            raise RuntimeError(stdout.decode().strip() + "\n" + stderr.decode().strip())
        # Strict failed AND wasn't recoverable (or recovery didn't
        # fix it). Log + try the relaxed spec.
        logger.warning(
            "install_conda_env %r: STRICT pin failed (conda exit %s). "
            "Falling back to non-strict (>=) spec. Strict packages were: %s. "
            "Conda stderr (first 400 chars): %s",
            env_name,
            rc,
            requirements_to_package_list(requirements, strict=True),
            stderr.decode().strip()[:400],
        )

    # ALWAYS verify what's actually in the env. Conda's exit 0 is
    # necessary but not sufficient — silent no-ops happen, partial
    # downloads happen, the conda-libmamba-solver crashing on
    # libarchive leaves an env with conda-meta but no binaries. We
    # check:
    #
    #   1. every requested package is actually present in the env;
    #   2. its installed MAJOR version matches the requested one
    #      (``>=`` fallback can silently install a CLI-incompatible
    #      major bump — MUSCLE 3.x → 5.x is the canonical case).
    #
    # Either failure tears the env down and FAIL-LOUDs so the cache
    # doesn't serve the broken state.
    verify_proc = await asyncio.create_subprocess_exec(
        _conda, "list", "-n", env_name, "--json", stdout=PIPE, stderr=PIPE,
    )
    v_stdout, v_stderr = await verify_proc.communicate()
    if verify_proc.returncode != 0:
        raise RuntimeError(
            f"install_conda_env {env_name!r}: post-install `conda list` "
            f"verification failed (exit {verify_proc.returncode}): "
            f"{v_stderr.decode().strip()[:400]}"
        )
    import json as _json  # local import — stdlib, cheap
    installed = {pkg["name"]: pkg["version"] for pkg in _json.loads(v_stdout)}

    # Conda's `list --json` reports packages whose METADATA is
    # recorded — not whose files are actually on disk. We've observed
    # `conda create` exit 0 + metadata claiming muscle=3.8.1551 is
    # installed while the env's bin/ directory is empty (only
    # conda-meta + etc; ~5KB total). The metadata path is the silent-
    # failure shape we have to catch BEFORE packing the empty env into
    # the Redis cache where it poisons every subsequent run.
    #
    # Look up the env's actual prefix via `conda info --envs --json`,
    # check its bin/ contains at least one non-conda binary, and
    # require its total disk size to be larger than a sanity floor.
    info_proc = await asyncio.create_subprocess_exec(
        _conda, "info", "--envs", "--json", stdout=PIPE, stderr=PIPE,
    )
    i_stdout, i_stderr = await info_proc.communicate()
    if info_proc.returncode != 0:
        raise RuntimeError(
            f"install_conda_env {env_name!r}: `conda info --envs --json` "
            f"verification failed (exit {info_proc.returncode}): "
            f"{i_stderr.decode().strip()[:400]}"
        )
    info_obj = _json.loads(i_stdout)
    env_prefix = next(
        (p for p in info_obj.get("envs", []) if p.endswith(f"/envs/{env_name}")),
        None,
    )
    if env_prefix is None:
        raise RuntimeError(
            f"install_conda_env {env_name!r}: env disappeared after "
            f"`conda create` returned 0. `conda info --envs` shows: "
            f"{info_obj.get('envs')!r}"
        )
    import os as _os
    bin_dir = _os.path.join(env_prefix, "bin")
    bin_files: list[str] = []
    if _os.path.isdir(bin_dir):
        bin_files = _os.listdir(bin_dir)
    # Treat the conda-shim files as "no real install" — they're added
    # by `conda create` even for an empty env.
    _conda_shim = {"activate", "conda", "conda-env", "deactivate", "python"}
    package_binaries = [f for f in bin_files if f not in _conda_shim]
    if not package_binaries:
        logger.error(
            "install_conda_env %r: post-install env at %r has NO "
            "package binaries (bin/ contents: %s). Conda likely "
            "produced a metadata-only env (silent-failure shape — "
            "`conda list` reports the package as installed, but the "
            "files were never downloaded). Tearing it down so the "
            "Redis cache does not serve a broken env.",
            env_name,
            env_prefix,
            sorted(bin_files),
        )
        rm = await asyncio.create_subprocess_exec(
            _conda, "env", "remove", "-n", env_name, "-y",
            stdout=PIPE, stderr=PIPE,
        )
        await rm.communicate()
        raise RuntimeError(
            f"install_conda_env {env_name!r}: `conda create` returned 0 "
            f"and `conda list` reports the package(s) installed, but "
            f"{env_prefix}/bin/ contains no package binaries — only the "
            f"conda shim. The metadata-but-no-files state is a silent "
            f"conda failure (commonly: a libmambapy/libarchive crash in "
            f"the system conda, or a partial offline-mode resolve). "
            f"Check `which conda`, `conda config --show channels`, and "
            f"that the conda binary's own dyld dependencies are intact "
            f"(`/opt/anaconda3` installs are the usual culprit on "
            f"macOS)."
        )
    for requirement in requirements:
        if requirement.type != "package":
            continue
        installed_version = installed.get(requirement.value, "")
        if not installed_version:
            # Conda reported success but the package isn't in the env.
            # This is the canonical conda-silent-no-op shape (env name
            # already existed, `conda create -y` was a no-op).
            logger.error(
                "install_conda_env %r: post-install verification — "
                "package %r was requested but is NOT present in the "
                "env. Conda likely silently no-op'd a `create` against "
                "a pre-existing env, or the install failed half-way "
                "with a 0 exit. Tearing the env down so the cache "
                "doesn't serve a non-functional state.",
                env_name,
                requirement.value,
            )
            rm = await asyncio.create_subprocess_exec(
                _conda, "env", "remove", "-n", env_name, "-y",
                stdout=PIPE, stderr=PIPE,
            )
            await rm.communicate()
            raise RuntimeError(
                f"install_conda_env {env_name!r}: package "
                f"{requirement.value!r} was requested but is not in "
                f"the env after `conda create` returned exit 0. "
                f"Check that `conda env list` shows no stale {env_name!r} "
                f"env, that bioconda is on your channels "
                f"(`conda config --show channels`), and that "
                f"`{requirement.value}={requirement.version}` resolves "
                f"in your environment."
            )
        requested_major = (requirement.version or "").split(".", 1)[0]
        installed_major = installed_version.split(".", 1)[0]
        if requested_major and installed_major and requested_major != installed_major:
            logger.error(
                "install_conda_env %r: MAJOR version skew for %r — "
                "Galaxy XML asked for %s, conda installed %s. "
                "The Galaxy tool's <command> template was authored "
                "against the requested major and will fail at "
                "dispatch time. Removing the env so the cache "
                "doesn't serve it.",
                env_name,
                requirement.value,
                requirement.version,
                installed_version,
            )
            rm = await asyncio.create_subprocess_exec(
                _conda, "env", "remove", "-n", env_name, "-y",
                stdout=PIPE, stderr=PIPE,
            )
            await rm.communicate()
            raise RuntimeError(
                f"install_conda_env {env_name!r}: refusing to keep a "
                f"major-version-mismatched env. {requirement.value!r} "
                f"requested {requirement.version!r} (major "
                f"{requested_major!r}), conda installed "
                f"{installed_version!r} (major {installed_major!r}). "
                f"Either update the Galaxy tool XML to a version "
                f"available in the configured channels, or add the "
                f"channel that carries the requested major (e.g. "
                f"`conda config --add channels bioconda`)."
            )
    # used_fallback is no longer load-bearing for verification (we
    # verify always), but keep the variable for future logging hooks.
    _ = used_fallback

    # Pack the environment in another thread
    future = loop.run_in_executor(None, pack_conda_env, env_name, r, n_threads)
    asyncio.ensure_future(future)

    return packages


def pack_conda_env(env_name: str, r: StrictRedis, n_threads: int = -1) -> None:
    """
    Packages the generated Conda enviroment, compresses w/ zstd, and pushes to Redis.
    """
    out_path = mktemp(suffix=".tar.zst")
    logger.info(f"Packing environment '{env_name}' into {out_path}")
    # Pack Conda environment to buffer
    conda_pack.pack(name=env_name, output=out_path, n_threads=n_threads)
    with open(out_path, mode="rb") as f:
        buff = f.read()
        logger.debug(f"Resulting size of packed environment '{env_name}': {len(buff)}")

        if len(buff) <= 0:
            raise RuntimeError("Length of packaged environment <=0!")

        # Create Redis transaction and add buffer
        pipe = r.pipeline(transaction=True)
        pipe.hset("conda_envs", mapping={env_name: buff})
        pipe.execute()
        logger.info(f"Environment '{env_name}' stored in Redis.")
    os.remove(out_path)


def unpack_conda_env(env_name: str, r: StrictRedis, target_path: str) -> None:
    """
    Get packaged Conda environment from Redis and upack it.
    Raises KeyError if Conda enviorment is not in Redis.
    """
    buff = r.hget("conda_envs", env_name)
    if buff is None:
        raise KeyError(f"No entry for '{env_name}' in Redis hash 'conda_envs'")

    logger.info(f"Getting environment {env_name} from Redis")
    dctx = zstandard.ZstdDecompressor()
    reader = dctx.stream_reader(BytesIO(buff))  # type: ignore

    with tarfile.open(fileobj=reader, mode="r|*") as tar:
        tar.extractall(path=target_path)

    conda_unpack = os.path.join(target_path, "bin", "conda-unpack")
    subprocess.run([conda_unpack], cwd=target_path, check=True)
    logger.info(f"Unpacked environment {env_name}")


async def pull_image(image: str, engine: Literal["docker", "podman"]):
    cmd = [engine, "pull"]
    if engine == "podman":
        cmd += ["--remote", "-H", "unix:///run/podman/podman.sock"]
    cmd.append(image)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out_b, err_b = await proc.communicate()

    if proc.returncode != 0:
        msg = (err_b or out_b).decode(errors="replace").strip()
        logger.error(f"{engine} pull failed: {msg}")
        raise RuntimeError(f"{engine} pull failed")
    logger.info((out_b or err_b).decode(errors="replace").strip())


async def remove_image(image: str, engine: Literal["docker", "podman"]):
    cmd = [engine]
    if engine == "podman":
        cmd += ["--remote", "-H", "unix:///run/podman/podman.sock"]
    cmd.append("rmi")
    cmd.append(image)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        msg = (stderr or stdout).decode(errors="replace").strip()
        logger.error(f"{engine} image remove failed: {msg}")
        raise RuntimeError(f"{engine} image remove failed")
    logger.info((stdout or stderr).decode(errors="replace").strip())


async def run_command_w_conda(
    tool_id: str, script_path: str, env: dict[str, str]
) -> CompletedProcess:
    # Resolve conda binary against the spawn env (which is what carries
    # CONDA_EXE from the orchestrator). Falling back to PATH lookup
    # (the env's PATH, which the orchestrator composes correctly) is
    # also safe — but explicit CONDA_EXE closes the case where a
    # stale /opt/anaconda3/bin/conda wins via the operator's PATH.
    cmd = [
        env.get("CONDA_EXE", "conda"),
        "run",
        "-n",
        tool_id,
        "--no-capture-output",
        "bash",
        script_path,
    ]
    logger.info(f"Running subprocess: {cmd}")
    result = await asyncio.to_thread(
        subprocess.run,
        cmd,
        env=env,
        cwd=env["__tool_directory__"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error(
            f"Error in running tool command: \n{result.stdout}\n{result.stderr}"
        )
        raise Exception(f"Error in running tool command: {result.stderr}")
    return result


async def run_command_in_container(
    image: str,
    engine: Literal["docker", "podman"],
    script_path: str,
    env: dict[str, str],
) -> CompletedProcess:
    cmd = [engine]
    if engine == "podman":
        cmd += ["--remote", "-H", "unix:///run/podman/podman.sock"]
    cmd += ["run", "--rm", "-v", "/tmp:/tmp"]

    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]

    cmd += [image, "bash", script_path]

    logger.debug(f"Starting container with command: {' '.join(cmd)}")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode is None:
        raise RuntimeError("No return code returned!")

    result = CompletedProcess(
        args=cmd, returncode=process.returncode, stdout=stdout, stderr=stderr
    )

    return result
