#!/usr/bin/env python3
"""Is a flattened API graph FAITHFUL to the template it came from?

WHY THIS EXISTS: our scratch flattener once emitted `pose_end_percent=0` for a template whose
own widgets say `1`. Nothing failed, nothing logged — the graph validated against /object_info,
rendered fine, and produced a subtly degraded clip. A shifted WIDGET VALUE is invisible to every
other check we have: the graph is structurally valid, the class types are known, the render
succeeds. Only a direct comparison against the template's own values catches it.

HOW: flattened node ids carry the template's inner id as a suffix (`672:587` came from template
node `587`), so nodes can be paired 1:1 by that suffix — NOT by type, which produces false
positives whenever a template has several nodes of one type (it always does).

Reads widgets_values_named (name -> value), the same field the flattener consumes, and compares
every LITERAL input. Linked inputs are skipped: the template routes many widgets through links,
and the flat graph legitimately still has a link (or a resolved value) there.

usage:  python3 tools/flatten_fidelity.py TEMPLATE.json FLATTENED.json [--ssh name@forge]
        (--ssh is the SSH TARGET; the ComfyUI URL forge:8188 is NOT an ssh host)
exit:   0 faithful / 1 mismatches found (so it can gate a build)
"""
import json, sys, os, subprocess

def template_by_id(g):
    out = {}
    def walk(o):
        if isinstance(o, dict):
            if o.get("type") and (o.get("widgets_values_named") or o.get("widgets_values")):
                out[str(o.get("id"))] = {"type": o["type"], "named": o.get("widgets_values_named") or {}}
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(g)
    return out

def load_flat(path, host=None):
    if host:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, "cat " + path],
                           capture_output=True, text=True)
        if r.returncode:
            sys.exit("cannot read %s on %s: %s" % (path, host, r.stderr.strip()[:200]))
        return json.loads(r.stdout)
    with open(os.path.expanduser(path)) as fh:
        return json.load(fh)

def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    tpath, fpath = sys.argv[1], sys.argv[2]
    host = None
    if "--ssh" in sys.argv:
        host = sys.argv[sys.argv.index("--ssh") + 1]
    with open(os.path.expanduser(tpath)) as fh:
        tb = template_by_id(json.load(fh))
    flat = load_flat(fpath, host)
    if not flat:
        sys.exit("empty/!loaded flattened graph")
    bad, unpaired, compared = [], 0, 0
    for nid, nd in flat.items():
        t = tb.get(str(nid).split(":")[-1])
        if not t:
            unpaired += 1
            continue
        lit = {k: v for k, v in (nd.get("inputs") or {}).items() if not isinstance(v, list)}
        for k, v in sorted(lit.items()):
            if k in t["named"]:
                compared += 1
                if t["named"][k] != v:
                    bad.append((t["type"], nid, k, v, t["named"][k]))
    print("compared %d literal inputs across %d flattened nodes (%d had no template counterpart)"
          % (compared, len(flat), unpaired))
    if bad:
        print("\nMISMATCHES — the flattened graph does not match its template:")
        for ty, nid, k, got, want in bad:
            print("  %-22s node %-10s %-24s flattened=%-10r template=%r" % (ty, nid, k, got, want))
        print("\na shifted widget value is SILENT everywhere else: the graph still validates and "
              "still renders. Fix the flattener, or write the template's value down as intentional.")
        sys.exit(1)
    print("FAITHFUL: every literal matches its own template node.")
    sys.exit(0)

if __name__ == "__main__":
    main()
