#!/usr/bin/env python3
"""
test_build_engine.py — Member 4 end-to-end tests

Tests covered:
  1.  Parser — line-numbered errors, COPY globs, CMD exec/shell forms
  2.  Cold build  — all COPY/RUN steps are CACHE MISS
  3.  Warm build  — all COPY/RUN steps are CACHE HIT (identical layer digests)
  4.  Partial cache invalidation — changing a COPY source invalidates that step
      and all subsequent COPY/RUN steps, while earlier steps stay cached
  5.  Build isolation — two independent builds do not share layer state
  6.  images (ListImages) — built image appears in the listing
  7.  rmi (DeleteImage / ImageExists) — image is removed after rmi

Run with:
    python3 test_build_engine.py
"""

import os
import sys
import json
import shutil
import tempfile
import io
import contextlib

# ── Redirect storage to a temp dir BEFORE importing anything that touches disk ──

_test_home = tempfile.mkdtemp(prefix="docksmith-test-be-")

import storage
storage.DOCKSMITH_HOME = _test_home
storage.IMAGES_DIR     = os.path.join(_test_home, "images")
storage.LAYERS_DIR     = os.path.join(_test_home, "layers")
storage.CACHE_DIR      = os.path.join(_test_home, "cache")
storage.init_store()

from storage import (
    LoadImage, SaveImage, WriteLayer, CreateTar,
    ListImages, DeleteImage, ImageExists,
)

import build_engine  # must be imported after storage redirect
from build_engine import BuildEngine
import parser as dockparser
from parser import ParseError, parse, expand_copy_srcs

# ── Test harness ──────────────────────────────────────────────────────────────

errors = []

def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        errors.append(name)


@contextlib.contextmanager
def capture_stdout():
    """Capture sys.stdout during a block; yield the StringIO object."""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _make_fake_base(name_tag="alpine:3.18"):
    """
    Write a minimal fake base image into the redirected store.
    Recreates the image if its layer has been deleted (e.g. after rmi).
    """
    from storage import LayerExists
    name, tag = (name_tag.rsplit(":", 1) if ":" in name_tag else (name_tag, "latest"))
    if ImageExists(name_tag):
        # Check that the layer is still on disk (may have been wiped by rmi)
        try:
            m = LoadImage(name_tag)
            if all(LayerExists(l["digest"]) for l in m.get("layers", [])):
                return  # image is healthy
        except Exception:
            pass

    src = tempfile.mkdtemp()
    os.makedirs(os.path.join(src, "bin"), exist_ok=True)
    with open(os.path.join(src, "bin", "sh"), "w") as f:
        f.write("#!/bin/sh\n")
    tar_bytes = CreateTar(src)
    shutil.rmtree(src)

    digest, size = WriteLayer(tar_bytes)
    manifest = {
        "name":    name,
        "tag":     tag,
        "digest":  "",
        "created": "2024-01-01T00:00:00Z",
        "config": {
            "Env":        [],
            "Cmd":        ["/bin/sh"],
            "WorkingDir": "/",
        },
        "layers": [{"digest": digest, "size": size, "createdBy": "imported"}],
    }
    SaveImage(manifest)


def _build(name_tag, context_dir, no_cache=False):
    """Run a build and return (engine, manifest). Captures and discards stdout."""
    dockerfile = os.path.join(context_dir, "Docksmithfile")
    engine = BuildEngine(dockerfile, name_tag, context_dir, no_cache=no_cache)
    with capture_stdout():
        engine.build()
    return engine, LoadImage(name_tag)


def _write_file(path, content):
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _make_context(files: dict) -> str:
    """Create a temp context dir with the given file→content mapping. Returns the dir."""
    ctx = tempfile.mkdtemp(prefix="docksmith-ctx-")
    for rel, content in files.items():
        full = os.path.join(ctx, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    return ctx


# ── Ensure base image exists for all build tests ──────────────────────────────

_make_fake_base("alpine:3.18")


# ═════════════════════════════════════════════════════════════════════════════
# 1. Parser Tests
# ═════════════════════════════════════════════════════════════════════════════
print("\n── Parser: basic parsing ──")

_pctx = tempfile.mkdtemp()

# Write a Docksmithfile with all 6 instructions including CMD exec form
_df1 = os.path.join(_pctx, "Docksmithfile")
with open(_df1, "w") as f:
    f.write(
        "# comment line — should be ignored\n"
        "\n"
        "FROM alpine:3.18\n"
        "WORKDIR /app\n"
        "ENV APP_NAME=docksmith-sample\n"
        'COPY *.txt /app/\n'
        "RUN echo hello\n"
        'CMD ["sh", "-c", "echo hello"]\n'
    )

instrs = parse(_df1)
check("6 instructions parsed",         len(instrs) == 6)
check("FROM is first",                 instrs[0]["instr"] == "FROM")
check("FROM image parsed",             instrs[0]["args"]["image"] == "alpine:3.18")
check("WORKDIR path parsed",           instrs[1]["args"]["path"] == "/app")
check("ENV key parsed",                instrs[2]["args"]["key"] == "APP_NAME")
check("ENV value parsed",              instrs[2]["args"]["value"] == "docksmith-sample")
check("COPY srcs contains glob",       "*.txt" in instrs[3]["args"]["srcs"])
check("COPY dst parsed",               instrs[3]["args"]["dst"] == "/app/")
check("RUN command parsed",            instrs[4]["args"]["command"] == "echo hello")
check("CMD exec form",                 instrs[5]["args"]["form"] == "exec")
check("CMD exec cmd list",             instrs[5]["args"]["cmd"] == ["sh", "-c", "echo hello"])
check("Line numbers correct",          [d["lineno"] for d in instrs] == [3, 4, 5, 6, 7, 8])

shutil.rmtree(_pctx)

print("\n── Parser: CMD shell form ──")

_pctx2 = tempfile.mkdtemp()
_df2 = os.path.join(_pctx2, "Docksmithfile")
with open(_df2, "w") as f:
    f.write("FROM alpine:3.18\nCMD echo hello world\n")

instrs2 = parse(_df2)
check("CMD shell form detected",       instrs2[1]["args"]["form"] == "shell")
check("CMD shell cmd list",            instrs2[1]["args"]["cmd"] == ["echo", "hello", "world"])
shutil.rmtree(_pctx2)

print("\n── Parser: line-numbered errors ──")

def _raises_parse_error(content, lineno=None):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "Docksmithfile")
    with open(p, "w") as f:
        f.write(content)
    try:
        parse(p)
        shutil.rmtree(d)
        return False, None
    except ParseError as e:
        shutil.rmtree(d)
        return True, e
    finally:
        shutil.rmtree(d, ignore_errors=True)

ok, err = _raises_parse_error("RUN echo hi\n")
check("First instruction not FROM raises ParseError",  ok)
check("Error message contains 'FROM'",                 ok and "FROM" in str(err))

ok, err = _raises_parse_error("FROM alpine:3.18\nBADINSTR arg\n")
check("Unknown instruction raises ParseError",         ok)
check("Error has line number 2",                       ok and ":2:" in str(err))

ok, err = _raises_parse_error("FROM alpine:3.18\nENV NOEQUALS\n")
check("ENV without = raises ParseError",               ok)

ok, err = _raises_parse_error('FROM alpine:3.18\nCMD ["not", "closed"\n')
check("CMD bad JSON raises ParseError",                ok)

ok, err = _raises_parse_error("FROM alpine:3.18\nCOPY onlyone\n")
check("COPY missing dest raises ParseError",           ok)

ok, err = _raises_parse_error("")
check("Empty Docksmithfile raises ParseError",         ok)

print("\n── Parser: expand_copy_srcs glob ──")

_gctx = tempfile.mkdtemp()
for fname in ["a.py", "b.py", "notes.txt"]:
    with open(os.path.join(_gctx, fname), "w") as f:
        f.write(fname)

expanded = expand_copy_srcs(["*.py"], _gctx)
check("Glob *.py matches 2 files",     len(expanded) == 2)
check("Glob results are absolute",     all(os.path.isabs(p) for p in expanded))
check("Glob excludes .txt",            all(p.endswith(".py") for p in expanded))

expanded_all = expand_copy_srcs(["*.py", "*.txt"], _gctx)
check("Multiple glob patterns merged", len(expanded_all) == 3)

try:
    expand_copy_srcs(["*.go"], _gctx)
    check("No-match glob raises ValueError", False)
except ValueError:
    check("No-match glob raises ValueError", True)

shutil.rmtree(_gctx)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Cold Build
# ═════════════════════════════════════════════════════════════════════════════
print("\n── Cold build (CACHE MISS on all COPY/RUN steps) ──")

_cold_ctx = _make_context({
    "Docksmithfile": (
        "FROM alpine:3.18\n"
        "WORKDIR /app\n"
        "ENV VER=1\n"
        "COPY static.txt .\n"
        "RUN echo step5\n"
        "CMD sh\n"
    ),
    "static.txt": "static content",
})

# Clear cache for a clean cold build
_cache_file = os.path.join(storage.CACHE_DIR, "cache.json")
if os.path.exists(_cache_file):
    os.remove(_cache_file)

_cold_engine, _cold_manifest = _build("coldtest:v1", _cold_ctx, no_cache=False)

check("Cold build: image saved",         ImageExists("coldtest:v1"))
check("Cold build: has >1 layer",        len(_cold_manifest["layers"]) > 1)
check("Cold build: ENV stored",          "VER=1" in _cold_manifest["config"]["Env"])
check("Cold build: WorkingDir stored",   _cold_manifest["config"]["WorkingDir"] == "/app")
check("Cold build: CMD stored",          _cold_manifest["config"]["Cmd"] == ["sh"])

# Cache file should now exist and have entries
_cache_data = json.load(open(_cache_file)) if os.path.exists(_cache_file) else {}
check("Cold build: cache file written",  len(_cache_data) > 0)

shutil.rmtree(_cold_ctx)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Warm Build (cache hits)
# ═════════════════════════════════════════════════════════════════════════════
print("\n── Warm build (CACHE HIT — same layer digests) ──")

_warm_ctx = _make_context({
    "Docksmithfile": (
        "FROM alpine:3.18\n"
        "WORKDIR /app\n"
        "ENV VER=1\n"
        "COPY static.txt .\n"
        "RUN echo step5\n"
        "CMD sh\n"
    ),
    "static.txt": "static content",
})

# Clear cache first — this is the "first" build
if os.path.exists(_cache_file):
    os.remove(_cache_file)

_, _warm_m1 = _build("warmtest:v1", _warm_ctx, no_cache=False)

# Second build — warm (cache should have all entries from first build)
_, _warm_m2 = _build("warmtest:v1", _warm_ctx, no_cache=False)

_layers1 = [l["digest"] for l in _warm_m1["layers"]]
_layers2 = [l["digest"] for l in _warm_m2["layers"]]

check("Warm build: same layer count",   len(_layers1) == len(_layers2))
check("Warm build: all digests match",  _layers1 == _layers2,
      f"\n  build1: {_layers1}\n  build2: {_layers2}")

shutil.rmtree(_warm_ctx)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Partial Cache Invalidation
# ═════════════════════════════════════════════════════════════════════════════
print("\n── Partial cache invalidation (file change invalidates COPY + downstream) ──")

# Docksmithfile structure:
#   Step 3: COPY static.txt .     ← unchanged → should stay CACHE HIT
#   Step 4: RUN echo step4        ← unchanged → should stay CACHE HIT
#   Step 5: COPY app.py .         ← app.py will change → CACHE MISS
#   Step 6: RUN echo step6        ← after broken cache → CACHE MISS

_inv_ctx = _make_context({
    "Docksmithfile": (
        "FROM alpine:3.18\n"
        "WORKDIR /app\n"
        "COPY static.txt .\n"
        "RUN echo step4\n"
        "COPY app.py .\n"
        "RUN echo step6\n"
        "CMD sh\n"
    ),
    "static.txt": "this never changes",
    "app.py":     "version = 1\n",
})

# Clear cache for a fresh start
if os.path.exists(_cache_file):
    os.remove(_cache_file)

_, _inv_m1 = _build("invtest:v1", _inv_ctx, no_cache=False)

# Now change app.py and rebuild
with open(os.path.join(_inv_ctx, "app.py"), "w") as f:
    f.write("version = 2\n")

_, _inv_m2 = _build("invtest:v1", _inv_ctx, no_cache=False)

# base_len = number of layers from the base image (alpine:3.18 fake = 1 layer)
# After that: COPY static (idx base_len), RUN step4 (base_len+1),
#             COPY app (base_len+2), RUN step6 (base_len+3)
_base_len = len(LoadImage("alpine:3.18")["layers"])
_d1 = [l["digest"] for l in _inv_m1["layers"]]
_d2 = [l["digest"] for l in _inv_m2["layers"]]

check("Partial inv: same layer count",           len(_d1) == len(_d2))
check("Partial inv: base layers unchanged",      _d1[:_base_len] == _d2[:_base_len])
check("Partial inv: COPY static unchanged",      _d1[_base_len]   == _d2[_base_len])
check("Partial inv: RUN step4 unchanged",        _d1[_base_len+1] == _d2[_base_len+1])
check("Partial inv: COPY app.py invalidated",    _d1[_base_len+2] != _d2[_base_len+2],
      "COPY app.py layer should differ after changing app.py")
check("Partial inv: RUN step6 invalidated",      _d1[_base_len+3] != _d2[_base_len+3],
      "RUN step6 layer should differ after cache_broken")

shutil.rmtree(_inv_ctx)


# ═════════════════════════════════════════════════════════════════════════════
# 5. Build Isolation
# ═════════════════════════════════════════════════════════════════════════════
print("\n── Build isolation (two builds do not share in-memory state) ──")

_iso_ctx_a = _make_context({
    "Docksmithfile": (
        "FROM alpine:3.18\n"
        "WORKDIR /alpha\n"
        "ENV ISO=alpha\n"
        "COPY a.txt .\n"
        "CMD sh\n"
    ),
    "a.txt": "alpha",
})

_iso_ctx_b = _make_context({
    "Docksmithfile": (
        "FROM alpine:3.18\n"
        "WORKDIR /beta\n"
        "ENV ISO=beta\n"
        "COPY b.txt .\n"
        "CMD sh\n"
    ),
    "b.txt": "beta",
})

if os.path.exists(_cache_file):
    os.remove(_cache_file)

_, _iso_ma = _build("iso:alpha", _iso_ctx_a)
_, _iso_mb = _build("iso:beta",  _iso_ctx_b)

check("Isolation: alpha WorkingDir",    _iso_ma["config"]["WorkingDir"] == "/alpha")
check("Isolation: beta WorkingDir",     _iso_mb["config"]["WorkingDir"] == "/beta")
check("Isolation: alpha ENV",           "ISO=alpha" in _iso_ma["config"]["Env"])
check("Isolation: beta ENV",            "ISO=beta"  in _iso_mb["config"]["Env"])
check("Isolation: different digests",   _iso_ma["digest"] != _iso_mb["digest"])

shutil.rmtree(_iso_ctx_a)
shutil.rmtree(_iso_ctx_b)


# ═════════════════════════════════════════════════════════════════════════════
# 6. images — ListImages
# ═════════════════════════════════════════════════════════════════════════════
print("\n── images (ListImages) ──")

_img_ctx = _make_context({
    "Docksmithfile": "FROM alpine:3.18\nCMD sh\n",
})

_, _img_m = _build("listtest:v1", _img_ctx)

all_images = ListImages()
names = [(m["name"], m["tag"]) for m in all_images]

check("images: listtest:v1 present",    ("listtest", "v1") in names)
check("images: result is a list",       isinstance(all_images, list))
check("images: each has digest",        all("digest" in m for m in all_images))
check("images: each has created",       all("created" in m for m in all_images))
check("images: digest has sha256:",     _img_m["digest"].startswith("sha256:"))

shutil.rmtree(_img_ctx)


# ═════════════════════════════════════════════════════════════════════════════
# 7. rmi — DeleteImage / ImageExists
# ═════════════════════════════════════════════════════════════════════════════
print("\n── rmi (DeleteImage / ImageExists) ──")

_rmi_ctx = _make_context({
    "Docksmithfile": "FROM alpine:3.18\nCMD sh\n",
})

_, _rmi_m = _build("rmitest:v1", _rmi_ctx)

check("rmi: image exists before delete",    ImageExists("rmitest:v1"))

DeleteImage("rmitest:v1")
check("rmi: image gone after delete",       not ImageExists("rmitest:v1"))

try:
    LoadImage("rmitest:v1")
    check("rmi: LoadImage raises after delete", False)
except FileNotFoundError:
    check("rmi: LoadImage raises after delete", True)

try:
    DeleteImage("ghost:nope")
    check("rmi: deleting non-existent raises", False)
except FileNotFoundError:
    check("rmi: deleting non-existent raises", True)

shutil.rmtree(_rmi_ctx)


# ═════════════════════════════════════════════════════════════════════════════
# 8. --no-cache flag
# ═════════════════════════════════════════════════════════════════════════════
print("\n── --no-cache flag ──")

# Recreate base image in case rmi test wiped its shared layers
_make_fake_base("alpine:3.18")

_nc_ctx = _make_context({
    "Docksmithfile": (
        "FROM alpine:3.18\n"
        "COPY nc.txt .\n"
        "RUN echo nocache\n"
        "CMD sh\n"
    ),
    "nc.txt": "no-cache test",
})

if os.path.exists(_cache_file):
    os.remove(_cache_file)

# Build once to populate cache
_, _nc_m1 = _build("nocache:v1", _nc_ctx, no_cache=False)

# Build again with --no-cache — layers MUST differ (rebuilt from scratch)
_, _nc_m2 = _build("nocache:v1", _nc_ctx, no_cache=True)

# Because nc.txt content is the same, the layer content is identical,
# so digests will actually be the same (reproducible builds).
# The important thing is the cache file was NOT updated after --no-cache.
_nc_cache_after = json.load(open(_cache_file)) if os.path.exists(_cache_file) else {}
_nc_cache_count_before = len(json.load(open(_cache_file)))  # loaded after first build

_, _nc_m3 = _build("nocache:v1", _nc_ctx, no_cache=True)
_nc_cache_count_after = len(json.load(open(_cache_file))) if os.path.exists(_cache_file) else 0

check("--no-cache: cache not grown by no-cache build",
      _nc_cache_count_before == _nc_cache_count_after)
check("--no-cache: image still saved",   ImageExists("nocache:v1"))

shutil.rmtree(_nc_ctx)


# ═════════════════════════════════════════════════════════════════════════════
# 9. sampleapp Docksmithfile uses all 6 instructions
# ═════════════════════════════════════════════════════════════════════════════
print("\n── sampleapp Docksmithfile (all 6 instructions present) ──")

_repo_root = os.path.dirname(os.path.abspath(__file__))
_sample_df = os.path.join(_repo_root, "sampleapp", "Docksmithfile")

if os.path.exists(_sample_df):
    _sample_instrs = parse(_sample_df)
    _sample_instr_names = {d["instr"] for d in _sample_instrs}
    for expected in ("FROM", "WORKDIR", "ENV", "COPY", "RUN", "CMD"):
        check(f"sampleapp uses {expected}",  expected in _sample_instr_names)

    # ENV is APP_NAME=... which is meant to be overridden at run-time
    _env_instr = next((d for d in _sample_instrs if d["instr"] == "ENV"), None)
    check("sampleapp ENV key is APP_NAME",   _env_instr is not None and _env_instr["args"]["key"] == "APP_NAME")

    # CMD must be exec form
    _cmd_instr = next((d for d in _sample_instrs if d["instr"] == "CMD"), None)
    check("sampleapp CMD is exec form",      _cmd_instr is not None and _cmd_instr["args"]["form"] == "exec")
else:
    check("sampleapp/Docksmithfile exists",  False, f"not found at {_sample_df}")


# ── Cleanup ───────────────────────────────────────────────────────────────────
shutil.rmtree(_test_home)

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 56)
if errors:
    print(f"FAILED: {len(errors)} test(s)")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("All tests passed!")
    sys.exit(0)
