#!/usr/bin/env python3
"""forge_core — a thin, Tk-FREE facade over forge_lab for headless callers.

Why: `forge_lab.py` is an 11k-line single-file tkinter app, but it ends with
`if __name__ == "__main__": launch()`, so the MODULE is importable and every builder is
callable with no window and no GPU. This facade gives the rig (and any headless script a
stable, documented surface: import one module and call the app's OWN submit()/build_graph()/
estimate_seconds() — so a headless run lands in Forge Lab's Takes index exactly like a UI run.

⚠️ Importing this module must NOT import tkinter. forge_lab.py imports tkinter only INSIDE
functions (never at module top), so `import forge_core` stays Tk-free — asserted by
tools/core_matrix.py, which runs the import in a subprocess and checks `tkinter` is absent
from sys.modules.

Pointing at a different machine / data dir
------------------------------------------
`import forge_lab` runs at THIS module's import time, so setting os.environ *after* the
import would be too late for module-level globals (DATA/RUNS/HOSTFILE/CHARACTERS, FORGE).
Two supported ways:

  * Set the env BEFORE importing forge_core (best for the rig):
        FORGE_LAB_DATA=/tmp/fcore/data FORGE_URL=http://127.0.0.1:8188 python3 -c "import forge_core"
    `FORGE_LAB_DATA` is honoured by forge_lab's own DATA line; `FORGE_URL` seeds CANDIDATES[0].

  * Call configure() after importing (safe, in-place rebind of the module globals):
        import forge_core as c
        c.configure(data_dir="/tmp/fcore/data", forge_url="http://127.0.0.1:8188")

configure() rebinds forge_lab's DATA/RUNS/HOSTFILE/CHARACTERS and FORGE/F_HOST/F_PORT in
place AND sets the env vars, so an in-process caller and any subprocess it spawns agree.
"""
import os
import urllib.parse

import forge_lab as _fl

# The stable API this facade promises. Exposed dynamically (see __getattr__) so that a name
# like RUNS reflects configure()'s rebind rather than a stale import-time copy.
_EXPORTS = (
    "MODES", "MODEL_CHOICES", "engine_of", "h3_frames", "build_graph", "estimate_seconds",
    "submit", "run_state", "collect", "wait_and_collect", "read_index", "read_character",
    "list_characters", "write_character", "character_still", "compose_character_prompt",
    "upload_image", "get_json", "post_json", "video_probe", "image_size",
    "RUNS", "DATA",
)

__all__ = list(_EXPORTS) + ["configure"]


def __getattr__(name):
    # PEP 562: `forge_core.RUNS` / `from forge_core import RUNS` read the live forge_lab value.
    if name in _EXPORTS:
        return getattr(_fl, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def configure(data_dir=None, forge_url=None):
    """Point this process at its own data dir and ComfyUI URL.

    Safe approach: rebind forge_lab's already-imported module globals IN PLACE (rather than
    re-importing) and mirror the same values into os.environ so spawned subprocesses inherit
    them. `data_dir` also moves CHARACTERS, because the character store lives under DATA.

    Returns a dict of the effective {data_dir, runs, characters, forge}.
    """
    if data_dir:
        d = os.path.expanduser(str(data_dir))
        _fl.DATA = d
        _fl.RUNS = os.path.join(d, "runs")
        _fl.HOSTFILE = os.path.join(d, "forge_host.txt")
        # CHARACTERS is derived from DATA in forge_lab (`os.path.join(DATA, "characters")`).
        _fl.CHARACTERS = os.path.join(d, "characters")
        os.makedirs(_fl.RUNS, exist_ok=True)
        os.environ["FORGE_LAB_DATA"] = d

    if forge_url:
        u = str(forge_url).rstrip("/")
        _fl.FORGE = u
        _fl.CANDIDATES = [u] + [c for c in _fl.CANDIDATES if c != u]
        pr = urllib.parse.urlparse(u)
        _fl.F_HOST = pr.hostname or "forge"
        _fl.F_PORT = pr.port or 8188
        # Trust the explicit URL: mark resolved so get_json()/post_json() use it directly
        # instead of probing CANDIDATES and possibly falling back to another host.
        _fl._ok = True
        os.environ["FORGE_URL"] = u

    return {"data_dir": _fl.DATA, "runs": _fl.RUNS,
            "characters": _fl.CHARACTERS, "forge": _fl.FORGE}