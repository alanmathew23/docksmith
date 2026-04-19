#!/usr/bin/env python3
"""
main.py — Member 1
The docksmith CLI entry point.

Usage:
    python3 main.py build -t <name:tag> [--no-cache] <context>
    python3 main.py images
    python3 main.py rmi <name:tag>
    python3 main.py run [-e KEY=VALUE ...] <name:tag> [cmd ...]
    python3 main.py import-image <tar_path> <name:tag>
"""

import sys
import os
import argparse
import time
import shutil
import tempfile

from storage import (
    init_store,
    LoadImage,
    SaveImage,
    WriteLayer,
    ListImages,
    DeleteImage,
    ImageExists,
    ExtractLayers,
)


# ── build ─────────────────────────────────────────────────────────────────────

def cmd_build(args):
    from build_engine import build# Member 2

    context_path = os.path.abspath(args.context)
    dockerfile   = os.path.join(context_path, "Docksmithfile")

    if not os.path.isdir(context_path):
        die(f"Context directory not found: {context_path}")
    if not os.path.exists(dockerfile):
        die(f"No Docksmithfile found in: {context_path}")

    build(args.tag, context_path, dockerfile=dockerfile, no_cache=args.no_cache)


# ── images ────────────────────────────────────────────────────────────────────

def cmd_images(args):
    """
    Spec 7: Columns: Name, Tag, ID (first 12 chars of digest hex), Created.
    """
    manifests = ListImages()

    if not manifests:
        print("No images found.")
        return

    fmt = "{:<20} {:<12} {:<14} {}"
    print(fmt.format("NAME", "TAG", "ID", "CREATED"))
    print("-" * 65)

    for m in manifests:
        name    = m.get("name", "?")
        tag     = m.get("tag", "?")
        digest  = m.get("digest", "")
        # Spec: "first 12 characters of the digest"
        # digest format is "sha256:<64 hex chars>"; ID = first 12 of the hex part
        short_id = digest[7:19] if digest.startswith("sha256:") else digest[:12]
        created  = m.get("created", "?")
        print(fmt.format(name, tag, short_id, created))


# ── rmi ───────────────────────────────────────────────────────────────────────

def cmd_rmi(args):
    """
    Spec 7: Remove manifest + all layer files. Fail clearly if image not found.
    """
    if not ImageExists(args.name_tag):
        die(f"Image '{args.name_tag}' not found.")

    DeleteImage(args.name_tag)
    print(f"Deleted: {args.name_tag}")


# ── run ───────────────────────────────────────────────────────────────────────

def cmd_run(args):
    """
    Spec 6 + 7:
      - Assemble layers into a temp dir via storage.ExtractLayers.
      - Apply image ENV; -e overrides take precedence (spec 6).
      - WorkingDir from image config; defaults to / if not set (spec 6).
      - Pass command as list to RunIsolated (Member 3).
      - Block until process exits, print exit code, clean up temp dir.
      - Fail clearly if no CMD defined and no command override given.
    """
    from runtime import RunIsolated # Member 3

    # Parse -e KEY=VALUE overrides
    env_overrides = {}
    for kv in (args.env or []):
        if "=" not in kv:
            die(f"Invalid -e argument '{kv}'. Expected format: KEY=VALUE")
        k, v = kv.split("=", 1)
        env_overrides[k] = v

    manifest = LoadImage(args.name_tag)

    config        = manifest.get("config", {})
    layer_digests = [l["digest"] for l in manifest.get("layers", [])]
    workdir       = config.get("WorkingDir") or "/"   # spec: default to /
    image_cmd     = config.get("Cmd")
    image_env_raw = config.get("Env") or []

    # Build env dict: image ENV first, -e overrides win (spec 6)
    env = {}
    for item in image_env_raw:
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
    env.update(env_overrides)

    # Resolve command to run (spec 6: fail clearly if nothing defined)
    cmd_override = args.cmd  # list from REMAINDER, may be empty
    if cmd_override:
        command = cmd_override
    elif image_cmd:
        command = image_cmd if isinstance(image_cmd, list) else [image_cmd]
    else:
        die(
            f"No CMD is defined in image '{args.name_tag}' and no command was given.\n"
            f"Provide a command: docksmith run {args.name_tag} <command>"
        )

    # Assemble filesystem from layers, run isolated, clean up
    tmpdir = tempfile.mkdtemp(prefix="docksmith-run-")
    try:
        ExtractLayers(layer_digests, tmpdir)
        exit_code = RunIsolated(tmpdir, command, env, workdir)
        print(f"Container exited with code {exit_code}")
        sys.exit(exit_code)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)   # always clean up


# ── import-image ──────────────────────────────────────────────────────────────

def cmd_import(args):
    """
    One-time setup: import a base image tar (docker save format) into the local store.
    After this, all builds and runs work fully offline (spec 4.3 + 8).

    Reads the docker-save manifest.json to find layer order and config path.
    """
    import tarfile as tarlib
    import json as jsonlib

    tar_path = args.tar_path
    name_tag = args.name_tag

    if not os.path.exists(tar_path):
        die(f"File not found: {tar_path}")

    name, tag = (name_tag.rsplit(":", 1) if ":" in name_tag else (name_tag, "latest"))
    print(f"Importing '{tar_path}' as {name}:{tag} ...")

    layers_imported = []

    with tarlib.open(tar_path, "r:*") as tar:
        # Read Docker manifest.json for layer order and config path
        try:
            mf = tar.extractfile(tar.getmember("manifest.json"))
            docker_manifest = jsonlib.load(mf)
            layer_paths = docker_manifest[0]["Layers"]
            config_path = docker_manifest[0]["Config"]
        except (KeyError, IndexError):
            die(
                "manifest.json not found or malformed.\n"
                "Ensure the tar was produced by: docker save <image> -o image.tar"
            )

        # Read Docker image config for ENV, CMD, WorkingDir
        try:
            cfg_f = tar.extractfile(tar.getmember(config_path))
            docker_config = jsonlib.load(cfg_f)
            container_cfg = docker_config.get("config") or {}
        except Exception:
            container_cfg = {}

        # Import each layer tar into the local store
        for lpath in layer_paths:
            try:
                lfile = tar.extractfile(tar.getmember(lpath))
                raw   = lfile.read()
            except KeyError:
                die(f"Layer not found in tar: {lpath}")

            digest, size = WriteLayer(raw)
            layers_imported.append({
                "digest":    digest,
                "size":      size,
                "createdBy": f"imported:{os.path.basename(tar_path)}",
            })
            print(f"  + {digest[:19]}... ({size:,} bytes)")

    manifest = {
        "name":    name,
        "tag":     tag,
        "digest":  "",   # computed by SaveImage
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "Env":        container_cfg.get("Env") or [],
            "Cmd":        container_cfg.get("Cmd"),
            "WorkingDir": container_cfg.get("WorkingDir") or "/",
        },
        "layers": layers_imported,
    }

    SaveImage(manifest)
    print(f"\nImported {name}:{tag}  ({len(layers_imported)} layers)")
    print(f"Digest: {manifest['digest']}")


# ── helpers ───────────────────────────────────────────────────────────────────

def die(msg: str):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


# ── CLI wiring ────────────────────────────────────────────────────────────────

def main():
    init_store()

    parser = argparse.ArgumentParser(
        prog="docksmith",
        description="A simplified Docker-like build and runtime system.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # build
    p_build = sub.add_parser("build", help="Build an image from a Docksmithfile")
    p_build.add_argument("-t", dest="tag", required=True, metavar="name:tag")
    p_build.add_argument("--no-cache", action="store_true",
                         help="Skip all cache lookups and writes")
    p_build.add_argument("context", help="Build context directory")
    p_build.set_defaults(func=cmd_build)

    # images
    p_images = sub.add_parser("images", help="List all images in the local store")
    p_images.set_defaults(func=cmd_images)

    # rmi
    p_rmi = sub.add_parser("rmi", help="Remove an image and its layer files")
    p_rmi.add_argument("name_tag", metavar="name:tag")
    p_rmi.set_defaults(func=cmd_rmi)

    # run
    p_run = sub.add_parser("run", help="Run a container from an image")
    p_run.add_argument(
        "-e", dest="env", action="append", metavar="KEY=VALUE",
        help="Set/override environment variable (repeatable)"
    )
    p_run.add_argument("name_tag", metavar="name:tag")
    p_run.add_argument("cmd", nargs=argparse.REMAINDER,
                       help="Command override (optional)")
    p_run.set_defaults(func=cmd_run)

    # import-image (one-time setup)
    p_import = sub.add_parser(
        "import-image",
        help="Import a base image tar into the local store (one-time setup)"
    )
    p_import.add_argument("tar_path", help="Path to docker-save tar file")
    p_import.add_argument("name_tag", metavar="name:tag",
                          help="Name and tag to assign (e.g. alpine:3.18)")
    p_import.set_defaults(func=cmd_import)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
