"""
storage.py — Member 1
Handles all ~/.docksmith/ disk I/O: images, layers, directory setup.

Public API (for teammates):
    init_store()                            -> None
    LoadImage(name_tag)                     -> manifest dict
    SaveImage(manifest)                     -> None
    WriteLayer(tar_bytes)                   -> (digest: str, size: int)
    CreateTar(src_path, files=None)         -> bytes  (sorted, zeroed timestamps)
    ExtractLayers(layer_digests, dest_dir)  -> None
    ListImages()                            -> list[dict]
    DeleteImage(name_tag)                   -> None
    ImageExists(name_tag)                   -> bool
    LayerExists(digest)                     -> bool
    LayerPath(digest)                       -> str
    LayerSize(digest)                       -> int
"""

import os
import json
import hashlib
import tarfile
import io
import time

# ── Directory layout ──────────────────────────────────────────────────────────

DOCKSMITH_HOME = os.path.expanduser("~/.docksmith")
IMAGES_DIR     = os.path.join(DOCKSMITH_HOME, "images")
LAYERS_DIR     = os.path.join(DOCKSMITH_HOME, "layers")
CACHE_DIR      = os.path.join(DOCKSMITH_HOME, "cache")


def init_store():
    """Create ~/.docksmith/ and all subdirectories if they don't exist."""
    for d in (IMAGES_DIR, LAYERS_DIR, CACHE_DIR):
        os.makedirs(d, exist_ok=True)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _image_path(name: str, tag: str) -> str:
    safe = f"{name}_{tag}".replace("/", "_").replace(":", "_")
    return os.path.join(IMAGES_DIR, f"{safe}.json")


def _parse_name_tag(name_tag: str):
    """Split 'name:tag' -> ('name', 'tag'). Default tag is 'latest'."""
    if ":" in name_tag:
        name, tag = name_tag.rsplit(":", 1)
    else:
        name, tag = name_tag, "latest"
    return name, tag


def _to_iso8601(value) -> str:
    """
    Normalise a 'created' value to ISO-8601 string.
    Accepts: ISO-8601 string, Unix int/float timestamp, or None (-> now).
    """
    if value is None:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if isinstance(value, (int, float)):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
    return str(value)


def _compute_manifest_digest(manifest: dict) -> str:
    """
    Spec 4.1:
      1. Set digest field to ""
      2. Serialize JSON: sorted keys, no extra whitespace
      3. SHA-256 of those UTF-8 bytes
    Uses a shallow copy — safe because only the top-level 'digest' key is mutated.
    """
    tmp = dict(manifest)
    tmp["digest"] = ""
    canonical = json.dumps(tmp, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(canonical.encode("utf-8"))


# ── Public API ────────────────────────────────────────────────────────────────

def LoadImage(name_tag: str) -> dict:
    """
    Load and return an image manifest dict from disk.
    Raises FileNotFoundError with a clear message if the image is not found.
    """
    init_store()
    name, tag = _parse_name_tag(name_tag)
    path = _image_path(name, tag)

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Image '{name_tag}' not found in local store. "
            f"Run setup.sh to import a base image, or build one first."
        )

    with open(path, "r") as f:
        return json.load(f)


def SaveImage(manifest: dict) -> None:
    """
    Write an image manifest to images/.

    Validates and enforces spec 4.1 requirements:
      - 'created' normalised to ISO-8601 string.
      - 'config' must be a dict with Env (list), Cmd (list|None), WorkingDir (str).
      - Each layer entry must have 'digest', 'size' (int), and 'createdBy' (str).
      - 'digest' is computed per spec (serialize with digest="", SHA-256).

    The manifest dict is mutated in-place (digest and created fields updated).
    """
    init_store()

    # FIX: Normalise created to ISO-8601
    manifest["created"] = _to_iso8601(manifest.get("created"))

    # FIX: Validate config field (required by spec 4.1)
    config = manifest.get("config")
    if config is None:
        raise ValueError("Manifest missing required 'config' field.")
    if not isinstance(config, dict):
        raise ValueError("Manifest 'config' must be a dict.")
    if "Env" not in config:
        raise ValueError("Manifest config missing 'Env' field.")
    if "Cmd" not in config:
        raise ValueError("Manifest config missing 'Cmd' field.")
    if "WorkingDir" not in config:
        raise ValueError("Manifest config missing 'WorkingDir' field.")

    # Validate layer entries (spec 4.1: digest, size, createdBy required)
    for i, layer in enumerate(manifest.get("layers", [])):
        if "digest" not in layer:
            raise ValueError(f"Layer {i} missing required 'digest' field.")
        if "size" not in layer:
            raise ValueError(
                f"Layer {i} missing required 'size' field. "
                f"Use the (digest, size) tuple returned by WriteLayer()."
            )
        if "createdBy" not in layer:
            raise ValueError(f"Layer {i} missing required 'createdBy' field.")

    # Compute and embed manifest digest
    manifest["digest"] = _compute_manifest_digest(manifest)

    path = _image_path(manifest["name"], manifest["tag"])
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


def WriteLayer(tar_bytes: bytes):
    """
    Write raw tar bytes to layers/ named by their SHA-256 digest.
    Idempotent — if the layer already exists it is NOT overwritten (immutable, spec 4.2).

    Returns:
        (digest, size) where:
          digest — "sha256:<hex>" string (use as layer entry 'digest')
          size   — byte length of the tar  (use as layer entry 'size')

    Example:
        digest, size = WriteLayer(tar_bytes)
        layer_entry = {
            "digest":    digest,
            "size":      size,
            "createdBy": "COPY . /app",
        }
    """
    init_store()
    digest   = _sha256_bytes(tar_bytes)
    hex_part = digest[len("sha256:"):]
    path     = os.path.join(LAYERS_DIR, hex_part + ".tar")

    if not os.path.exists(path):   # immutability: never overwrite
        with open(path, "wb") as f:
            f.write(tar_bytes)

    return digest, len(tar_bytes)


def CreateTar(src_path: str, files: list = None) -> bytes:
    """
    Create a reproducible tar archive (spec 8: reproducible builds).

    Reproducibility rules:
      - Entries in lexicographically sorted order by relative path.
      - mtime = 0 for all entries (timestamps zeroed).
      - uid = gid = 0, uname = gname = "".

    Args:
        src_path: Root directory to archive from.
        files:    Optional list of relative paths within src_path to include.
                  If None, every file and directory under src_path is included.

    Returns:
        Raw tar bytes (delta — caller decides what files to include).
    """
    buf = io.BytesIO()

    with tarfile.open(fileobj=buf, mode="w:") as tar:
        if files is None:
            all_entries = []
            for root, dirs, filenames in os.walk(src_path):
                dirs.sort()  # make os.walk order deterministic
                for fname in filenames:
                    abs_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(abs_path, src_path)
                    all_entries.append((rel_path, abs_path))
                for d in dirs:
                    abs_dir = os.path.join(root, d)
                    rel_dir = os.path.relpath(abs_dir, src_path)
                    all_entries.append((rel_dir, abs_dir))
            all_entries.sort(key=lambda x: x[0])
        else:
            all_entries = sorted(
                [(f, os.path.join(src_path, f)) for f in files],
                key=lambda x: x[0],
            )

        for rel_path, abs_path in all_entries:
            if not os.path.exists(abs_path):
                continue  # skip missing files gracefully

            info = tar.gettarinfo(abs_path, arcname=rel_path)
            # Zero everything that could differ between runs
            info.mtime = 0
            info.uid   = 0
            info.gid   = 0
            info.uname = ""
            info.gname = ""

            if info.isreg():
                with open(abs_path, "rb") as fh:
                    tar.addfile(info, fh)
            else:
                tar.addfile(info)

    return buf.getvalue()


def ExtractLayers(layer_digests: list, dest_dir: str) -> None:
    """
    Extract layers in order into dest_dir.
    Later layers overwrite earlier ones at the same path (spec 4.2).

    Uses filter='tar' to suppress Python 3.14 DeprecationWarning while
    still allowing symlinks and special files that real image layers contain.

    Raises FileNotFoundError if any layer file is missing (broken image).
    """
    init_store()
    os.makedirs(dest_dir, exist_ok=True)

    for digest in layer_digests:
        hex_part = digest[len("sha256:"):]
        path = os.path.join(LAYERS_DIR, hex_part + ".tar")

        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Layer '{digest}' not found on disk. "
                f"The image is broken — a shared layer may have been deleted by rmi."
            )

        # FIX: use filter='tar' to avoid DeprecationWarning on Python 3.12+
        # 'tar' preserves symlinks and special files needed by real image layers
        with tarfile.open(path, "r:*") as t:
            for member in t.getmembers():
                t.extract(member, path=dest_dir)


def ListImages() -> list:
    """Return all image manifests sorted by filename. Silently skips corrupted files."""
    init_store()
    manifests = []
    for fname in sorted(os.listdir(IMAGES_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(IMAGES_DIR, fname)
        with open(path, "r") as f:
            try:
                manifests.append(json.load(f))
            except json.JSONDecodeError:
                pass
    return manifests


def DeleteImage(name_tag: str) -> None:
    """
    Remove the image manifest and ALL its layer files from disk.
    Spec: no reference counting — shared layers will be gone, breaking other images.
    Raises FileNotFoundError with a clear message if the image does not exist.
    """
    init_store()
    manifest = LoadImage(name_tag)  # raises clearly if not found

    for layer in manifest.get("layers", []):
        hex_part = layer["digest"][len("sha256:"):]
        lpath    = os.path.join(LAYERS_DIR, hex_part + ".tar")
        if os.path.exists(lpath):
            os.remove(lpath)

    name, tag = _parse_name_tag(name_tag)
    mpath = _image_path(name, tag)
    if os.path.exists(mpath):
        os.remove(mpath)


def ImageExists(name_tag: str) -> bool:
    """Return True if the image manifest file exists on disk."""
    init_store()
    name, tag = _parse_name_tag(name_tag)
    return os.path.exists(_image_path(name, tag))


def LayerExists(digest: str) -> bool:
    """Return True if the layer tar file exists on disk."""
    init_store()
    hex_part = digest[len("sha256:"):]
    return os.path.exists(os.path.join(LAYERS_DIR, hex_part + ".tar"))


def LayerPath(digest: str) -> str:
    """Absolute path to a layer tar file (may or may not exist on disk)."""
    hex_part = digest[len("sha256:"):]
    return os.path.join(LAYERS_DIR, hex_part + ".tar")


def LayerSize(digest: str) -> int:
    """Byte size of a layer tar on disk. Returns 0 if not found."""
    path = LayerPath(digest)
    return os.path.getsize(path) if os.path.exists(path) else 0
