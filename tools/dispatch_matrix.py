#!/usr/bin/env python3
"""GPU-free assertions for the DISPATCHER (dispatch.py) — the queue's other half.

    /opt/homebrew/bin/python3 tools/dispatch_matrix.py       # exits 1 on any failure

No Tk, no rig, no GPU, no network. The rig is faked at the three seams the dispatcher has:
`probe(port)` (what /prompt would say), the room check (cards.room_now) and the RUNNERS.
Timing is driven by the test: a fake runner blocks on an Event so a job can be held "running"
while the next tick happens.

What this proves, in the order a person would ask it:
  * two one-card jobs run at the SAME TIME, one per card;
  * a third job WAITS (queued, not refused) and starts by itself when a card frees;
  * a job needing BOTH cards (Turbo) only starts when both are free, on the main lane;
  * a job that needs no card (it runs on the Mac) is never held up by the rig;
  * a card with no ROOM is not used — and the other card is tried instead;
  * a job whose card went busy under it is PUT BACK IN THE QUEUE, not marked failed;
  * a job that really raises fails with its sentence kept for the panel.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
# ⚠️ BEFORE jobqueue is imported: this suite must never touch the app's own DATA/queue.json
# (its first sections read the process-wide queue, and a stray entry would show up in
# Ryan's Home tab). template: setdefault, so an explicit FORGE_LAB_DATA still wins.
os.environ.setdefault("FORGE_LAB_DATA", tempfile.mkdtemp(prefix="dispatchmatrix-data-"))
import cards          # noqa: E402
import dispatch as dsp  # noqa: E402
import jobqueue as jq   # noqa: E402

FAILS = []
TMP = tempfile.mkdtemp(prefix="dispatchmatrix-")


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def fresh_queue(name, autosave=True):
    """A queue of its own, INSTALLED as the process-wide one.

    The window calls dispatch.enqueue(), which uses jobqueue.app_queue() — so the suite points
    that singleton at its own file and keeps exercising the real call path. (Nothing else in
    this process has loaded it yet.)
    """
    q = jq.Queue(path=os.path.join(TMP, "%s.json" % name), autosave=autosave)
    jq._APP_QUEUE = q
    return q


def wait_until(cond, secs=6.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        if cond():
            return True
        time.sleep(0.02)
    return False


def rig(route="both", probe_value=0):
    """Configure cards for the suite: a settings file of our own (so route() is ours) and a
    rig that is never really touched."""
    sdir = os.path.join(TMP, "cards")
    os.makedirs(sdir, exist_ok=True)
    p = os.path.join(sdir, "cards_settings.json")
    with open(p, "w") as f:
        json.dump({"route": route}, f)
    cards.CFG.update({"enabled": True, "host": "forge", "main_port": 8188,
                      "held_path": os.path.join(sdir, "held.json"), "ssh": "name@forge"})
    cards.LAST_BUSY.clear()

    def probe(port):
        return probe_value if not callable(probe_value) else probe_value(port)
    return probe


def room_ok(card, need, snap=None):
    return {"ok": True, "why": "ok", "usable": 99999.0, "need": int(need or 0), "blockers": [],
            "freed": [], "note": ""}


def room_none(card, need, snap=None):
    return {"ok": False, "why": "full", "usable": 100.0, "need": int(need or 0), "blockers": [],
            "freed": [], "note": "not enough"}


class Fake:
    """A fake rig: lanes, a set of blockers and per-job gates."""

    def __init__(self, route="both"):
        self.route = route
        self.lanes = {}                      # port -> queue_remaining (None = not answering)
        self.gates = {}                      # tag -> Event a job blocks on
        self.ran = []                        # [(tag, port)] in start order
        self.busy_cards = set()              # cards with no room (fake room check)
        self.threads = {}
        for card, port in ((0, 8188), (1, 8189)):
            self.lanes[port] = 0

    def probe(self, port):
        return self.lanes.get(port)

    def room(self, card, need, snap=None):
        if card in self.busy_cards:
            return {"ok": False, "why": "busy", "usable": 0.0, "need": int(need or 0),
                    "blockers": [], "freed": [], "note": "something is working"}
        return room_ok(card, need)

    def runner(self, payload, report, lane_port=None):
        tag = payload.get("tag") or "?"
        self.ran.append((tag, lane_port))
        report("rendering %s" % tag)
        self.gates.setdefault(tag, threading.Event()).wait(15)
        if payload.get("explode"):
            raise RuntimeError("the rig said no")
        if payload.get("busy"):
            raise cards.CardBusy("video", 0, 20000, 100, [])
        return {"file": "/tmp/%s.mp4" % tag, "kind": "video", "tag": tag}

    def dispatcher(self, q, tick_s=0.15):
        return dsp.Dispatcher(q=q, tick_s=tick_s, probe=self.probe, room=self.room)

    def release(self, tag):
        self.gates.setdefault(tag, threading.Event()).set()


print("\nONE QUEUE FOR THE PROCESS")
q0 = jq.app_queue()
check(jq.app_queue() is q0, "jobqueue.app_queue() is a singleton")
check(jq.Queue(path=os.path.join(TMP, "other.json")) is not q0,
      "a test can still build its own Queue without disturbing the app's")

print("\nTHE LANES THAT EXIST")
rig(route="both")
check([c for c, _p in dsp.lane_ports()] == [0, 1], "BOTH mode offers two lanes")
check(dsp.lane_ports()[1][1] == cards.SECOND_PORT, "...card 2's lane is the second ComfyUI")
rig(route="one")
check([c for c, _p in dsp.lane_ports()] == [0],
      "'one' mode offers card 1 only — the dispatcher never overrules the CARD MODE setting")
check(cards.lane_port(0) == 8188 and cards.lane_port(1) == 8189,
      "cards.lane_port() names the instance that lives on each card")

print("\nCAPACITY IS READ OFF THE LANE ITSELF")
rig(route="both")
f = Fake()
check(dsp.capacity(probe=f.probe) == {0: 8188, 1: 8189}, "both lanes idle -> both are capacity")
f.lanes[8189] = 1
check(dsp.capacity(probe=f.probe) == {0: 8188}, "a lane with work on it is NOT capacity")
f.lanes[8189] = None
check(dsp.capacity(probe=f.probe) == {0: 8188}, "a lane that is not answering is not capacity")
f.lanes[8189] = 0
f.lanes[8188] = 0

print("\nTWO JOBS AT ONCE, ONE PER CARD")
rig(route="both")
f = Fake()
q = fresh_queue("two")
d = f.dispatcher(q)
one = dsp.enqueue("one", {"tag": "one"}, need="video", one_of=(0, 1), est_s=30, kind="job")
two = dsp.enqueue("two", {"tag": "two"}, need="video", one_of=(0, 1), est_s=30, kind="job")
dsp.register("job", f.runner)
started = d.tick()
check(len(started) == 2, "ONE tick starts two jobs (got %d)" % len(started))
check(f.ran == [("one", 8188), ("two", 8189)],
      "one per card, card 1 first %r" % (f.ran,))
check(q.get(one["id"])["cards"] == [0] and q.get(two["id"])["cards"] == [1],
      "each 'any card' entry was CLAIMED to the card it took")
check(q.cards_busy() == {0, 1}, "both cards read as busy, from the queue's own claims")
check(q.get(one["id"])["lane"] == "comfy_a" and q.get(two["id"])["lane"] == "comfy_b",
      "each row says which lane it is on")
check(q.get(one["id"])["one_of"] is None, "after the claim it is no longer 'any card'")

print("\nA THIRD JOB WAITS, THEN STARTS BY ITSELF")
three = dsp.enqueue("three", {"tag": "three"}, need="video", one_of=(0, 1), est_s=30,
                    kind="job")
d.start()                                  # from here it is the LOOP that does the work
check(wait_until(lambda: q.get(three["id"])["state"] == jq.STATE_QUEUED, 1.5)
      and f.ran == [("one", 8188), ("two", 8189)],
      "with both cards busy the third job is left QUEUED (never refused)")
check("any card" in jq.label(q.get(three["id"])), "its row says it will take any card")
check(q.queued_ahead(three["id"]) == 0, "and it is next up")
f.release("one")
check(wait_until(lambda: q.get(one["id"])["state"] == jq.STATE_DONE), "the first job finishes")
check(wait_until(lambda: ("three" in [t for t, _p in f.ran])), "the waiting job starts by itself")
check(f.ran[-1] == ("three", 8188), "it took the card that freed")
check(dsp.result(one["id"])["file"].endswith("one.mp4"),
      "a finished job keeps its RESULT for the window that asked for it")
check(q.get(three["id"])["lane"] == "comfy_a", "its lane is reported on the row")
f.release("two")
f.release("three")
wait_until(lambda: not q.running())
d.stop()

print("\nTURBO: BOTH CARDS OR NOTHING, AND ONLY ON THE MAIN LANE")
tq = fresh_queue("turbo")
f2 = Fake()
d2 = f2.dispatcher(tq)
dsp.register("job", f2.runner)
a = dsp.enqueue("a", {"tag": "a"}, need="video", one_of=(0, 1), est_s=10, kind="job")
b = dsp.enqueue("b", {"tag": "b"}, need="video", one_of=(0, 1), est_s=10, kind="job")
turbo = dsp.enqueue("turbo", {"tag": "turbo"}, need="turbo", one_of=None, cards=[0, 1],
                    est_s=900, kind="job")
d2.start()
check(wait_until(lambda: len(f2.ran) == 2 and f2.ran[0][1] == 8188 and f2.ran[1][1] == 8189),
      "the two one-card jobs still take a card each %r" % (f2.ran,))
check([e["title"] for e in tq.running()] == ["a", "b"], "and Turbo is NOT among the runners")
f2.release("a")
wait_until(lambda: tq.get(turbo["id"])["state"] in (jq.STATE_QUEUED, jq.STATE_RUNNING), 1.5)
check(tq.get(turbo["id"])["state"] == jq.STATE_QUEUED,
      "a job needing BOTH cards waits while only one is free")
f2.release("b")
check(wait_until(lambda: tq.get(turbo["id"])["state"] == jq.STATE_RUNNING),
      "...and starts once both are free")
check(tq.get(turbo["id"])["lane"] == "comfy_a",
      "...on the MAIN lane, the only one that sees both cards")
check(tq.get(turbo["id"])["cards"] == [0, 1], "Turbo keeps both cards in its footprint")
check(tq.cards_busy() == {0, 1}, "so the queue reports both cards busy")

print("\nA JOB THAT NEEDS NO CARD IS NEVER HELD UP")
lq = fresh_queue("local")
f3 = Fake()
d3 = f3.dispatcher(lq)
dsp.register("job", f3.runner)
h1 = dsp.enqueue("h1", {"tag": "h1"}, need="video", one_of=(0, 1), kind="job")
h2 = dsp.enqueue("h2", {"tag": "h2"}, need="video", one_of=(0, 1), kind="job")
loc = dsp.enqueue("vertical", {"tag": "loc", "video": True}, need="local", one_of=None,
                  kind="job")
d3.tick()
check(("loc", None) in f3.ran, "a Mac-only job runs while BOTH cards are busy %r" % (f3.ran,))
check(lq.get(loc["id"])["cards"] == [] and lq.get(loc["id"])["one_of"] is None,
      "it claims no card at all")
check(lq.get(loc["id"])["lane"] == "this Mac", "and its row says where it ran")

print("\nNO ROOM -> THE OTHER CARD IS TRIED, AND NOTHING SPINS")
rq = fresh_queue("room")
f4 = Fake()
d4 = f4.dispatcher(rq)
dsp.register("job", f4.runner)
f4.busy_cards = {0}                       # card 1 has something working on it
j = dsp.enqueue("j", {"tag": "j"}, need="video", one_of=(0, 1), kind="job")
d4.tick()
check(f4.ran == [("j", 8189)], "a job refused card 1 starts on card 2 instead %r" % (f4.ran,))
f4.busy_cards = {0, 1}
f4.release("j")
wait_until(lambda: rq.get(j["id"])["state"] == jq.STATE_DONE)
j2 = dsp.enqueue("j2", {"tag": "j2"}, need="video", one_of=(0, 1), kind="job")
j3 = dsp.enqueue("j3", {"tag": "j3"}, need="video", one_of=(0, 1), kind="job")
check(d4.tick() == [] and rq.get(j2["id"])["state"] == jq.STATE_QUEUED,
      "with no room anywhere it waits")
check(d4.tick() == [] and rq.get(j3["id"])["state"] == jq.STATE_QUEUED,
      "a second blocked job does not hide the first (skip, not spin)")
f4.busy_cards = set()
d4.tick()
check(rq.get(j2["id"])["state"] == jq.STATE_RUNNING and rq.get(j3["id"])["state"] == jq.STATE_RUNNING,
      "when the room comes back BOTH waiting jobs start")
for tag in ("j2", "j3"):
    f4.release(tag)

print("\nPAUSE IS HONOURED BY THE SCHEDULER")
pq = fresh_queue("pause")
f5 = Fake()
d5 = f5.dispatcher(pq)
dsp.register("job", f5.runner)
p1 = dsp.enqueue("p1", {"tag": "p1"}, need="video", one_of=(0, 1), kind="job")
pq.pause(p1["id"])
d5.tick()
check(f5.ran == [] and pq.get(p1["id"])["state"] == jq.STATE_PAUSED,
      "a paused job is never started")
pq.resume(p1["id"])
d5.tick()
check([t for t, _p in f5.ran] == ["p1"], "resuming puts it back in line and it starts")
f5.release("p1")

print("\nA CARD THAT GOES BUSY UNDER US -> BACK IN THE QUEUE, NOT A FAILURE")
cq = fresh_queue("cardbusy")
f6 = Fake()
d6 = f6.dispatcher(cq)
dsp.register("job", f6.runner)
cb = dsp.enqueue("cb", {"tag": "cb", "busy": True}, need="video", one_of=(0, 1), kind="job")
f6.gates.setdefault("cb", threading.Event()).set()     # let the runner get to its raise
d6.tick()
check(wait_until(lambda: cq.get(cb["id"])["state"] == jq.STATE_QUEUED),
      "a CardBusy raises the job back to QUEUED instead of failing it (%r)"
      % (cq.get(cb["id"]) or {},))
check("busy" in (cq.get(cb["id"])["note"] or ""), "the row says why it went back %r"
      % (cq.get(cb["id"])["note"],))
check(cq.get(cb["id"])["error"] is None, "and no error is recorded against it")
check(d6.tick() == [] , "the loop does NOT hot-spin on it")
check(cb["id"] in d6._cooldown, "it is cooled down before the next try")

print("\nA JOB THAT REALLY FAILS KEEPS ITS SENTENCE")
xq = fresh_queue("boom")
f7 = Fake()
d7 = f7.dispatcher(xq)
dsp.register("job", f7.runner)
xb = dsp.enqueue("xb", {"tag": "xb", "explode": True}, need="video", one_of=(0, 1),
                 kind="job")
f7.gates.setdefault("xb", threading.Event()).set()
d7.tick()
check(wait_until(lambda: xq.get(xb["id"])["state"] == jq.STATE_FAILED), "it is marked failed")
check("the rig said no" in (xq.get(xb["id"])["error"] or ""), "...with the reason kept")
check(dsp.error(xb["id"]) == "the rig said no", "and dispatch.error() has it for the window")
check(dsp.result(xb["id"]) is None, "a failed job has no result")

print("\nAN UNKNOWN KIND IS REFUSED, NOT RUN")
uq = fresh_queue("unknown")
f8 = Fake()
d8 = f8.dispatcher(uq)
uk = dsp.enqueue("uk", {"tag": "uk"}, need="video", one_of=(0, 1), kind="nosuchkind")
d8.tick()
check(wait_until(lambda: uq.get(uk["id"])["state"] == jq.STATE_FAILED)
      and "registered" in (dsp.error(uk["id"]) or ""), "nothing registered -> it fails loudly")
print("\nTHE LOOP ITSELF")
f9 = Fake()
lq2 = fresh_queue("loop")
d9 = f9.dispatcher(lq2, tick_s=0.1)
dsp.register("job", f9.runner)
d9.start()
check(d9.running(), "start() brings the thread up")
check(d9.start() is False, "starting it twice is refused, not a second thread")
z = dsp.enqueue("z", {"tag": "z"}, need="video", one_of=(0, 1), kind="job")
check(wait_until(lambda: [t for t, _p in f9.ran] == ["z"]), "a queued job is picked up by the loop")
f9.release("z")
check(wait_until(lambda: lq2.get(z["id"])["state"] == jq.STATE_DONE), "and finishes")
d9.kick()
d9.stop()
check(not d9.running(), "stop() ends it")
check(dsp.summary() in ("nothing waiting", "1 running", "1 waiting") or "nothing" in dsp.summary(),
      "summary() reads the real queue (%r)" % dsp.summary())

print("\nTHE WEIRDNESS THIS FEATURE EXISTS TO PREVENT")
wq = fresh_queue("weird")
f10 = Fake()
d10 = f10.dispatcher(wq)
dsp.register("job", f10.runner)
w1 = dsp.enqueue("w1", {"tag": "w1"}, need="video", one_of=(0, 1), kind="job")
w2 = dsp.enqueue("w2", {"tag": "w2"}, need="video", one_of=(0, 1), kind="job")
d10.tick()
f10.release("w1")
f10.release("w2")
wait_until(lambda: len(wq.running()) == 0)
f10.ran[:] = []
w3 = dsp.enqueue("w3", {"tag": "w3"}, need="video", one_of=(0, 1), kind="job")
d10.tick()
check(f10.ran == [("w3", 8188)],
      "after both jobs finish, the next job goes back to card 1 (the warm one) %r" % (f10.ran,))
check(wq.get(w3["id"])["cards"] == [0], "and claims card 1 again")

print("\nA STALE 'RUNNING' ENTRY FROM THE LAST RUN CANNOT HOLD A CARD FOR EVER")
sq = fresh_queue("stale", autosave=True)
st1 = jq.new_entry("left running", "gate", [0], payload={"need": "video", "gate": True})
sq.add(st1)
sq.mark_running(st1["id"], lane="comfy_a")
sq.save()
sq2 = jq.Queue(path=sq._path).load()
check(sq2.get(st1["id"])["state"] == jq.STATE_FAILED,
      "loading a queue marks a RUNNING entry failed (its worker died with the process)")
check("closed while this was running" in (sq2.get(st1["id"])["error"] or ""),
      "...and says why: %r" % (sq2.get(st1["id"])["error"],))
check(sq2.cards_busy() == set(), "so it does NOT keep claiming its card after a restart")

print("\nRENDER — the shape every engine module uses (quick, studio_pipeline, places, charpack)")
# ⚠️ render() checks the PROCESS-WIDE scheduler (`running()`), so the suite starts THAT one
# with fake lanes — not a private instance. Getting this wrong sends a real prompt to the rig
# (seen: a 64px "x" render landed on forge while writing this test).
rq = fresh_queue("render")
f12 = Fake()
calls = []


def fake_render(payload, report, lane_port=None):
    calls.append((payload.get("mode"), dict(payload.get("params") or {}),
                  payload.get("refs"), lane_port))
    report("rendering on forge")
    return {"pid": "p1", "seed": 3,
            "meta": {"id": "p1", "seed": 3, "images": [{"file": "o.png"}]}}


dsp.RUNNERS["render"] = fake_render
dsp.PROBE["fn"] = f12.probe
dsp.ROOM["fn"] = f12.room
dsp.start()
box = {}


def _call_render():
    box["meta"] = dsp.render("Quick — the loft", "t2i", {"prompt": "x", "width": 64}, None,
                             need="image", est_s=45)


threading.Thread(target=_call_render, daemon=True).start()
time.sleep(0.3)
check("meta" not in box, "render() BLOCKS while its job waits for a card")
check(wait_until(lambda: box.get("meta", {}).get("id") == "p1", 8),
      "a card frees -> the runner submits -> render() returns the collected take")
check(calls and calls[0][0] == "t2i" and not calls[0][2],
      "...the runner got the mode and the refs (%r)" % (calls[:1],))
check(calls and calls[0][3] == 8188, "...and the pinned lane (%r)" % (calls[0][3],))
check(len(rq.entries()) == 1 and rq.entries()[0]["state"] == jq.STATE_DONE,
      "...with its queue entry DONE")
calls[:] = []
_rq2 = fresh_queue("render2")


def _call_again():
    try:
        dsp.render("Quick — the shed", "t2i", {"prompt": "y"}, None, need="image")
        box["err"] = None
    except Exception as exc:                   # noqa: BLE001
        box["err"] = "%s" % exc


threading.Thread(target=_call_again, daemon=True).start()
check(wait_until(lambda: len(_rq2.entries()) == 1, 5), "a second render queues")
for e in _rq2.entries():
    _rq2.remove(e["id"])
check(wait_until(lambda: "err" in box, 8) and "queue" in (box.get("err") or ""),
      "deleting the row makes render() STOP with a sentence (%r)" % (box.get("err"),))
dsp.stop()
dsp.PROBE["fn"] = None
dsp.ROOM["fn"] = None

print("\nRENDER WITH NO SCHEDULER RUNNING submits straight through (tests, headless scripts)")
import forge_core as _fc                                                    # noqa: E402
_real_submit, _real_wait = _fc.submit, _fc.wait_and_collect
_direct = []
_fc.submit = lambda mode, p, refs=None, _d=_direct: (_d.append((mode, dict(p))), ("zz1", 5))[1]
_fc.wait_and_collect = lambda pid, timeout=1800, **k: {"id": pid, "images": [{"file": "d.png"}]}
try:
    _m = dsp.render("no scheduler", "t2i", {"prompt": "z"}, None, need="image")
    check(_m.get("id") == "zz1" and _direct and _direct[0][0] == "t2i",
          "no scheduler -> submit + collect exactly as every engine module did before")
    check(not dsp.queue().live(), "...and nothing was put in the queue")
finally:
    _fc.submit, _fc.wait_and_collect = _real_submit, _real_wait

print("\nGATES — a window's own thread borrowing a card (2026-10-22)")
rig(route="both")
gq = fresh_queue("gate")
f11 = Fake()
d11 = f11.dispatcher(gq, tick_s=0.15)
dsp.register("job", f11.runner)
g1 = dsp.admit("Make it move — a barn", need="video", est_s=60)
check(gq.get(g1.id)["kind"] == "gate" and (gq.get(g1.id)["payload"] or {}).get("gate") is True,
      "admit() queues a gate entry")
check(gq.get(g1.id)["state"] == jq.STATE_QUEUED, "...waiting, like any other job")
d11.tick()
check(gq.get(g1.id)["state"] == jq.STATE_RUNNING and g1.port == 8188,
      "one tick gives it card 1 and releases its gate (%r)" % (g1.port,))
check(gq.cards_busy() == {0}, "its card is held while the window renders")
check(gq.get(g1.id)["lane"] == "comfy_a", "the row says which lane it is on")
check(f11.ran == [], "no runner was called: the WINDOW does the work")
g1b = dsp.admit("outfit — the loft", need="image", est_s=20)
d11.tick()
check(gq.get(g1b.id)["state"] == jq.STATE_RUNNING and g1b.port == 8189,
      "a second gate takes the OTHER card")
g2 = dsp.admit("Make it move — the shed", need="video", est_s=60)
d11.tick()
check(gq.get(g2.id)["state"] == jq.STATE_QUEUED, "a third gate waits: both cards are held")
out = {}


def _waiter(gate, box):
    box["ok"] = gate.wait(poll=0.05)


threading.Thread(target=_waiter, args=(g2, out), daemon=True).start()
time.sleep(0.3)
check("ok" not in out, "wait() BLOCKS while the job has no card")
g1.done()
check(gq.get(g1.id)["state"] == jq.STATE_DONE, "done() finishes the entry")
d11.tick()
check(wait_until(lambda: out.get("ok") is True, 3),
      "a card is free -> the dispatcher releases the gate and wait() returns")
check(g2.port == 8188, "...and the worker knows which lane it got (%r)" % (g2.port,))
g3 = dsp.admit("Make it move — nowhere", need="video")
res = {}
threading.Thread(target=_waiter, args=(g3, res), daemon=True).start()
time.sleep(0.3)
gq.remove(g3.id)
check(wait_until(lambda: res.get("ok") is False, 3),
      "deleting the row stops the waiting worker (False, not a crash)")
check(g3.cancelled, "...and it says it was cancelled")
orphan = jq.new_entry("orphan", "gate", [], payload={"need": "video", "gate": True},
                      one_of=[0, 1])
gq.add(orphan)
d11.tick()
check(gq.get(orphan["id"])["state"] == jq.STATE_CANCELLED,
      "a gate entry NOTHING is waiting on (the app restarted) is cancelled")
check("gone" in (gq.get(orphan["id"])["note"] or ""), "...and says why: %r"
      % (gq.get(orphan["id"])["note"],))
check(gq.get(orphan["id"])["cards"] == [], "it never claimed a card")
g4 = dsp.admit("Make it move — paused", need="video")
gq.pause(g4.id)
d11.tick()
check(gq.get(g4.id)["state"] == jq.STATE_PAUSED and g4.port is None,
      "a PAUSED gate is never released (it keeps its place in line)")
gq.resume(g4.id)
g1b.done()
g2.done()
d11.tick()
check(g4.port in (8188, 8189), "resuming it lets it start when a card frees")
g4.done()
check(gq.get(g4.id)["state"] == jq.STATE_DONE, "and it finishes normally")

print("\nWHAT A MODE NEEDS (one question, asked the same way by every window)")
check(dsp.need_for("t2i")["need"] == "image", "a still is an image job")
check(dsp.need_for("video")["need"] == "video", "a video is a video job")
check(dsp.need_for("song")["need"] == "song", "a song is a song job")
_n2 = dsp.need_for("video", {"gpus": 2})
check(_n2["need"] == "turbo" and _n2["cards"] == [0, 1] and _n2["one_of"] is None,
      "a 2-card Turbo needs BOTH cards and is not 'any card'")
check(dsp.need_for("t2i")["one_of"] == [0, 1], "a one-card job will take either card")

print("\nPY COMPILE")
import py_compile  # noqa: E402
for name in ("dispatch.py", "jobqueue.py", "cards.py"):
    try:
        py_compile.compile(os.path.join(HERE, name), doraise=True)
        check(True, "%s compiles" % name)
    except py_compile.PyCompileError as exc:
        check(False, "%s compiles: %s" % (name, exc))

shutil.rmtree(TMP, ignore_errors=True)
print("\n%s — %d checks failed" % ("FAIL" if FAILS else "ALL GREEN", len(FAILS)))
sys.exit(1 if FAILS else 0)
