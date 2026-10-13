#!/usr/bin/env python3
"""GPU-free, forge-free checks for Forge Lab's network helpers, against a LOCAL STUB forge.

    /opt/homebrew/bin/python3 tools/net_matrix.py      # exit 1 on any failure

Never touches the real rig: FORGE is pointed at a stub on 127.0.0.1 before any call, and
FORGE_LAB_DATA is a throwaway dir so Ryan's real runs/index.json is never written.
"""
import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["FORGE_LAB_DATA"] = tempfile.mkdtemp(prefix="forgenet-")   # BEFORE import

spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(ROOT, "forge_lab.py"))
fl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fl)

FAILS, N = [], [0]


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


# ---------------------------------------------------------------- the stub forge
POSTS = []
QUEUE = {"queue_running": [[0, "p_run", {}, {}, []]],
         "queue_pending": [[1, "p_mine", {}, {}, []], [2, "p_other", {}, {}, []]]}


class Stub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/queue":
            self._send(json.dumps(QUEUE).encode())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        POSTS.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
        # /json answers with a body; everything else answers like REAL ComfyUI does for
        # /interrupt and /queue: 200 with an EMPTY body (measured on forge, 2026-10-10).
        self._send(b'{"ok": 1}' if self.path == "/json" else b"")


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
fl.FORGE = "http://127.0.0.1:%d" % srv.server_address[1]
fl._ok = True                                   # skip resolve_forge(): never probe the real rig

# ---------------------------------------------------------------- §1 empty body is success
print("§1 post_json and forge's empty replies")
try:
    r = fl.post_json("/interrupt", {})
    check(r == {}, "POST /interrupt with an EMPTY 200 body returns {} (got %r)" % (r,))
except Exception as e:
    check(False, "POST /interrupt with an EMPTY 200 body raised %s: %s" % (type(e).__name__, e))

# ---------------------------------------------------------------- §2 a real body still parses
print("§2 post_json still parses a JSON body")
try:
    check(fl.post_json("/json", {}) == {"ok": 1}, "a JSON body still comes back parsed")
except Exception as e:
    check(False, "a JSON body raised %s" % e)

# ---------------------------------------------------------------- §3 cancel_queued (Task 6)
print("§3 cancel_queued only removes THIS app's WAITING runs")
if not hasattr(fl, "cancel_queued"):
    check(False, "cancel_queued() exists")
else:
    fl._index_insert({"id": "p_mine", "label": "x", "mode": "t2i", "status": "running"})
    del POSTS[:]
    got = fl.cancel_queued(["p_run", "p_mine", "p_gone"])
    check(got == ["p_mine"], "only the pending id we own is cancelled (got %r)" % (got,))
    deletes = [b.get("delete") for (path, b) in POSTS if path == "/queue"]
    check(deletes == [["p_mine"]], "exactly one POST /queue {delete:[p_mine]} (got %r)" % (deletes,))
    flat = [x for d in deletes for x in (d or [])]
    check("p_other" not in flat, "another client's queued run is NEVER deleted")
    check("p_run" not in flat, "the RUNNING prompt is never deleted (INTERRUPT is for that)")
    ent = next((i for i in fl.read_index() if i["id"] == "p_mine"), {})
    check(ent.get("status") == "cancelled", "the take is marked cancelled in index.json "
                                              "(got %r)" % ent.get("status"))
    del POSTS[:]
    check(fl.cancel_queued(["p_run"]) == [] and not POSTS,
          "nothing of ours waiting -> no POST at all")

# ---------------------------------------------------------------- §4 queue_note (Task 7)
print("§4 queue_note — the estimate's 'forge is busy' line")
if not hasattr(fl, "queue_note"):
    check(False, "queue_note() exists")
else:
    check(fl.queue_note(0) == "", "an idle forge adds nothing")
    check(fl.queue_note(None) == "", "an unknown queue adds nothing")
    s = fl.queue_note(2)
    check("2 job(s)" in s, "the count is in the line (%r)" % s)
    s = fl.queue_note(3, 125)
    check("2m 05s" in s, "your own in-flight estimate is shown as m/s (%r)" % s)

srv.shutdown()
print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f in FAILS:
    print("  FAILED: %s" % f)
sys.exit(1 if FAILS else 0)