"""
parser.py — Member 4
Parses a Docksmithfile into a list of structured instruction dicts.

Supported instructions: FROM, WORKDIR, ENV, COPY, RUN, CMD

Features:
  - Line-numbered ParseError for all syntax problems
  - COPY srcs accept shell glob patterns (*, ?, [...])
  - CMD exec form:  CMD ["executable", "arg1", "arg2"]
  - CMD shell form: CMD echo hello

Public API:
    parse(dockerfile_path: str) -> list[dict]

    Each dict has:
        lineno : int   — 1-based line number in the file
        instr  : str   — "FROM" | "WORKDIR" | "ENV" | "COPY" | "RUN" | "CMD"
        raw    : str   — verbatim argument text after the instruction keyword
        args   : dict  — instruction-specific parsed data (see below)

    args shapes:
        FROM    -> {"image": str}
        WORKDIR -> {"path": str}
        ENV     -> {"key": str, "value": str}
        COPY    -> {"srcs": list[str], "dst": str}
                   srcs may contain glob patterns; call expand_copy_srcs() to resolve them
        RUN     -> {"command": str}
        CMD     -> {"form": "exec"|"shell", "cmd": list[str]}
"""

import json
import glob as _glob_mod
import os

SUPPORTED_INSTRUCTIONS = frozenset({"FROM", "WORKDIR", "ENV", "COPY", "RUN", "CMD"})


# ── Public exception ──────────────────────────────────────────────────────────

class ParseError(Exception):
    """Raised by parse() when the Docksmithfile contains a syntax error."""

    def __init__(self, lineno: int, message: str):
        super().__init__(f"Docksmithfile:{lineno}: {message}")
        self.lineno = lineno


# ── Main entry point ──────────────────────────────────────────────────────────

def parse(dockerfile_path: str) -> list:
    """
    Parse a Docksmithfile and return a list of instruction dicts.

    Blank lines and lines starting with '#' are ignored.
    Raises ParseError (with line number) on any syntax problem.
    Raises FileNotFoundError if the file does not exist.
    """
    instructions = []

    with open(dockerfile_path, "r") as fh:
        lines = fh.readlines()

    for lineno, raw_line in enumerate(lines, 1):
        line = raw_line.strip()

        # Skip blank lines and comments
        if not line or line.startswith("#"):
            continue

        # Split keyword from the rest
        parts = line.split(None, 1)
        instr = parts[0].upper()
        raw_arg = parts[1].strip() if len(parts) > 1 else ""

        if instr not in SUPPORTED_INSTRUCTIONS:
            raise ParseError(lineno, f"Unknown instruction '{parts[0]}'")

        args = _parse_args(instr, raw_arg, lineno)

        instructions.append({
            "lineno": lineno,
            "instr":  instr,
            "raw":    raw_arg,
            "args":   args,
        })

    if not instructions:
        raise ParseError(1, "Docksmithfile is empty")

    if instructions[0]["instr"] != "FROM":
        raise ParseError(
            instructions[0]["lineno"],
            "First instruction must be FROM"
        )

    return instructions


# ── Glob helper ───────────────────────────────────────────────────────────────

def expand_copy_srcs(srcs: list, context_dir: str) -> list:
    """
    Expand a COPY instruction's srcs list against context_dir.

    Each entry in srcs may be a plain path or a shell glob pattern
    (*, ?, [...]).  Returns a sorted, deduplicated list of absolute paths
    that matched.  Raises ValueError if a pattern matches nothing.
    """
    matched = []
    for pattern in srcs:
        full_pattern = os.path.join(context_dir, pattern)
        hits = sorted(_glob_mod.glob(full_pattern, recursive=True))
        if not hits:
            raise ValueError(
                f"COPY source pattern '{pattern}' matched no files "
                f"in context '{context_dir}'"
            )
        for h in hits:
            if h not in matched:
                matched.append(h)
    return matched


# ── Per-instruction argument parsers ──────────────────────────────────────────

def _parse_args(instr: str, raw: str, lineno: int) -> dict:
    if instr == "FROM":
        return _parse_from(raw, lineno)
    if instr == "WORKDIR":
        return _parse_workdir(raw, lineno)
    if instr == "ENV":
        return _parse_env(raw, lineno)
    if instr == "COPY":
        return _parse_copy(raw, lineno)
    if instr == "RUN":
        return _parse_run(raw, lineno)
    if instr == "CMD":
        return _parse_cmd(raw, lineno)
    # Should never reach here given the SUPPORTED_INSTRUCTIONS check above.
    raise ParseError(lineno, f"Unhandled instruction '{instr}'")  # pragma: no cover


def _parse_from(raw: str, lineno: int) -> dict:
    if not raw:
        raise ParseError(lineno, "FROM requires an image name")
    # Optional alias: FROM image AS alias
    parts = raw.split()
    image = parts[0]
    return {"image": image}


def _parse_workdir(raw: str, lineno: int) -> dict:
    if not raw:
        raise ParseError(lineno, "WORKDIR requires a path argument")
    return {"path": raw}


def _parse_env(raw: str, lineno: int) -> dict:
    if "=" not in raw:
        raise ParseError(lineno, "ENV requires KEY=VALUE format")
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        raise ParseError(lineno, "ENV key cannot be empty")
    return {"key": key, "value": value}


def _parse_copy(raw: str, lineno: int) -> dict:
    parts = raw.split()
    if len(parts) < 2:
        raise ParseError(lineno, "COPY requires at least one source and a destination: COPY SRC DEST")
    # Last token is always the destination; everything before it is a source/glob
    srcs = parts[:-1]
    dst  = parts[-1]
    return {"srcs": srcs, "dst": dst}


def _parse_run(raw: str, lineno: int) -> dict:
    if not raw:
        raise ParseError(lineno, "RUN requires a command")
    return {"command": raw}


def _parse_cmd(raw: str, lineno: int) -> dict:
    stripped = raw.strip()
    if not stripped:
        raise ParseError(lineno, "CMD requires a command")

    if stripped.startswith("["):
        # Exec form: CMD ["executable", "arg1", ...]
        try:
            cmd_list = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ParseError(lineno, f"CMD exec form is not valid JSON: {exc}") from exc
        if not isinstance(cmd_list, list):
            raise ParseError(lineno, "CMD exec form must be a JSON array")
        if not all(isinstance(s, str) for s in cmd_list):
            raise ParseError(lineno, "CMD exec form array must contain only strings")
        return {"form": "exec", "cmd": cmd_list}
    else:
        # Shell form: CMD echo hello world
        return {"form": "shell", "cmd": stripped.split()}
