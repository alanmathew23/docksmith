import hashlib
import os

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def hash_copy_sources(src_paths):
    all_files = []

    for src in src_paths:
        if os.path.isfile(src):
            all_files.append(src)
        else:
            for root, _, files in os.walk(src):
                for f in files:
                    all_files.append(os.path.join(root, f))

    all_files.sort()

    h = hashlib.sha256()
    for f in all_files:
        h.update(f.encode())
        h.update(sha256_file(f).encode())

    return h.hexdigest()


def serialize_env(env_dict):
    if not env_dict:
        return ""
    return ";".join(f"{k}={v}" for k, v in sorted(env_dict.items()))


def compute_cache_key(prev_digest, instr_text, workdir, env_dict, copy_hash=None):
    h = hashlib.sha256()

    h.update((prev_digest or "").encode())
    h.update(instr_text.encode())
    h.update((workdir or "").encode())
    h.update(serialize_env(env_dict).encode())

    if copy_hash:
        h.update(copy_hash.encode())

    return h.hexdigest()