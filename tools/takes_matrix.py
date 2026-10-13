#!/usr/bin/env python3
"""GPU-free assertions for the TAKES rating / filter / lineage upgrade in forge_lab.py.

    /opt/homebrew/bin/python3 tools/takes_matrix.py      # exits 1 on any failure

Tk-free, forge-free, no GPU. Index writes go to a THROWAWAY FORGE_LAB_DATA dir set BEFORE
forge_lab is imported, so Ryan's real ~/forge-lab/runs/index.json is never touched.
"""
import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="forgetakes-")
os.environ["FORGE_LAB_DATA"] = TMP          # BEFORE import: RUNS derives from DATA

spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(ROOT, "forge_lab.py"))
fl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fl)

FAILS = []
N = [0]          # checks actually RUN — the summary used to print a hard-coded 0


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


# ---------------------------------------------------------------- set_rating round-trip
fl._index_insert({"id": "t1", "label": "one", "mode": "video", "steps": 12})
fl._index_insert({"id": "t2", "label": "two", "mode": "t2i"})
fl._index_insert({"id": "t3", "label": "three", "mode": "t2i"})


def rating_of(pid):
    return next((i.get("rating") for i in fl.read_index() if i["id"] == pid), "MISSING")


check(fl.set_rating("t1", 1) == 1, "set_rating(1) returns 1")
check(rating_of("t1") == 1, "★ lands in index.json")
check(fl.set_rating("t1", -1) == -1, "set_rating(-1) returns -1")
check(rating_of("t1") == -1, "✕ lands in index.json")
check(rating_of("t3") is None, "an untouched take has no rating key")
check(fl.set_rating("t2", 0) == 0 and rating_of("t2") == 0, "set_rating(0) stores 0")
check(fl.set_rating("t3", 9) == 1, "set_rating(9) clamps to 1")
check(fl.set_rating("t3", "junk") == 0, "set_rating('junk') clamps to 0")
check(fl.set_rating("t2", -7) == -1, "set_rating(-7) clamps to -1")

# ordering must not change (the takes list is newest-first)
_ids = [i["id"] for i in fl.read_index()]
check(_ids == ["t3", "t2", "t1"],
      "set_rating never reorders the index (%r)" % _ids)

# ---------------------------------------------------------------- take_rating
check(fl.take_rating({"rating": 1}) == 1, "take_rating reads 1")
check(fl.take_rating({"rating": -1}) == -1, "take_rating reads -1")
check(fl.take_rating({}) == 0, "take_rating(missing) is 0")
check(fl.take_rating({"rating": "x"}) == 0, "take_rating(junk) is 0")

# ---------------------------------------------------------------- filter_takes (pure)
items = [{"id": "k", "rating": 1}, {"id": "r", "rating": -1},
         {"id": "u", "rating": 0}, {"id": "n"}]      # n = no rating -> counted unrated


def ids(seq):
    return [i["id"] for i in seq]


check(ids(fl.filter_takes(items, "All")) == ["k", "r", "u", "n"], "All keeps everything")
check(ids(fl.filter_takes(items, "★ only")) == ["k"], "★ only -> keeps stars")
check(ids(fl.filter_takes(items, "unrated")) == ["u", "n"], "unrated -> rating 0 / missing")
check(ids(fl.filter_takes(items, "hide ✕")) == ["k", "u", "n"], "hide ✕ drops rejects")
check(ids(fl.filter_takes(items, "nonsense")) == ["k", "r", "u", "n"], "unknown filter -> All")
# unkept only
check(ids(fl.filter_takes(items, "All", unkept_only=True)) == ["r", "u", "n"],
      "unkept only drops ★ keeps")
# the INTERSECTION of the two controls
check(ids(fl.filter_takes(items, "hide ✕", unkept_only=True)) == ["u", "n"],
      "hide ✕ ∩ unkept only -> unrated non-rejects")
check(ids(fl.filter_takes(items, "★ only", unkept_only=True)) == [],
      "★ only ∩ unkept only -> empty (they contradict)")
check(fl.filter_takes([], "All") == [], "empty in, empty out")
check(fl.filter_takes(None, "All") == [], "None in, empty out")

# ---------------------------------------------------------------- take_lineage
check(fl.take_lineage({"character": "alice", "project": "ep7", "shot": 3})
      == "from: alice · ep7 · shot 3", "lineage renders character · project · shot N")
check(fl.take_lineage({"character": "alice"}) == "from: alice",
      "lineage with only a character")
check(fl.take_lineage({"project": "ep7"}) == "from: ep7", "lineage with only a project")
check(fl.take_lineage({"shot": 0}) == "from: shot 0", "shot 0 is a real shot, not missing")
check(fl.take_lineage({}) == "", "no fields -> no lineage line")
check(fl.take_lineage({"character": "g", "shot": None}) == "from: g",
      "shot None is omitted")
check(fl.take_lineage(None) == "", "None -> no lineage line")

# ---------------------------------------------------------------- take_specs
check(fl.take_specs({"mode": "video", "steps": 12, "wall": 46.4}) == "h3 · 12 steps · 46.4s",
      "specs = engine · steps · seconds (h3)")
check(fl.take_specs({"mode": "t2i", "steps": 25}) == "qwen · 25 steps",
      "specs omits seconds when absent (qwen)")
check(fl.take_specs({"mode": "video", "promote": True}) == "",
      "a promoted take has no specs line")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f in FAILS:
    print("  FAILED: %s" % f)
sys.exit(1 if FAILS else 0)