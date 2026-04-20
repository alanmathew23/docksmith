import os
import glob as _glob_mod
import time
import json
import tempfile
import shutil
import stat

from storage import (
    LoadImage,
    SaveImage,
    WriteLayer,
    CreateTar,
    ExtractLayers,
    CACHE_DIR,
)

from cache import compute_cache_key, hash_copy_sources
from stubs import RunIsolated
from parser import parse as parse_docksmithfile, expand_copy_srcs


class BuildEngine:
    def __init__(self, dockerfile, name_tag, context_path, no_cache=False):
        self.file = dockerfile
        self.context = context_path
        self.name, self.tag = self._parse_name_tag(name_tag)

        self.layers = []
        self.env = {}
        self.workdir = ""
        self.cmd = None

        self.no_cache = no_cache
        self.cache = self._load_cache()
        self.cache_broken = False

    def _parse_name_tag(self, name_tag):
        if ":" in name_tag:
            return name_tag.rsplit(":", 1)
        return name_tag, "latest"

    def _cache_path(self):
        # Use storage.CACHE_DIR so test redirects work automatically.
        return os.path.join(CACHE_DIR, "cache.json")

    def _load_cache(self):
        path = self._cache_path()
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {}

    def _save_cache(self):
        os.makedirs(os.path.dirname(self._cache_path()), exist_ok=True)
        with open(self._cache_path(), "w") as f:
            json.dump(self.cache, f, indent=2)

    def _env_to_list(self):
        return [f"{k}={v}" for k, v in sorted(self.env.items())]

    def build(self):
        steps = parse_docksmithfile(self.file)

        prev_digest = None

        for step_dict in steps:
            i      = step_dict["lineno"]
            instr  = step_dict["instr"]
            raw    = step_dict["raw"]
            args   = step_dict["args"]
            # Human-readable label used in cache key and print output
            step_label = f"{instr} {raw}"

            start = time.time()

            # ── FROM ─────────────────────
            if instr == "FROM":
                base = LoadImage(args["image"])

                self.layers = list(base["layers"])
                prev_digest = base["digest"]

                self.workdir = base["config"].get("WorkingDir") or "/"
                self.cmd = base["config"].get("Cmd")

                for e in base["config"].get("Env") or []:
                    if "=" in e:
                        k, v = e.split("=", 1)
                        self.env[k] = v

                print(f"Step {i} : FROM {args['image']}")
                continue

            # ── WORKDIR ─────────────────
            if instr == "WORKDIR":
                self.workdir = args["path"]
                print(f"Step {i} : WORKDIR {args['path']}")
                continue

            # ── ENV ─────────────────────
            if instr == "ENV":
                self.env[args["key"]] = args["value"]
                print(f"Step {i} : ENV {raw}")
                continue

            # ── CMD ─────────────────────
            if instr == "CMD":
                self.cmd = args["cmd"]   # already a list (exec or shell form)
                print(f"Step {i} : CMD {raw}")
                continue

            # ── COPY / RUN ──────────────
            copy_hash = None

            if instr == "COPY":
                # Expand glob patterns for cache-key computation
                try:
                    expanded = expand_copy_srcs(args["srcs"], self.context)
                except ValueError as exc:
                    raise RuntimeError(f"Line {i}: {exc}") from exc
                copy_hash = hash_copy_sources(expanded)

            cache_key = compute_cache_key(
                prev_digest,
                step_label,
                self.workdir,
                self.env,
                copy_hash,
            )

            hit = (
                not self.no_cache
                and not self.cache_broken
                and cache_key in self.cache
            )

            if hit:
                layer = self.cache[cache_key]
                self.layers.append(layer)
                prev_digest = layer["digest"]
                print(f"Step {i} : {step_label} [CACHE HIT] {time.time()-start:.2f}s")
                continue

            # ── CACHE MISS ──────────────
            self.cache_broken = True

            tmp = tempfile.mkdtemp()
            ExtractLayers([l["digest"] for l in self.layers], tmp)

            if instr == "COPY":
                dst = args["dst"]
                dst_path = os.path.join(tmp, self.workdir.lstrip("/"), dst.lstrip("/"))

                if len(expanded) == 1 and os.path.isfile(expanded[0]) and not dst.endswith("/"):
                    # Single file → copy to exact destination path
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    shutil.copy2(expanded[0], dst_path)
                else:
                    # Multiple files or directory → destination is a directory
                    os.makedirs(dst_path, exist_ok=True)
                    for src_path in expanded:
                        if os.path.isdir(src_path):
                            shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
                        else:
                            shutil.copy2(src_path, dst_path)

            elif instr == "RUN":
                RunIsolated(tmp, args["command"], self.env, self.workdir)

            tar_bytes = CreateTar(tmp)
            digest, size = WriteLayer(tar_bytes)

            layer_entry = {
                "digest":    digest,
                "size":      size,
                "createdBy": step_label,
            }

            self.layers.append(layer_entry)

            if not self.no_cache:
                self.cache[cache_key] = layer_entry

            prev_digest = digest

            def _on_rm_error(func, path, exc_info):
                os.chmod(path, stat.S_IWRITE)
                func(path)

            shutil.rmtree(tmp, onerror=_on_rm_error)

            print(f"Step {i} : {step_label} [CACHE MISS] {time.time()-start:.2f}s")

        self._save_cache()

        manifest = {
            "name":   self.name,
            "tag":    self.tag,
            "layers": self.layers,
            "config": {
                "Env":        self._env_to_list(),
                "Cmd":        self.cmd,
                "WorkingDir": self.workdir,
            },
            "created": None,
            "digest":  "",
        }

        SaveImage(manifest)

        print(f"\nSuccessfully built {self.name}:{self.tag}")


# ── REQUIRED WRAPPER FOR CLI ─────────────────
def build(tag, context_path, dockerfile="Docksmithfile", no_cache=False):
    engine = BuildEngine(dockerfile, tag, context_path, no_cache)
    engine.build()