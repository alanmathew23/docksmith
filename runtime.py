"""
runtime.py — Docksmith Member 3
================================
Provides RunIsolated(rootfs, command, env, workdir) -> int

Uses Linux namespaces (unshare) + chroot to isolate a process inside a
temporary rootfs directory. The same primitive is used for:
  - RUN instructions during `docksmith build`
  - `docksmith run`

Requirements:
  - Linux only (uses unshare(1) and chroot(1) or pivot_root via ctypes)
  - Must NOT use Docker, runc, containerd, or any other container runtime
  - A file written inside the container must NOT appear on the host filesystem
"""

import os
import sys
import signal
import shutil
import tempfile
import subprocess
import ctypes
import ctypes.util


# ---------------------------------------------------------------------------
# Linux clone / unshare flags
# ---------------------------------------------------------------------------
CLONE_NEWNS   = 0x00020000  # new mount namespace
CLONE_NEWUTS  = 0x04000000  # new UTS namespace (hostname)
CLONE_NEWPID  = 0x20000000  # new PID namespace
CLONE_NEWIPC  = 0x08000000  # new IPC namespace
CLONE_NEWUSER = 0x10000000  # new user namespace

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _unshare(flags: int) -> None:
    """Call unshare(2) syscall directly via libc."""
    ret = _libc.unshare(ctypes.c_int(flags))
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def _mount(source: str, target: str, fs_type: str, flags: int, data: str = "") -> None:
    """Call mount(2) directly via libc."""
    ret = _libc.mount(
        source.encode(),
        target.encode(),
        fs_type.encode() if fs_type else None,
        ctypes.c_ulong(flags),
        data.encode() if data else None,
    )
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


MS_BIND   = 4096
MS_REC    = 16384
MS_PRIVATE = 1 << 18
MS_NOSUID  = 2
MS_NODEV   = 4
MS_NOEXEC  = 8


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def RunIsolated(
    rootfs: str,
    command: list[str],
    env: dict[str, str],
    workdir: str = "/",
) -> int:
    """
    Run `command` inside `rootfs` with full Linux process isolation.

    Parameters
    ----------
    rootfs  : Absolute path to the assembled container filesystem on the host.
    command : Argv list, e.g. ["python3", "main.py"] or ["/bin/sh", "-c", "echo hi"].
    env     : Dict of environment variables to inject (image ENV + -e overrides already merged).
    workdir : Working directory *inside* the container. Defaults to "/".

    Returns
    -------
    Exit code of the container process (int).
    """
    rootfs = os.path.realpath(rootfs)
    if isinstance(command, str):
        command = ["/bin/sh", "-c", command]
    if not workdir:
        workdir = "/"

    # Fork a child that will set up namespaces and exec into the container.
    pid = os.fork()
    if pid == 0:
        # ---- CHILD --------------------------------------------------------
        try:
            _child_exec(rootfs, command, env, workdir)
        except Exception as exc:
            print(f"[docksmith runtime] child error: {exc}", file=sys.stderr)
            os._exit(126)
        os._exit(0)  # unreachable after exec
    else:
        # ---- PARENT -------------------------------------------------------
        try:
            _, status = os.waitpid(pid, 0)
        except KeyboardInterrupt:
            os.kill(pid, signal.SIGTERM)
            _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _child_exec(rootfs: str, command: list[str], env: dict[str, str], workdir: str) -> None:
    """
    Called in the child process. Sets up namespaces, chroots, and execs.

    Strategy
    --------
    1. Unshare mount + UTS + IPC namespaces  (PID ns needs extra work for /proc,
       skip for simplicity — mount ns alone is sufficient for host isolation).
    2. Bind-mount rootfs onto itself so we can make it a private mount point.
    3. Bind-mount essential pseudo-filesystems (/proc, /dev, /sys) from the host
       into the container rootfs so basic commands work.
    4. chroot into rootfs.
    5. chdir to workdir.
    6. execve the command with the supplied environment.
    """

    # 1. New mount namespace (isolates all mounts from host)
    try:
        _unshare(CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWIPC)
    except OSError:
        # Fallback: try without UTS/IPC (some older kernels / restricted envs)
        _unshare(CLONE_NEWNS)

    # 2. Make the entire mount tree private so host doesn't see our mounts
    try:
        _mount("none", "/", "", MS_REC | MS_PRIVATE)
    except OSError:
        pass  # best-effort; chroot still isolates writes

    # 3. Bind-mount pseudo-filesystems into the container rootfs
    _setup_pseudo_fs(rootfs)

    # 4. chroot
    os.chroot(rootfs)
    os.chdir("/")

    # 5. chdir to workdir inside the container
    try:
        os.chdir(workdir)
    except OSError:
        # workdir might not exist; fall back to /
        os.chdir("/")

    # 6. Build environment list for execve
    env_list = [f"{k}={v}" for k, v in env.items()]

    # 7. exec — replaces this child process entirely
    try:
        os.execvpe(command[0], command, dict(item.split("=", 1) for item in env_list))
    except FileNotFoundError:
        # Try /bin/sh -c as fallback for bare shell commands
        if len(command) == 1:
            os.execvpe("/bin/sh", ["/bin/sh", "-c", command[0]],
                       dict(item.split("=", 1) for item in env_list))
        raise


def _setup_pseudo_fs(rootfs: str) -> None:
    """
    Bind-mount /proc, /dev, /sys from the host into the container rootfs.
    These are needed by many basic commands. We bind-mount (not create) so
    they reflect the host kernel; they are inside the new mount namespace
    and thus invisible to the host after the child exits.
    """
    pseudo = [
        ("/proc", "proc"),
        ("/dev",  "dev"),
        ("/sys",  "sys"),
    ]
    for host_path, rel in pseudo:
        target = os.path.join(rootfs, rel)
        os.makedirs(target, exist_ok=True)
        if os.path.exists(host_path):
            try:
                _mount(host_path, target, "", MS_BIND | MS_REC)
            except OSError:
                pass  # non-fatal — some envs restrict bind mounts


# ---------------------------------------------------------------------------
# Layer extraction helper (called by build engine and docksmith run)
# ---------------------------------------------------------------------------

def ExtractLayers(layer_digests: list[str], layers_dir: str, dest_dir: str) -> None:
    """
    Extract a list of layer tar files (by digest) in order into dest_dir.
    Later layers overwrite earlier ones (union semantics).

    Parameters
    ----------
    layer_digests : Ordered list of digest strings, e.g. ["sha256:aaa...", ...]
    layers_dir    : Path to ~/.docksmith/layers/
    dest_dir      : Destination directory (temporary rootfs being assembled)
    """
    for digest in layer_digests:
        # digest is "sha256:<hex>"; filename is the hex part
        hex_part = digest.split(":", 1)[1] if ":" in digest else digest
        tar_path = os.path.join(layers_dir, hex_part)
        if not os.path.exists(tar_path):
            # Try full digest as filename
            tar_path = os.path.join(layers_dir, digest.replace(":", "_"))
        if not os.path.exists(tar_path):
            raise FileNotFoundError(
                f"Layer tar not found for digest {digest} in {layers_dir}"
            )
        # Extract; --no-same-owner avoids permission errors when not root
        result = subprocess.run(
            ["tar", "-xf", tar_path, "-C", dest_dir, "--no-same-owner"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to extract layer {digest}: {result.stderr.decode().strip()}"
            )


# ---------------------------------------------------------------------------
# Convenience: assemble rootfs from image manifest and run
# ---------------------------------------------------------------------------

def RunImage(manifest: dict, command: list[str] | None, extra_env: dict[str, str],
             layers_dir: str) -> int:
    """
    High-level helper used by `docksmith run`.

    1. Creates a temp directory.
    2. Extracts all image layers into it.
    3. Merges image ENV with -e overrides.
    4. Calls RunIsolated.
    5. Cleans up the temp directory.
    """
    config  = manifest.get("config", {})
    img_env = {}
    for pair in config.get("Env", []):
        k, _, v = pair.partition("=")
        img_env[k] = v

    # -e overrides take precedence
    merged_env = {**img_env, **extra_env}

    workdir = config.get("WorkingDir") or "/"

    # Determine command
    if not command:
        command = config.get("Cmd")
    if not command:
        raise ValueError(
            "No CMD defined in image and no command given. "
            "Provide a command or set CMD in the Docksmithfile."
        )

    layer_digests = [layer["digest"] for layer in manifest.get("layers", [])]

    tmp = tempfile.mkdtemp(prefix="docksmith_run_")
    try:
        ExtractLayers(layer_digests, layers_dir, tmp)
        exit_code = RunIsolated(tmp, command, merged_env, workdir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return exit_code


# ---------------------------------------------------------------------------
# Standalone smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Quick isolation smoke-test — run this as root on Linux:
        sudo python3 runtime.py

    It will:
      1. Create a tiny fake rootfs with busybox (if available).
      2. Run `id` inside it.
      3. Try to write a file inside the container.
      4. Verify the file does NOT appear on the host.
    """
    import tempfile, shutil, stat

    print("=== Docksmith Runtime Smoke Test ===")
    tmp_root = tempfile.mkdtemp(prefix="docksmith_test_")
    try:
        # Minimal rootfs: copy /bin/sh and its libs if ldd works
        os.makedirs(os.path.join(tmp_root, "bin"), exist_ok=True)
        os.makedirs(os.path.join(tmp_root, "tmp"), exist_ok=True)

        sh_src = shutil.which("sh") or "/bin/sh"
        shutil.copy2(sh_src, os.path.join(tmp_root, "bin", "sh"))
        st = os.stat(os.path.join(tmp_root, "bin", "sh"))
        os.chmod(os.path.join(tmp_root, "bin", "sh"), st.st_mode | stat.S_IEXEC)

        # Copy libraries needed by sh
        try:
            ldd_out = subprocess.check_output(["ldd", sh_src], text=True)
            for line in ldd_out.splitlines():
                for part in line.split():
                    if part.startswith("/") and os.path.exists(part):
                        dest_dir = os.path.join(tmp_root, os.path.dirname(part).lstrip("/"))
                        os.makedirs(dest_dir, exist_ok=True)
                        shutil.copy2(part, os.path.join(dest_dir, os.path.basename(part)))
        except Exception:
            pass

        print(f"[test] rootfs: {tmp_root}")
        print("[test] Running: /bin/sh -c 'echo hello from inside container'")
        code = RunIsolated(
            rootfs=tmp_root,
            command=["/bin/sh", "-c", "echo hello from inside container && echo $MYVAR"],
            env={"MYVAR": "works!"},
            workdir="/",
        )
        print(f"[test] exit code: {code}")

        # Isolation test
        sentinel = "/tmp/docksmith_isolation_test_file"
        print(f"\n[test] Writing {sentinel} inside container...")
        RunIsolated(
            rootfs=tmp_root,
            command=["/bin/sh", "-c", f"echo secret > {sentinel}"],
            env={},
            workdir="/",
        )
        host_path = os.path.join(tmp_root, sentinel.lstrip("/"))
        if os.path.exists(sentinel):
            print(f"[test] FAIL — file appeared on host at {sentinel}")
        elif os.path.exists(host_path):
            print(f"[test] NOTE — file is inside tmp rootfs ({host_path}), not on host. PASS")
        else:
            print("[test] PASS — file not found on host filesystem")

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        print("\n=== Done ===")
