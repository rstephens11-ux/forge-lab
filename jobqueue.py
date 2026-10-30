#!/usr/bin/env python3
"""jobqueue — the rig's ONE queue for everything Forge Lab submits.

WHY THIS EXISTS
    The rig has two cards and many things that want them: an image, a video, a Turbo video
    (which needs BOTH cards), a song, an app job, a voice batch, the writer. Until now a busy
    card meant a REFUSAL ("card 2 is busy") — which throws away the idea you had when you
    clicked. Now a submission always succeeds: if nothing can take it, it waits here, visibly,
    in an order you can change.

WHAT THIS MODULE IS (and is not)
    It is the QUEUE: what is waiting, in what order, paused or not, done or failed, which cards
    a job needs, and which entries may run right now given which cards are free. It is NOT the
    dispatcher: it never posts to ComfyUI, never ssh'es, never loads a model. A separate
    scheduler calls next_job(), hands the payload to an adapter, and reports back with
    mark_running() / finish(). That split is what keeps this file testable with no Tk and no GPU
    (tools/queue_matrix.py).

    It is also not a MODEL queue. ComfyUI has its own FIFO internally; that queue cannot pause
    and cannot be reordered, and it cannot span two instances. So Forge Lab never hands a second
    job to a busy lane: it holds it here instead, and this queue is the authority.

STORAGE
    DATA/queue.json, written atomically (tmp + os.replace) so a crash mid-write cannot leave a
    half file. A corrupt file is moved aside to queue.json.bak and treated as empty — losing the
    queue is survivable, refusing to start the app is not.

RULES THAT MATTER
  * A RUNNING entry cannot be moved, paused or deleted. It is already on a card; the only honest
    action is cancel, and that belongs to the adapter (ComfyUI can /interrupt, an ssh rig job
    needs its remote process killed). pause() on a running entry returns False and says why.
  * Manual order beats submission order: move()/bump() reorder anything not running, and
    next_job() always takes the first eligible entry in list order.
  * A job needing BOTH cards only runs when both are free, so Turbo is exclusive by construction.
    The other shape is `one_of`: a ONE-card job that will take either card. That is what lets
    two one-card jobs run at the same time, one per card — `cards` alone cannot express it
    (cards=[0, 1] means both are needed). The scheduler calls claim_card() to pin a one_of
    entry to the card it actually took, so cards_busy() stays honest from that moment on.
  * Terminal entries (done/failed/cancelled) stay visible for reference but are pruned to the
    last KEEP_TERMINAL, newest first.
"""
import json
import os
import threading
import time
import uuid

KEEP_TERMINAL = 60          # terminal entries kept; older ones are pruned on load/save
STATE_QUEUED = "queued"
STATE_PAUSED = "paused"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
LIVE = (STATE_QUEUED, STATE_PAUSED, STATE_RUNNING)
TERMINAL = (STATE_DONE, STATE_FAILED, STATE_CANCELLED)

_LOCK = threading.RLock()


def default_path():
    """DATA/queue.json, resolved the way the app resolves DATA."""
    data = os.path.expanduser(os.environ.get("FORGE_LAB_DATA") or "~/forge-lab")
    return os.path.join(data, "queue.json")


_APP_QUEUE = None


def app_queue(path=None):
    """The ONE queue for this process: the panel, the Home tab and the dispatcher share it.

    Built here rather than in home_queue because home_queue imports tkinter and the
    dispatcher must not (dispatch.py is Tk-free and imports this module).

    ⚠️ Never build a second Queue for the app data dir: a panel reordering a file the
    dispatcher never re-reads is a queue that does not work.
    """
    global _APP_QUEUE
    if _APP_QUEUE is None:
        _APP_QUEUE = Queue(path=path).load()
    return _APP_QUEUE


def _now():
    return round(time.time(), 3)


# --------------------------------------------------------------------------- entries
def new_entry(title, kind, cards, est_s=0.0, payload=None, note="", one_of=None):
    """A queue entry. `cards` is the set of card INDICES the job needs (0-based).

    kind: "comfy" (a ComfyUI prompt), "rig" (an ssh one-shot), "llm" (the writer). The queue
    only cares that `cards` is honest — the adapter decides what `payload` means.

    `one_of`: cards that would EACH do the job on their own ("a one-card job, any card").
    Leave `cards` empty and pass one_of=[0, 1] for that. An entry with neither (cards=[])
    needs no card at all — a job that runs on this Mac.
    """
    if isinstance(cards, int):
        cards = [cards]
    if isinstance(one_of, int):
        one_of = [one_of]
    return {
        "id": uuid.uuid4().hex[:8],
        "title": str(title)[:120],
        "kind": kind,
        "cards": sorted({int(c) for c in (cards or [])}),
        "one_of": sorted({int(c) for c in (one_of or [])}) or None,
        "est_s": float(est_s or 0.0),
        "payload": payload if payload is not None else {},
        "state": STATE_QUEUED,
        "lane": None,
        "note": note,
        "error": None,
        "made": _now(),
        "started": None,
        "ended": None,
    }


def is_live(e):
    return e.get("state") in LIVE


def is_movable(e):
    return e.get("state") in (STATE_QUEUED, STATE_PAUSED)


# --------------------------------------------------------------------------- the queue
class Queue:
    """Thread-safe, persistent job queue. Rebuild it with Queue(path) in a test."""

    def __init__(self, path=None, entries=None, autosave=True):
        self._path = path or default_path()
        self._autosave = autosave
        self._entries = list(entries or [])

    # ---------------------------------------------------------------- persistence
    def load(self):
        """Read the queue file. Missing -> empty. Corrupt -> set aside, empty."""
        with _LOCK:
            self._entries = []
            if not os.path.isfile(self._path):
                return self
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, list):
                    raise ValueError("queue file is not a list")
                self._entries = [e for e in raw if isinstance(e, dict) and e.get("id")]
                # ⚠️ A RUNNING entry in a file being LOADED is stale by definition: the worker
                # running it died with the last process. Left alone it would hold its card for
                # ever (cards_busy() counts running entries) and the queue would never use both
                # cards again. Mark it failed, honestly, and let the panel show it.
                for e in self._entries:
                    if e.get("state") == STATE_RUNNING:
                        e["state"] = STATE_FAILED
                        e["error"] = "the app closed while this was running"
                        e["ended"] = _now()
                        e["lane"] = None
            except Exception:
                try:
                    os.replace(self._path, self._path + ".bak")
                except Exception:
                    pass
                self._entries = []
            self._prune()
            return self

    def save(self):
        with _LOCK:
            self._prune()
            d = os.path.dirname(self._path) or "."
            os.makedirs(d, exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._entries, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self._path)
            return self._path

    def _prune(self):
        term = [e for e in self._entries if e.get("state") in TERMINAL]
        if len(term) <= KEEP_TERMINAL:
            return
        drop = {id(e) for e in sorted(term, key=lambda e: e.get("ended") or 0)[:-KEEP_TERMINAL]}
        self._entries = [e for e in self._entries if id(e) not in drop]

    def _saved(self):
        if self._autosave:
            self.save()

    # ---------------------------------------------------------------- reading
    def entries(self):
        """A shallow copy of the list (entries themselves are shared dicts — copy before edit)."""
        with _LOCK:
            return list(self._entries)

    def get(self, jid):
        with _LOCK:
            for e in self._entries:
                if e.get("id") == jid:
                    return e
        return None

    def live(self):
        with _LOCK:
            return [e for e in self._entries if is_live(e)]

    def running(self):
        with _LOCK:
            return [e for e in self._entries if e.get("state") == STATE_RUNNING]

    def stats(self):
        with _LOCK:
            out = {}
            for e in self._entries:
                out[e.get("state")] = out.get(e.get("state"), 0) + 1
            out["total"] = len(self._entries)
            return out

    def cards_busy(self):
        """Card indices held by RUNNING entries."""
        busy = set()
        for e in self.running():
            busy.update(int(c) for c in (e.get("cards") or []))
        return busy

    def queued_ahead(self, jid):
        """How many dispatchable entries sit before `jid`. -1 when it is not dispatchable."""
        with _LOCK:
            seen = 0
            for e in self._entries:
                if e.get("id") == jid:
                    return seen if e.get("state") == STATE_QUEUED else -1
                if e.get("state") == STATE_QUEUED:
                    seen += 1
            return -1

    def next_job(self, free_cards, skip=None):
        """The first entry, in list order, that may run now. None when nothing fits.

        `free_cards` is the CAPACITY — cards with room right now. Running entries this queue
        owns are subtracted from it here, because a caller that passed raw "card 1 has room"
        would otherwise be handed a second job for a card a job is already on. That subtraction
        is the whole reason a scheduler can be simple.

        A paused or running entry never qualifies; a 2-card job needs both of its cards free;
        a `one_of` job needs any ONE of its cards free.

        `skip` is a set of entry ids THIS pass has already refused (a card that looked free but
        whose preflight said no). Skipping them is what stops one blocked job from hiding every
        job behind it.
        """
        free = {int(c) for c in (free_cards or [])} - self.cards_busy()
        skip = set(skip or ())
        with _LOCK:
            for e in self._entries:
                if e.get("state") != STATE_QUEUED or e.get("id") in skip:
                    continue
                one = [int(c) for c in (e.get("one_of") or [])]
                if one:
                    if set(one) & free:
                        return e
                    continue
                need = [int(c) for c in (e.get("cards") or [])]
                if need and not set(need) <= free:
                    continue
                return e
        return None

    # ---------------------------------------------------------------- writing
    def add(self, entry):
        with _LOCK:
            self._entries.append(entry)
            self._saved()
            return entry

    def move(self, jid, delta):
        """Nudge an entry up (-1) or down (+1) among the MOVABLE entries. True when it moved.

        Running entries are stepped over rather than swapped with, so a job that is already on a
        card never changes position in the list.
        """
        with _LOCK:
            mov = [i for i, e in enumerate(self._entries) if is_movable(e)]
            idx = next((i for i in mov if self._entries[i].get("id") == jid), None)
            if idx is None:
                return False
            pos = mov.index(idx)
            tgt_pos = pos + int(delta)
            if tgt_pos < 0 or tgt_pos >= len(mov):
                return False
            tgt = mov[tgt_pos]
            self._entries[idx], self._entries[tgt] = self._entries[tgt], self._entries[idx]
            self._saved()
            return True

    def bump(self, jid):
        """Move an entry to the front of the waiting line ("run next")."""
        with _LOCK:
            e = self.get(jid)
            if e is None or not is_movable(e):
                return False
            self._entries.remove(e)
            first_live = next((i for i, x in enumerate(self._entries) if is_live(x)), len(self._entries))
            self._entries.insert(first_live, e)
            self._saved()
            return True

    def pause(self, jid):
        """Hold a WAITING entry in place. A running entry cannot be paused (False)."""
        with _LOCK:
            e = self.get(jid)
            if e is None or e.get("state") != STATE_QUEUED:
                return False
            e["state"] = STATE_PAUSED
            self._saved()
            return True

    def resume(self, jid):
        with _LOCK:
            e = self.get(jid)
            if e is None or e.get("state") != STATE_PAUSED:
                return False
            e["state"] = STATE_QUEUED
            self._saved()
            return True

    def remove(self, jid):
        """Delete a waiting entry. A running one must be cancelled by its adapter first."""
        with _LOCK:
            e = self.get(jid)
            if e is None or e.get("state") == STATE_RUNNING:
                return False
            self._entries.remove(e)
            self._saved()
            return True

    def mark_running(self, jid, lane):
        with _LOCK:
            e = self.get(jid)
            if e is None or e.get("state") != STATE_QUEUED:
                return None
            e["state"] = STATE_RUNNING
            e["lane"] = lane
            e["started"] = _now()
            self._saved()
            return e

    def note(self, jid, text):
        """Set a waiting/running entry's one-line note (progress, "waiting for a card")."""
        with _LOCK:
            e = self.get(jid)
            if e is None:
                return None
            e["note"] = str(text)[:200]
            self._saved()
            return e

    def claim_card(self, jid, card):
        """Pin a `one_of` entry to the ONE card the scheduler is about to use.

        Call it after the card has been checked and immediately before mark_running(): from
        then on cards_busy() names that card, so the OTHER card stays available to the next
        job instead of looking taken by this one. Returns the entry, or None if it was not
        waiting any more.
        """
        with _LOCK:
            e = self.get(jid)
            if e is None or e.get("state") != STATE_QUEUED:
                return None
            e["cards"] = [int(card)]
            e["one_of"] = None
            self._saved()
            return e

    def finish(self, jid, ok=True, error=None, note=None):
        with _LOCK:
            e = self.get(jid)
            if e is None or e.get("state") != STATE_RUNNING:
                return False
            e["state"] = STATE_DONE if ok else STATE_FAILED
            e["ended"] = _now()
            e["error"] = None if ok else (str(error)[:400] if error else "failed")
            if note:
                e["note"] = str(note)[:200]
            self._saved()
            return True

    def cancel(self, jid, note=None):
        """Mark a waiting entry cancelled (the adapter owns cancelling a RUNNING one)."""
        with _LOCK:
            e = self.get(jid)
            if e is None or e.get("state") == STATE_RUNNING:
                return False
            e["state"] = STATE_CANCELLED
            e["ended"] = _now()
            if note:
                e["note"] = str(note)[:200]
            self._saved()
            return True

    def requeue(self, jid, note=None):
        """Put a failed/cancelled entry back at the end of the waiting line."""
        with _LOCK:
            e = self.get(jid)
            if e is None or e.get("state") not in (STATE_FAILED, STATE_CANCELLED):
                return False
            e.update({"state": STATE_QUEUED, "error": None, "lane": None,
                      "started": None, "ended": None})
            if note:
                e["note"] = str(note)[:200]
            self._saved()
            return True


# --------------------------------------------------------------------------- display helpers
def wait_seconds(queue, jid):
    """Rough seconds until `jid` may start: the estimate of everything dispatchable ahead of it."""
    with _LOCK:
        total = 0.0
        for e in queue.entries():
            if e.get("id") == jid:
                return round(total, 1) if e.get("state") == STATE_QUEUED else 0.0
            if e.get("state") == STATE_QUEUED:
                total += float(e.get("est_s") or 0.0)
        return 0.0


def label(e):
    """One line for a queue panel row: state, title, cards, wait."""
    cards = e.get("cards") or []
    one = e.get("one_of") or []
    if one:
        where = "any card" if len(one) > 1 else "card %s" % (one[0] + 1)
    else:
        where = "both cards" if len(cards) > 1 else ("card %s" % (cards[0] + 1) if cards
                                                     else "no card")
    st = e.get("state")
    extra = ""
    if st == STATE_RUNNING:
        extra = " · ".join(x for x in (e.get("lane"), e.get("note")) if x)
    elif e.get("note"):
        extra = e["note"]
    elif e.get("error"):
        extra = e["error"][:60]
    return "%-8s %-38s %-12s %s" % (st, (e.get("title") or "")[:38], where, extra)
