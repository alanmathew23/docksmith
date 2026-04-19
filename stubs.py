"""
stubs.py — FOR DEVELOPMENT/TESTING ONLY
Member 1 uses these stubs to test main.py and storage.py independently,
before Members 2 and 3 deliver their modules.

DO NOT include this file in the final submission.
Replace with the real build_engine.py and runtime.py from teammates.
"""

# ── Stub for Member 2: build_engine.py ───────────────────────────────────────

def build(tag, context_path, dockerfile="Docksmithfile", no_cache=False):
    """Stub: prints a fake build. Replace with Member 2's real implementation."""
    print(f"[STUB] build called: tag={tag}, context={context_path}, no_cache={no_cache}")
    print("Step 1/2 : FROM alpine:3.18")
    print("Step 2/2 : RUN echo hello [CACHE MISS] 0.01s")
    print("\n✅ Build complete! (STUB)")


# ── Stub for Member 3: runtime.py ────────────────────────────────────────────

def RunIsolated(rootfs, command, env, workdir):
    """Stub: prints what would run. Replace with Member 3's real implementation."""
    print(f"[STUB] RunIsolated called")
    print(f"  rootfs  = {rootfs}")
    print(f"  command = {command}")
    print(f"  workdir = {workdir}")
    print(f"  env     = {env}")
    return 0


def AssembleLayers(layer_digests, dest_dir):
    """Stub: calls the real storage.ExtractLayers (Member 1 owns this)."""
    from storage import ExtractLayers
    ExtractLayers(layer_digests, dest_dir)
