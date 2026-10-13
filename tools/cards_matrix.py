#!/usr/bin/env python3
"""GPU-free assertions for Forge Lab's CARD MODE (Solo / Turbo / Studio).

    /opt/homebrew/bin/python3 tools/cards_matrix.py        # exits 1 on any failure

No Tk, no forge, no GPU. The interesting logic is factored into module-level PURE
functions in forge_lab.py — `turbo_preflight(director)` and `migrate_card_mode(value)` —
precisely so they can be driven here with a FAKE director (the real one would load a
model on GPU1). The in-app UI wiring (hint == params, row visibility) lives in the
FORGE_LAB_TESTCARDS driver.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab",
                                               os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


class FakeDirector:
    """Stands in for director.Director: no network, no GPU. `unload()` frees the model
    only when `unload_works`, so the "stays loaded -> refuse" case is reachable.

    `models` holds EVERY resident name, because the real question is whether the guard frees
    the whole card or just the writer's own model.
    """

    def __init__(self, loaded=True, reachable=True, unload_works=True, status_raises=False,
                 models=None):
        self.models = list(models if models is not None
                           else (["orcarouter/Qwen3.8-27B"] if loaded else []))
        self.reachable = reachable
        self.unload_works = unload_works
        self.status_raises = status_raises
        self.unloads = 0
        self.unload_alls = 0
        self.status_calls = 0
        self.model = "orcarouter/Qwen3.8-27B"

    def status(self):
        self.status_calls += 1
        if self.status_raises:
            raise RuntimeError("boom")
        return {"reachable": self.reachable,
                "loaded": list(self.models),
                "vram_hint": None}

    def unload(self, model=None):
        self.unloads += 1
        if self.unload_works:
            if model in self.models:
                self.models.remove(model)
            else:
                self.models = []
        return {"done": True}

    def unload_all(self):
        self.unload_alls += 1
        freed = list(self.models)
        for name in freed:
            self.unload(name)
        return freed


print("=" * 78)
print("CARD MODE matrix — turbo_preflight + settings migration (GPU-free, network-free)")
print("=" * 78)

print("\nmigrate_card_mode · old 1/2 -> three-way names, unknown -> Solo")
check(fl.migrate_card_mode("1") == "Solo", "'1' -> Solo")
check(fl.migrate_card_mode("2") == "Turbo", "'2' -> Turbo")
check(fl.migrate_card_mode("Solo") == "Solo", "'Solo' -> Solo")
check(fl.migrate_card_mode("Turbo") == "Turbo", "'Turbo' -> Turbo")
check(fl.migrate_card_mode("Studio") == "Studio", "'Studio' -> Studio")
check(fl.migrate_card_mode(None) == "Solo", "None -> Solo")
check(fl.migrate_card_mode("") == "Solo", "empty -> Solo")
check(fl.migrate_card_mode("wat") == "Solo", "unknown -> Solo")
check(fl.migrate_card_mode(2) == "Solo", "a non-string 2 -> Solo (fail-safe, not a crash)")

print("\nturbo_preflight · writer resident, unload frees it -> ALLOW")
d = FakeDirector(loaded=True, unload_works=True)
ok, msg = fl.turbo_preflight(d, wait_s=1.0, poll_s=0.02)
check(ok is True, "ok=True when the writer unloads (%r)" % msg)
check(d.unloads == 1, "unload() was called exactly once (got %d)" % d.unloads)
check("unloaded" in msg, "message says the writer was unloaded (%r)" % msg)

print("\nturbo_preflight · writer STAYS resident -> REFUSE")
d = FakeDirector(loaded=True, unload_works=False)
ok, msg = fl.turbo_preflight(d, wait_s=0.15, poll_s=0.02)
check(ok is False, "ok=False when the writer stays resident (%r)" % msg)
check(d.unloads == 1, "unload() was still attempted (got %d)" % d.unloads)
check("still resident" in msg, "message explains the refusal (%r)" % msg)

print("\nturbo_preflight · Director UNREACHABLE -> ALLOW (nothing is on card 2)")
d = FakeDirector(reachable=False)
ok, msg = fl.turbo_preflight(d, wait_s=0.15, poll_s=0.02)
check(ok is True, "ok=True when Ollama is unreachable (%r)" % msg)
check(d.unloads == 0, "unload() is NOT called when unreachable (got %d)" % d.unloads)
check("unreachable" in msg or "card 2" in msg, "message says why (%r)" % msg)

print("\nturbo_preflight · card 2 already clear -> ALLOW, no unload")
d = FakeDirector(loaded=False)
ok, msg = fl.turbo_preflight(d, wait_s=0.15, poll_s=0.02)
check(ok is True, "ok=True when nothing is resident (%r)" % msg)
check(d.unloads == 0, "no unload when already clear (got %d)" % d.unloads)

print("\nturbo_preflight · status() raises -> ALLOW (fail-open on the check, not the render)")
d = FakeDirector(status_raises=True)
ok, msg = fl.turbo_preflight(d, wait_s=0.15, poll_s=0.02)
check(ok is True, "ok=True when status() raises (%r)" % msg)

print("\nFREE CARD 2 · ON unloads EVERYTHING resident, not just the writer's own model")
d = FakeDirector(models=["orcarouter/Qwen3.8-27B",
                         "huihui_ai/mistral-small-abliterated:latest"])
ok, msg = fl.turbo_preflight(d, wait_s=1.0, poll_s=0.02, free_card=True)
check(ok is True, "ok=True when the card can be freed (%r)" % msg)
check(d.models == [], "EVERY resident model was unloaded (left: %r)" % d.models)
check(d.unloads == 2, "unload() ran once per resident model (got %d)" % d.unloads)
check("unloaded" in msg, "the message says it unloaded (%r)" % msg)
check("Qwen3.8-27B" in msg, "and NAMES what it freed (%r)" % msg)

print("\nFREE CARD 2 · OFF leaves your models alone and REFUSES")
d = FakeDirector(models=["orcarouter/Qwen3.8-27B"])
ok, msg = fl.turbo_preflight(d, wait_s=0.15, poll_s=0.02, free_card=False)
check(ok is False, "ok=False when the setting is off and something is resident (%r)" % msg)
check(d.unloads == 0, "NOTHING was unloaded (got %d)" % d.unloads)
check("FREE CARD 2" in msg, "and the message names the setting that would free it (%r)" % msg)

print("\nFREE CARD 2 · OFF with a clear card -> ALLOW (the setting only matters when busy)")
d = FakeDirector(models=[])
ok, msg = fl.turbo_preflight(d, wait_s=0.15, poll_s=0.02, free_card=False)
check(ok is True, "ok=True when nothing is resident, setting or not (%r)" % msg)


class OldDirector:
    """A director with no unload_all() — the guard must still free its own model."""
    model = "orcarouter/Qwen3.8-27B"

    def __init__(self):
        self.models = [self.model]
        self.unloads = 0

    def status(self):
        return {"reachable": True, "loaded": list(self.models)}

    def unload(self, model=None):
        self.unloads += 1
        self.models = []
        return {}


print("\nFREE CARD 2 · a director WITHOUT unload_all() still frees its own model")
od = OldDirector()
ok, msg = fl.turbo_preflight(od, wait_s=1.0, poll_s=0.02, free_card=True)
check(ok is True and od.unloads == 1,
      "the fallback unloads the writer's own model (%r, unloads=%d)" % (msg, od.unloads))

print("\nturbo_preflight · the guard is a PURE module-level function (Tk-free import)")
check(callable(getattr(fl, "turbo_preflight", None)), "forge_lab.turbo_preflight exists")
check(callable(getattr(fl, "director_polish", None)), "forge_lab.director_polish exists")
import inspect  # noqa: E402
_src = inspect.getsource(fl.turbo_preflight)
check("tkinter" not in _src and "Tk(" not in _src,
      "turbo_preflight never touches Tk (it runs in a worker thread)")

print("\nPY COMPILE")
import py_compile  # noqa: E402
for f in ("forge_lab.py", "director.py"):
    try:
        py_compile.compile(os.path.join(HERE, f), doraise=True)
        check(True, "%s compiles" % f)
    except py_compile.PyCompileError as exc:
        check(False, "%s compiles: %s" % (f, exc))

print("\n%s — %d checks failed" % ("FAIL" if FAILS else "ALL GREEN", len(FAILS)))
sys.exit(1 if FAILS else 0)