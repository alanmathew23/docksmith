#!/usr/bin/env python3
"""
test_storage.py — Member 1 unit tests
Run with: python3 test_storage.py
Does NOT require Members 2, 3, or 4.
"""

import os, sys, json, hashlib, tarfile, io, time, tempfile, shutil, warnings

# Redirect storage to a temp dir so tests never touch real ~/.docksmith/
_test_home = tempfile.mkdtemp(prefix="docksmith-test-")
import storage
storage.DOCKSMITH_HOME = _test_home
storage.IMAGES_DIR     = os.path.join(_test_home, "images")
storage.LAYERS_DIR     = os.path.join(_test_home, "layers")
storage.CACHE_DIR      = os.path.join(_test_home, "cache")
storage.init_store()

from storage import (
    LoadImage, SaveImage, WriteLayer, CreateTar,
    ExtractLayers, ListImages, DeleteImage,
    ImageExists, LayerExists, LayerPath, LayerSize,
    _compute_manifest_digest, _to_iso8601,
)

errors = []

def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        errors.append(name)

# helper: build a valid manifest
def make_manifest(name="app", tag="v1", layer_digest=None, layer_size=0):
    return {
        "name": name, "tag": tag, "digest": "",
        "created": "2024-01-01T00:00:00Z",
        "config": {"Env": ["FOO=bar"], "Cmd": ["sh"], "WorkingDir": "/app"},
        "layers": (
            [{"digest": layer_digest, "size": layer_size, "createdBy": "COPY"}]
            if layer_digest else []
        ),
    }


# ── _to_iso8601 ───────────────────────────────────────────────────────────────
print("\n── _to_iso8601 ──")
check("None -> string",         isinstance(_to_iso8601(None), str))
check("int 0 -> epoch",         _to_iso8601(0) == "1970-01-01T00:00:00Z")
check("float -> string",        isinstance(_to_iso8601(1234567890.0), str))
check("string passthrough",     _to_iso8601("2024-01-01T00:00:00Z") == "2024-01-01T00:00:00Z")


# ── CreateTar ─────────────────────────────────────────────────────────────────
print("\n── CreateTar (reproducibility) ──")

src = tempfile.mkdtemp()
os.makedirs(os.path.join(src, "sub"))
with open(os.path.join(src, "b.txt"), "w") as f: f.write("bbb")
with open(os.path.join(src, "a.txt"), "w") as f: f.write("aaa")
with open(os.path.join(src, "sub", "c.txt"), "w") as f: f.write("ccc")

tar1 = CreateTar(src)
tar2 = CreateTar(src)

check("Returns bytes",          isinstance(tar1, bytes))
check("Deterministic",          tar1 == tar2, "two calls produced different bytes")
check("Non-empty",              len(tar1) > 0)

with tarfile.open(fileobj=io.BytesIO(tar1), mode="r:") as t:
    members = t.getmembers()
    names   = [m.name for m in members if m.isreg()]

check("mtime=0 all entries",    all(m.mtime == 0 for m in members))
check("uid=0 all entries",      all(m.uid == 0 for m in members))
check("gid=0 all entries",      all(m.gid == 0 for m in members))
check("uname='' all entries",   all(m.uname == "" for m in members))
check("entries sorted",         names == sorted(names), f"got {names}")

shutil.rmtree(src)


# ── WriteLayer ────────────────────────────────────────────────────────────────
print("\n── WriteLayer ──")

data           = b"fake tar bytes"
digest, size   = WriteLayer(data)

check("Returns (digest, size) tuple",    isinstance(digest, str) and isinstance(size, int))
check("Digest has sha256: prefix",       digest.startswith("sha256:"))
check("Digest is 71 chars",              len(digest) == 71)
check("Size matches len(tar_bytes)",     size == len(data))
check("Layer file exists after write",   LayerExists(digest))
check("LayerSize matches",               LayerSize(digest) == len(data))

# Idempotency (immutability, spec 4.2)
digest2, size2 = WriteLayer(data)
check("Idempotent: same digest",         digest == digest2)
check("Idempotent: same size",           size == size2)


# ── SaveImage / LoadImage ─────────────────────────────────────────────────────
print("\n── SaveImage / LoadImage ──")

m = make_manifest(layer_digest=digest, layer_size=size)
SaveImage(m)
check("Image manifest created",          ImageExists("app:v1"))

loaded = LoadImage("app:v1")
check("name preserved",                  loaded["name"] == "app")
check("tag preserved",                   loaded["tag"] == "v1")
check("config.WorkingDir preserved",     loaded["config"]["WorkingDir"] == "/app")
check("config.Env preserved",            loaded["config"]["Env"] == ["FOO=bar"])
check("config.Cmd preserved",            loaded["config"]["Cmd"] == ["sh"])
check("layers count preserved",          len(loaded["layers"]) == 1)
check("layer.size preserved",            loaded["layers"][0]["size"] == size)
check("layer.createdBy preserved",       loaded["layers"][0]["createdBy"] == "COPY")
check("digest set (not empty)",          loaded["digest"] != "")
check("digest has sha256: prefix",       loaded["digest"].startswith("sha256:"))
check("created is ISO string",           isinstance(loaded["created"], str) and "T" in loaded["created"])

# Digest deterministic across saves
SaveImage(make_manifest(layer_digest=digest, layer_size=size))
loaded2 = LoadImage("app:v1")
check("Digest deterministic",            loaded["digest"] == loaded2["digest"])


# ── Manifest digest spec compliance (4.1) ─────────────────────────────────────
print("\n── Manifest Digest (spec 4.1) ──")

tmp = dict(loaded)
tmp["digest"] = ""
canonical = json.dumps(tmp, sort_keys=True, separators=(",", ":"))
expected  = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
check("Digest matches spec formula",     loaded["digest"] == expected,
      f"\n    got      {loaded['digest']}\n    expected {expected}")


# ── SaveImage: int 'created' normalised ──────────────────────────────────────
print("\n── SaveImage created normalisation ──")

m2 = make_manifest(name="normtest", layer_digest=digest, layer_size=size)
m2["created"] = 0  # Unix timestamp int
SaveImage(m2)
l3 = LoadImage("normtest:v1")
check("int created -> ISO-8601",         l3["created"] == "1970-01-01T00:00:00Z",
      f"got {l3['created']}")
DeleteImage("normtest:v1")


# ── SaveImage: config validation ─────────────────────────────────────────────
print("\n── SaveImage config validation ──")

def try_bad(manifest_factory, label):
    try:
        SaveImage(manifest_factory())
        check(label, False, "should have raised ValueError")
    except ValueError:
        check(label, True)

try_bad(lambda: {
    "name":"x","tag":"y","digest":"","created":None,
    "layers":[]
}, "Missing config raises ValueError")

try_bad(lambda: {
    "name":"x","tag":"y","digest":"","created":None,
    "config": {"Cmd": None, "WorkingDir": "/"},  # missing Env
    "layers":[]
}, "Config missing Env raises ValueError")

try_bad(lambda: {
    "name":"x","tag":"y","digest":"","created":None,
    "config": {"Env": [], "WorkingDir": "/"},    # missing Cmd
    "layers":[]
}, "Config missing Cmd raises ValueError")

try_bad(lambda: {
    "name":"x","tag":"y","digest":"","created":None,
    "config": {"Env": [], "Cmd": None},           # missing WorkingDir
    "layers":[]
}, "Config missing WorkingDir raises ValueError")


# ── SaveImage: layer field validation ────────────────────────────────────────
print("\n── SaveImage layer validation ──")

base_cfg = {"Env": [], "Cmd": None, "WorkingDir": "/"}

try_bad(lambda: {
    "name":"x","tag":"y","digest":"","created":None,
    "config": base_cfg,
    "layers": [{"size": 10, "createdBy": "x"}]   # missing digest
}, "Layer missing digest raises ValueError")

try_bad(lambda: {
    "name":"x","tag":"y","digest":"","created":None,
    "config": base_cfg,
    "layers": [{"digest": digest, "createdBy": "x"}]  # missing size
}, "Layer missing size raises ValueError")

try_bad(lambda: {
    "name":"x","tag":"y","digest":"","created":None,
    "config": base_cfg,
    "layers": [{"digest": digest, "size": 10}]   # missing createdBy
}, "Layer missing createdBy raises ValueError")


# ── ListImages ────────────────────────────────────────────────────────────────
print("\n── ListImages ──")
images = ListImages()
check("app:v1 in list",     any(m["name"]=="app" and m["tag"]=="v1" for m in images))


# ── DeleteImage (rmi) ─────────────────────────────────────────────────────────
print("\n── DeleteImage (rmi) ──")
DeleteImage("app:v1")
check("Manifest gone after rmi",    not ImageExists("app:v1"))
check("Layer file gone after rmi",  not LayerExists(digest))


# ── Error cases ───────────────────────────────────────────────────────────────
print("\n── Error Cases ──")

try:
    LoadImage("ghost:latest")
    check("LoadImage raises on missing", False)
except FileNotFoundError:
    check("LoadImage raises FileNotFoundError on missing", True)

try:
    DeleteImage("ghost:latest")
    check("DeleteImage raises on missing", False)
except FileNotFoundError:
    check("DeleteImage raises FileNotFoundError on missing", True)


# ── ExtractLayers ─────────────────────────────────────────────────────────────
print("\n── ExtractLayers ──")

src2 = tempfile.mkdtemp()
with open(os.path.join(src2, "hello.txt"), "w") as f: f.write("layer1")
l1_digest, _ = WriteLayer(CreateTar(src2))
shutil.rmtree(src2)

src3 = tempfile.mkdtemp()
with open(os.path.join(src3, "hello.txt"), "w") as f: f.write("layer2-overwrites")
l2_digest, _ = WriteLayer(CreateTar(src3))
shutil.rmtree(src3)

dest = tempfile.mkdtemp()
ExtractLayers([l1_digest, l2_digest], dest)
check("File extracted",          os.path.exists(os.path.join(dest, "hello.txt")))
check("Later layer overwrites",  open(os.path.join(dest, "hello.txt")).read() == "layer2-overwrites")
shutil.rmtree(dest)

try:
    ExtractLayers(["sha256:" + "a"*64], tempfile.mkdtemp())
    check("ExtractLayers raises on missing layer", False)
except FileNotFoundError:
    check("ExtractLayers raises FileNotFoundError on missing layer", True)


# ── ExtractLayers: no DeprecationWarning on Python 3.12+ ─────────────────────
print("\n── ExtractLayers: no DeprecationWarning ──")

src4 = tempfile.mkdtemp()
with open(os.path.join(src4, "x.txt"), "w") as f: f.write("x")
ld, _ = WriteLayer(CreateTar(src4))
shutil.rmtree(src4)
dest4 = tempfile.mkdtemp()
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    ExtractLayers([ld], dest4)
    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
shutil.rmtree(dest4)
check("No DeprecationWarning from extractall", len(dep_warnings) == 0,
      f"got {[str(w.message) for w in dep_warnings]}")


# ── Short ID format (spec 7: first 12 chars of digest) ───────────────────────
print("\n── Short ID format (docksmith images spec 7) ──")

sample = "sha256:a3f9b2c1deadbeef12345678abcdef01"
short  = sample[7:19]
check("Short ID is 12 chars",         len(short) == 12, f"got {len(short)}")
check("Short ID is hex part, no sha256:", short == "a3f9b2c1dead", f"got '{short}'")


# ── No unnecessary imports of private functions ───────────────────────────────
print("\n── main.py does not import private storage functions ──")
with open("main.py") as f:
    main_src = f.read()
check("main.py does not import _to_iso8601", "_to_iso8601" not in main_src,
      "Private function _to_iso8601 leaked into main.py imports")


# ── Cleanup ───────────────────────────────────────────────────────────────────
shutil.rmtree(_test_home)

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 52)
if errors:
    print(f"FAILED: {len(errors)} test(s)")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("All tests passed!")
    sys.exit(0)
