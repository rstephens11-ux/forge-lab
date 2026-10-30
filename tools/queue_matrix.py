#!/usr/bin/env python3
"""GPU-free assertions for Forge Lab's rig QUEUE (jobqueue.py).

    /opt/homebrew/bin/python3 tools/queue_matrix.py        # exits 1 on any failure

No Tk, no forge, no GPU, no network. The queue is deliberately dumb about dispatch: it decides
what MAY run and in what order, and a scheduler elsewhere does the posting. That is what makes
this suite possible — and it is the reason the ordering/pause rules can be trusted before any
window exists.
"""
import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import jobqueue as jq  # noqa: E402

FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def mk(title, cards=0, est=10.0, kind="comfy"):
    return jq.new_entry(title, kind, cards, est_s=est)


def fresh():
    d = tempfile.mkdtemp(prefix="qmatrix-")
    return jq.Queue(path=os.path.join(d, "queue.json")), d


print("\nENTRIES + PERSISTENCE")
q, tmp = fresh()
a, b, c = mk("first"), mk("second"), mk("turbo", cards=[0, 1], est=99.0)
for e in (a, b, c):
    q.add(e)
check(len(q.entries()) == 3, "three entries held")
check(os.path.isfile(q._path), "autosave wrote the file")
q2 = jq.Queue(path=q._path).load()
check([e["title"] for e in q2.entries()] == ["first", "second", "turbo"],
      "order survives a reload")
check(q2.get(a["id"])["cards"] == [0], "a single-card job records the card it wants")
check(q.get(c["id"])["cards"] == [0, 1], "2-card job keeps both indices")

print("\nIDS AND SHAPE")
check(len({a["id"], b["id"], c["id"]}) == 3, "ids are unique")
check(jq.new_entry("x", "comfy", 1, 5)["cards"] == [1], "an int card becomes a list")
check(jq.new_entry("x", "comfy", 1, 5)["state"] == jq.STATE_QUEUED, "new entries start queued")

print("\nORDER — manual beats submission order")
q = jq.Queue(path=os.path.join(tmp, "o.json"))
x, y, z = mk("x"), mk("y"), mk("z")
for e in (x, y, z):
    q.add(e)
check([e["title"] for e in q.entries()] == ["x", "y", "z"], "natural order is submission order")
check(q.move(z["id"], -1) and [e["title"] for e in q.entries()] == ["x", "z", "y"],
      "move up swaps with the previous entry")
check(q.move(x["id"], 1) and [e["title"] for e in q.entries()] == ["z", "x", "y"],
      "move down swaps with the next entry")
check(not q.move(z["id"], -1), "moving the first entry up is refused, not wrapped")
check(q.bump(y["id"]) and [e["title"] for e in q.entries()] == ["y", "z", "x"],
      "bump puts it at the very front")

print("\nORDER — a RUNNING entry is pinned")
q.mark_running(z["id"], lane="comfy_a")
check(not q.move(z["id"], -1), "a running entry cannot be moved")
check(not q.bump(z["id"]), "a running entry cannot be bumped to the front")
check(not q.pause(z["id"]), "a running entry cannot be paused")
check(not q.remove(z["id"]), "a running entry cannot be deleted")
check(jq.label(q.get(z["id"])).startswith("running"), "label shows it running")
check(q.move(x["id"], -1) and [e["title"] for e in q.entries()][-1] == "y",
      "movable entries step over a running one instead of swapping with it")

print("\nPAUSE / RESUME")
q = jq.Queue(path=os.path.join(tmp, "p.json"))
p1, p2 = mk("p1"), mk("p2")
for e in (p1, p2):
    q.add(e)
check(q.pause(p1["id"]) and q.get(p1["id"])["state"] == jq.STATE_PAUSED, "pause holds it")
check(q.next_job({0})["id"] == p2["id"], "a paused entry is never dispatched")
check(q.resume(p1["id"]) and q.next_job({0})["id"] == p1["id"],
      "resuming restores its position in line")

print("\nCARD FITTING — Turbo is exclusive by construction")
q = jq.Queue(path=os.path.join(tmp, "c.json"))
one_a, one_b, turbo = mk("a", cards=0), mk("b", cards=1), mk("turbo", cards=[0, 1])
for e in (one_a, one_b, turbo):
    q.add(e)
check(q.next_job({0})["id"] == one_a["id"], "with card 1 free the first card-1 job wins")
check(q.next_job({1})["id"] == one_b["id"], "with only card 2 free the card-2 job is picked")
check(q.next_job({0, 1})["id"] == one_a["id"], "order beats capacity when everything is free")
q.mark_running(one_a["id"], lane="comfy_a")
check(q.cards_busy() == {0}, "a running job claims its card")
check(q.next_job({1})["id"] == one_b["id"], "the other card stays usable")
q.mark_running(one_b["id"], lane="comfy_b")
check(q.next_job({0, 1}) is None, "both cards busy -> nothing dispatches")
check(q.next_job({0}) is None, "a 2-card job needs BOTH cards, not one")
q.finish(one_a["id"]); q.finish(one_b["id"])
check(q.next_job({0}) is None, "Turbo still waits with only one card free")
check(q.next_job({0, 1})["id"] == turbo["id"], "Turbo dispatches once both cards are free")

print("\nFINISH + WAIT ESTIMATE")
q = jq.Queue(path=os.path.join(tmp, "f.json"))
f1, f2 = mk("f1", est=30.0), mk("f2", est=45.0)
for e in (f1, f2):
    q.add(e)
check(jq.wait_seconds(q, f2["id"]) == 30.0, "wait is the estimate of everything ahead")
q.mark_running(f1["id"], lane="comfy_a")
check(jq.wait_seconds(q, f1["id"]) == 0.0, "a running entry has no wait left")
q.finish(f1["id"], ok=False, error="CUDA OOM")
check(q.get(f1["id"])["state"] == jq.STATE_FAILED, "a failed job is marked failed, not done")
check("OOM" in (q.get(f1["id"])["error"] or ""), "the error is kept for the panel")
check(q.requeue(f1["id"]) and q.get(f1["id"])["state"] == jq.STATE_QUEUED,
      "a failed job can be requeued")
check(q.get(f1["id"])["error"] is None, "requeue clears the old error")
check(not q.finish(f1["id"]), "finishing a non-running job is refused")

print("\nDELETE + CANCEL")
q = jq.Queue(path=os.path.join(tmp, "d.json"))
d1, d2 = mk("d1"), mk("d2")
q.add(d1); q.add(d2)
check(q.remove(d1["id"]) and len(q.entries()) == 1, "a queued entry can be deleted")
check(q.cancel(d2["id"]) and q.get(d2["id"])["state"] == jq.STATE_CANCELLED,
      "a queued entry can be cancelled")

print("\nCORRUPT FILE IS SURVIVABLE")
bad = os.path.join(tmp, "bad.json")
with open(bad, "w") as f:
    f.write("{not json")
qc = jq.Queue(path=bad).load()
check(qc.entries() == [], "a corrupt queue loads as empty instead of raising")
check(os.path.isfile(bad + ".bak"), "the corrupt file is set aside as .bak")

print("\nPRUNING TERMINAL ENTRIES")
q = jq.Queue(path=os.path.join(tmp, "pr.json"), autosave=False)
for i in range(jq.KEEP_TERMINAL + 12):
    e = mk("t%d" % i)
    q.add(e)
    q.mark_running(e["id"], lane="comfy_a")
    q.finish(e["id"])
q.save()
kept = [e for e in q.entries() if e["state"] in jq.TERMINAL]
check(len(kept) == jq.KEEP_TERMINAL, "terminal history is capped at KEEP_TERMINAL")
check(kept[-1]["title"] == "t%d" % (jq.KEEP_TERMINAL + 11), "the NEWEST terminal entries are kept")

print("\nTHREAD SAFETY (smoke)")
q = jq.Queue(path=os.path.join(tmp, "t.json"), autosave=False)
errors = []


def adder(n):
    try:
        for i in range(20):
            q.add(mk("th%d-%d" % (n, i)))
    except Exception as exc:                       # noqa: BLE001
        errors.append(exc)


def reader():
    try:
        for _ in range(200):
            q.entries(); q.stats(); q.cards_busy()
    except Exception as exc:                       # noqa: BLE001
        errors.append(exc)


threads = [threading.Thread(target=adder, args=(n,)) for n in range(6)]
threads += [threading.Thread(target=reader) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check(not errors, "no exception from concurrent add/read (%s)" % (errors[:1] or "none"))
check(len(q.entries()) == 120, "every concurrent add landed (got %d)" % len(q.entries()))

print("\nONE OF — a one-card job that takes EITHER card (2026-10-22)")
q = jq.Queue(path=os.path.join(tmp, "oneof.json"))
o1 = jq.new_entry("either", "comfy", [], one_of=[0, 1])
o2 = jq.new_entry("either", "comfy", [], one_of=[0, 1])
turbo2 = mk("turbo", cards=[0, 1])
for e in (o1, o2, turbo2):
    q.add(e)
check(o1["cards"] == [] and o1["one_of"] == [0, 1],
      "a one_of job needs no specific card while it waits")
check("any card" in jq.label(o1), "and its row says so")
check(q.next_job({0})["id"] == o1["id"], "it dispatches with only card 1 free")
check(q.next_job({1})["id"] == o1["id"], "and with only card 2 free")
check(q.next_job(set()) is None, "with no card free it waits (unlike a no-card job)")
check(q.claim_card(o1["id"], 1)["cards"] == [1], "claim_card pins it to the card it took")
check(q.get(o1["id"])["one_of"] is None, "...and it is no longer 'any card'")
q.mark_running(o1["id"], lane="comfy_b")
check(q.cards_busy() == {1}, "a claimed job holds ONLY the card it took")
check(q.next_job({0})["id"] == o2["id"], "so the other card is still available to the next job")
q.mark_running(o2["id"], lane="comfy_a")
check(q.next_job({0, 1}) is None, "with both claimed, nothing else fits")
check(q.next_job({0, 1}, skip={o2["id"], turbo2["id"]}) is None,
      "skip steps over entries a scheduler has already refused")
check(not q.claim_card("nope", 0), "claim_card refuses an id it does not have")
check(jq.new_entry("no card", "comfy", [], one_of=[]).get("one_of") is None,
      "an entry with no one_of keeps None (a job that needs no card at all)")

print("\nLABEL FORMAT")
e = mk("Make it move — 6 s", cards=1)
check("card 2" in jq.label(e), "a card-2 job says 'card 2'")
e2 = mk("Turbo", cards=[0, 1])
check("both cards" in jq.label(e2), "a 2-card job says 'both cards'")

print("\nPY COMPILE")
import py_compile  # noqa: E402
try:
    py_compile.compile(os.path.join(HERE, "jobqueue.py"), doraise=True)
    check(True, "jobqueue.py compiles")
except py_compile.PyCompileError as exc:
    check(False, "jobqueue.py compiles: %s" % exc)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%s — %d checks failed" % ("FAIL" if FAILS else "ALL GREEN", len(FAILS)))
sys.exit(1 if FAILS else 0)
