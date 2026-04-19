import os
from os import path, stat
import time
import json
import tempfile
import shutil

from storage import (
    LoadImage,
    SaveImage,
    WriteLayer,
    CreateTar,
    ExtractLayers
)

from cache import compute_cache_key, hash_copy_sources
from stubs import RunIsolated


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
        return os.path.expanduser("~/.docksmith/cache/cache.json")

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

    def _parse_file(self):
        with open(self.file) as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]

    def _env_to_list(self):
        return [f"{k}={v}" for k, v in sorted(self.env.items())]

    def build(self):
        steps = self._parse_file()

        prev_digest = None

        for i, step in enumerate(steps, 1):
            start = time.time()

            instr, *rest = step.split(" ", 1)
            arg = rest[0] if rest else ""

            # ── FROM ─────────────────────
            if instr == "FROM":
                base = LoadImage(arg)

                self.layers = list(base["layers"])
                prev_digest = base["digest"]

                self.workdir = base["config"]["WorkingDir"]
                self.cmd = base["config"]["Cmd"]

                for e in base["config"]["Env"]:
                    k, v = e.split("=", 1)
                    self.env[k] = v

                print(f"Step {i} : FROM {arg}")
                continue

            # ── WORKDIR ─────────────────
            if instr == "WORKDIR":
                self.workdir = arg
                print(f"Step {i} : WORKDIR {arg}")
                continue

            # ── ENV ─────────────────────
            if instr == "ENV":
                k, v = arg.split("=", 1)
                self.env[k] = v
                print(f"Step {i} : ENV {arg}")
                continue

            # ── CMD ─────────────────────
            if instr == "CMD":
                self.cmd = arg.split()
                print(f"Step {i} : CMD {arg}")
                continue

            # ── COPY / RUN ──────────────
            copy_hash = None

            if instr == "COPY":
                src, dst = arg.split()
                src_path = os.path.join(self.context, src)
                copy_hash = hash_copy_sources([src_path])

            cache_key = compute_cache_key(
                prev_digest,
                step,
                self.workdir,
                self.env,
                copy_hash
            )

            hit = (
                not self.no_cache and
                not self.cache_broken and
                cache_key in self.cache
            )

            if hit:
                layer = self.cache[cache_key]
                self.layers.append(layer)
                prev_digest = layer["digest"]

                print(f"Step {i} : {step} [CACHE HIT] {time.time()-start:.2f}s")
                continue

            # ── MISS ────────────────────
            self.cache_broken = True

            tmp = tempfile.mkdtemp()
            ExtractLayers([l["digest"] for l in self.layers], tmp)

            if instr == "COPY":
                src, dst = arg.split()
                src_path = os.path.join(self.context, src)

                dst_path = os.path.join(tmp, self.workdir.lstrip("/"), dst)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)

                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
                else:
                    shutil.copy2(src_path, dst_path)

            elif instr == "RUN":
                RunIsolated(tmp, arg, self.env, self.workdir)

            tar_bytes = CreateTar(tmp)
            digest, size = WriteLayer(tar_bytes)

            layer_entry = {
                "digest": digest,
                "size": size,
                "createdBy": step
            }

            self.layers.append(layer_entry)

            if not self.no_cache:
                self.cache[cache_key] = layer_entry

            prev_digest = digest

            import stat

            def on_rm_error(func, path, exc_info):
                os.chmod(path, stat.S_IWRITE)
                func(path)

            shutil.rmtree(tmp, onerror=on_rm_error)


            print(f"Step {i} : {step} [CACHE MISS] {time.time()-start:.2f}s")

        self._save_cache()

        manifest = {
            "name": self.name,
            "tag": self.tag,
            "layers": self.layers,
            "config": {
                "Env": self._env_to_list(),
                "Cmd": self.cmd,
                "WorkingDir": self.workdir
            },
            "created": None,
            "digest": ""
        }

        SaveImage(manifest)

        print(f"\nSuccessfully built {self.name}:{self.tag}")


# ── REQUIRED WRAPPER FOR CLI ─────────────────
def build(tag, context_path, dockerfile="Docksmithfile", no_cache=False):
    engine = BuildEngine(dockerfile, tag, context_path, no_cache)
    engine.build()