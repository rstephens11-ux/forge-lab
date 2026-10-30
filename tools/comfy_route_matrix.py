#!/usr/bin/env python3
"""GPU-free checks for comfy_route.py — renders sent through the Comfy SDK.

    /opt/homebrew/bin/python3 tools/comfy_route_matrix.py

A fake `comfy_sdk` is put in sys.modules, so nothing touches the rig: it records the graph,
the assets and the submit. forge_lab.post_json and the direct upload are replaced by
recorders, so the test can tell which path a render took.
"""
import json
import os
import struct
import sys
import tempfile
import types
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
DATA = tempfile.mkdtemp(prefix="crm_")
os.environ["FORGE_LAB_DATA"] = DATA
os.environ["FORGE_LAB_NOAUTOSAVE"] = "1"
for _k in ("FORGE_LAB_COMFY_ROUTE", "COMFY_PROXY_TOKEN", "COMFY_BASE_URL"):
    os.environ.pop(_k, None)

FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


# ---- a fake comfy_sdk -----------------------------------------------------------------
REC = {"clients": [], "graphs": [], "assets": [], "set": [], "fail": None}


class _Asset:
    def __init__(self, path):
        self.file_path = os.path.basename(path)
        self.path = path


class _WF:
    def __init__(self, g):
        self.json = g

    def set_input(self, nid, key, val):
        REC["set"].append((nid, key, val))
        self.json[nid]["inputs"][key] = val


class _Job:
    id = "job-123"


class _Comfy:
    def __init__(self, *, api_key=None, client_info=None):
        REC["clients"].append({"base": os.environ.get("COMFY_BASE_URL"), "key": api_key,
                               "info": client_info})
        self.workflows = types.SimpleNamespace(from_json=self._from_json)
        self.assets = types.SimpleNamespace(from_file=self._asset)

    def _from_json(self, g):
        REC["graphs"].append(g)
        return _WF(g)

    def _asset(self, path):
        REC["assets"].append(path)
        return _Asset(path)

    def submit(self, wf):
        if REC["fail"]:
            raise REC["fail"]
        return _Job()


class ConnectError(Exception):
    pass


fake = types.ModuleType("comfy_sdk")
fake.Comfy = _Comfy
sys.modules["comfy_sdk"] = fake

import comfy_route as CR  # noqa: E402

print("\nWHEN THE SDK ROUTE IS ON")
check(not CR.enabled(), "no token, no setting -> direct (a fresh checkout is unchanged)")
with open(os.path.join(DATA, "comfy_proxy_token"), "w") as fh:
    fh.write("tok-abc\n")
check(CR.token() == "tok-abc", "the token is read from beside settings.json (trimmed)")
check(CR.enabled(), "token present + SDK importable -> SDK route")
with open(os.path.join(DATA, "settings.json"), "w") as fh:
    json.dump({"comfy_route": "direct"}, fh)
check(not CR.enabled(), "settings comfy_route=direct wins")
os.environ["FORGE_LAB_COMFY_ROUTE"] = "sdk"
check(CR.enabled(), "FORGE_LAB_COMFY_ROUTE=sdk wins over settings")
os.environ.pop("FORGE_LAB_COMFY_ROUTE")
with open(os.path.join(DATA, "settings.json"), "w") as fh:
    json.dump({"comfy_route": "sdk"}, fh)
_saved = sys.modules.pop("comfy_sdk")
sys.modules["comfy_sdk"] = None            # import fails
check(not CR.enabled(), "SDK not installed -> direct, never a crash")
sys.modules["comfy_sdk"] = _saved
check(CR.proxy_url("forge.local", 8188) == "http://forge.local:8288"
      and CR.proxy_url("forge.local", 8189) == "http://forge.local:8289",
      "card 1 -> proxy :8288, card 2 -> proxy :8289")

print("\nSTAGING")
a = os.path.join(DATA, "a"); b = os.path.join(DATA, "b")
os.makedirs(a); os.makedirs(b)
for d, data in ((a, b"one"), (b, b"two")):
    with open(os.path.join(d, "neutral.wav"), "wb") as fh:
        fh.write(data)
n1 = CR.stage(os.path.join(a, "neutral.wav"))
n2 = CR.stage(os.path.join(b, "neutral.wav"))
n1b = CR.stage(os.path.join(a, "neutral.wav"))
check(n1 == "neutral.wav" and n1b == n1, "a file is staged under its own name, idempotently")
check(n2 != n1 and n2.startswith("neutral_") and n2.endswith(".wav"),
      "a DIFFERENT file with the same name gets its own name (%s)" % n2)
check(open(CR._STAGED[n2], "rb").read() == b"two", "...and that name carries the second file")


# ---- forge_lab.submit through the route -----------------------------------------------
def png(path, w=64, h=36):
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


import importlib.util  # noqa: E402
sp = importlib.util.spec_from_file_location("forge_lab", os.path.join(ROOT, "forge_lab.py"))
fl = importlib.util.module_from_spec(sp)
sys.modules["forge_lab"] = fl
sp.loader.exec_module(fl)

POSTS, DIRECT_UPLOADS = [], []
fl.post_json = lambda path, obj, timeout=60, base=None: (POSTS.append((path, obj, base))
                                                         or {"prompt_id": "direct-1"})
_real_upload = fl.upload_image


def _upload(path):
    if getattr(fl._SUBMIT_TL, "sdk", False):
        return _real_upload(path)            # staging: the code under test
    DIRECT_UPLOADS.append(path)
    return os.path.basename(path)


fl.upload_image = _upload
ref = os.path.join(DATA, "margaret.png")
png(ref)

print("\nSUBMIT THROUGH THE SDK")
pid, seed = fl.submit("edit", {"prompt": "<image1> at a kitchen table", "steps": 8,
                               "seed": 5, "prefix": "t"}, [ref])
check(pid == "job-123", "submit() returns the SDK job id as the take id")
check(not POSTS, "no raw /prompt POST on the SDK route")
check(not DIRECT_UPLOADS, "no direct /upload/image on the SDK route")
check(REC["assets"] == [ref], "the still became an SDK asset (uploaded by the SDK)")
g = REC["graphs"][-1]
li = [k for k, v in g.items() if v["class_type"] == "LoadImage"]
check(li and any(s[0] == li[0] and s[1] == "image" for s in REC["set"]),
      "the asset is set on the LoadImage node's image input")
check(REC["clients"][-1]["base"] == "http://%s:%d" % (fl.F_HOST, fl.F_PORT + 100)
      and REC["clients"][-1]["key"] == "tok-abc" and REC["clients"][-1]["info"] == "forge-lab",
      "the client points at card 1's proxy with the token")
check(os.environ.get("COMFY_BASE_URL") is None, "COMFY_BASE_URL is put back after the client")
meta = next(i for i in fl.read_index() if i.get("id") == "job-123")
check(meta.get("route") == "comfy-sdk", "the take records route=comfy-sdk")
check(not getattr(fl._SUBMIT_TL, "sdk", False), "the staging flag is off again after submit")

print("\nPROXY DOWN -> DIRECT, NOTHING SENT TWICE")
REC["fail"] = RuntimeError("wrap")
REC["fail"].__cause__ = ConnectError("[Errno 61] Connection refused")
pid2, _ = fl.submit("edit", {"prompt": "<image1>", "steps": 8, "seed": 6, "prefix": "t"}, [ref])
check(pid2 == "direct-1" and len(POSTS) == 1 and POSTS[0][0] == "/prompt",
      "an unreachable proxy falls back to one direct /prompt")
check(DIRECT_UPLOADS == [ref], "...after uploading the staged still directly")
meta2 = next(i for i in fl.read_index() if i.get("id") == "direct-1")
check(meta2.get("route") == "direct", "...and the take says route=direct")

REC["fail"] = ValueError("node 7: bad input")
try:
    fl.submit("edit", {"prompt": "<image1>", "steps": 8, "seed": 7, "prefix": "t"}, [ref])
    check(False, "a workflow error from the SDK is raised, not retried directly")
except ValueError:
    check(len(POSTS) == 1, "a workflow error from the SDK is raised, not retried directly")
REC["fail"] = None

print("\nTEXT-ONLY RENDER")
pid3, _ = fl.submit("krea2", {"prompt": "a barn", "steps": 8, "seed": 1, "width": 512,
                              "height": 512, "prefix": "t"}, [])
check(pid3 == "job-123" and len(POSTS) == 1, "a text-to-image render goes through the SDK too")

with open(os.path.join(DATA, "settings.json"), "w") as fh:
    json.dump({"comfy_route": "direct"}, fh)
pid4, _ = fl.submit("krea2", {"prompt": "a barn", "steps": 8, "seed": 1, "width": 512,
                              "height": 512, "prefix": "t"}, [])
check(pid4 == "direct-1" and len(POSTS) == 2, "comfy_route=direct -> the old raw /prompt")

print("\n%s - %d checks failed" % ("ALL GREEN" if not FAILS else "FAIL", len(FAILS)))
sys.exit(1 if FAILS else 0)
