#!/usr/bin/env python3
"""comfy_route — send Forge Lab's renders through the Comfy SDK (Comfy API v2).

Tk-free. The rig runs Comfy's `comfy-api-proxy` in front of each ComfyUI:

    card 1 ComfyUI :8188  <-  comfy-api-proxy :8288
    card 2 ComfyUI :8189  <-  comfy-api-proxy :8289      (PROXY_OFFSET = 100)

and `submit()` in forge_lab hands the graph to `comfy_sdk.Comfy` instead of POSTing
/prompt itself. Input files (stills, voices, guide audio) become SDK *assets* (hashed with
blake3, uploaded only if the rig doesn't already have them).

WHY THE REST OF THE APP DIDN'T CHANGE: the proxy submits to ComfyUI with the SDK job id as
the ComfyUI prompt_id, and an asset lands in ComfyUI's input folder under its own file name
(both checked on the rig 2026-10-19: /queue and /history were keyed by job.id, and
LoadImage.image read the asset's basename). So run_state/collect/cards keep reading
/history and /queue exactly as before.

Settings (settings.json):
    "comfy_route": "sdk" | "direct" (the old raw /prompt). Unset: "sdk" once the proxy token
    exists (tools/rig_comfy_proxy.sh made it), else "direct".
Token: `comfy_proxy_token` beside settings.json (0600, git-ignored); the rig keeps the same
string in ~/.comfy-proxy-token. Env COMFY_PROXY_TOKEN wins.

If the SDK path fails to CONNECT (proxy down), submit() falls back to the direct path and
says so in the log; a job the proxy accepted is never re-sent (that would double-render).
"""
import json
import os
import threading

PROXY_OFFSET = 100
_STAGED = {}                 # file name the graph will carry -> local path (SDK uploads it)
_LOCK = threading.Lock()
_CLIENTS = {}


def _settings_dir():
    data = os.environ.get("FORGE_LAB_DATA")
    return os.path.expanduser(data) if data else os.path.expanduser("~/forge-lab")


def _read_settings():
    try:
        with open(os.path.join(_settings_dir(), "settings.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def sdk_available():
    try:
        import comfy_sdk  # noqa: F401
        return True
    except Exception:
        return False


def enabled():
    """True when renders should go through the Comfy SDK."""
    env = (os.environ.get("FORGE_LAB_COMFY_ROUTE") or "").strip().lower()
    route = env or str(_read_settings().get("comfy_route") or "").lower()
    if not route:                       # not chosen: on once the rig side is set up
        route = "sdk" if token() else "direct"
    return route == "sdk" and sdk_available()


def token():
    env = (os.environ.get("COMFY_PROXY_TOKEN") or "").strip()
    if env:
        return env
    try:
        with open(os.path.join(_settings_dir(), "comfy_proxy_token")) as fh:
            return fh.read().strip()
    except Exception:
        return ""


def proxy_url(host, comfy_port):
    return "http://%s:%d" % (host, int(comfy_port) + PROXY_OFFSET)


def client(host, comfy_port):
    """One comfy_sdk.Comfy per proxy. COMFY_BASE_URL is read at construction, so it is set
    under the lock and put back afterwards."""
    url = proxy_url(host, comfy_port)
    with _LOCK:
        c = _CLIENTS.get(url)
        if c is not None:
            return c
        from comfy_sdk import Comfy
        old = os.environ.get("COMFY_BASE_URL")
        os.environ["COMFY_BASE_URL"] = url
        try:
            c = Comfy(api_key=token() or None, client_info="forge-lab")
        finally:
            if old is None:
                os.environ.pop("COMFY_BASE_URL", None)
            else:
                os.environ["COMFY_BASE_URL"] = old
        _CLIENTS[url] = c
        return c


def stage(path):
    """The SDK path's stand-in for upload_image(): remember the file and return the name
    the graph will carry. The SDK uploads it (or finds the rig already has it) at submit."""
    name = os.path.basename(path)
    with _LOCK:
        prev = _STAGED.get(name)
        if prev and os.path.abspath(prev) != os.path.abspath(path):
            # two different files with one name would silently share a picture on the rig
            # (every character's voice is neutral.wav): send a uniquely named copy instead
            import hashlib
            import shutil
            import tempfile
            root, ext = os.path.splitext(name)
            h = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:8]
            name = "%s_%s%s" % (root, h, ext)
            tmp = os.path.join(tempfile.gettempdir(), "forge_lab_sdk_" + name)
            shutil.copyfile(path, tmp)
            path = tmp
        _STAGED[name] = path
    return name


def staged_inputs(graph):
    """[(node_id, input_name, local_path)] for every graph input that names a staged file."""
    out = []
    with _LOCK:
        staged = dict(_STAGED)
    for nid, node in graph.items():
        for key, val in (node.get("inputs") or {}).items():
            if isinstance(val, str) and val in staged:
                out.append((nid, key, staged[val]))
    return out


def submit(graph, host, comfy_port):
    """Submit through the SDK. Returns the job id (== ComfyUI prompt_id). Raises
    ProxyUnreachable when nothing was accepted (safe to fall back), anything else as is."""
    c = client(host, comfy_port)
    wf = c.workflows.from_json(json.loads(json.dumps(graph)))
    for nid, key, path in staged_inputs(graph):
        wf.set_input(nid, key, c.assets.from_file(path))
    try:
        job = c.submit(wf)
    except Exception as exc:
        if _is_connect_error(exc):
            raise ProxyUnreachable(str(exc)) from exc
        raise
    return job.id


class ProxyUnreachable(Exception):
    """The proxy could not be reached; nothing was submitted."""


def _is_connect_error(exc):
    seen = set()
    e = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        n = type(e).__name__
        if n in ("ConnectError", "ConnectTimeout", "ConnectionRefusedError",
                 "ConnectionError") or "Connection refused" in str(e):
            return True
        e = e.__cause__ or e.__context__
    return False


def staged_paths(graph):
    return [p for _n, _k, p in staged_inputs(graph)]
