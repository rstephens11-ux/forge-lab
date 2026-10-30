"""SERIES — the show bible a cartoon series is written from (roadmap 2026-10-18, item 9).
Tk-free; Quick's "Series" view is the window.

A series is DATA/series/<slug>/series.json:
    name, logline, tone, format, cast [character slugs], place (a kept place slug or None),
    gags [running gags], rules [things that are always true], episodes [quick job ids]

Every Quick video made "in" a series:
  * starts from the series' cast, format and place (the form fills in; you can still change
    anything),
  * gets the bible in the writer's brief (logline, tone, running gags, rules, and the last
    few episodes so it doesn't repeat them),
  * is added to the series' episode list when it's made.
next_ideas() asks the writer for episode ideas that fit the bible and haven't been done.
"""
import json
import os
import re
import time

import quick

IDEAS_SCHEMA = {"type": "object", "properties": {
    "ideas": {"type": "array", "items": {"type": "string"}}}, "required": ["ideas"]}


def root():
    d = os.path.join(quick.fl.DATA, "series")
    os.makedirs(d, exist_ok=True)
    return d


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "series"
    base, k = s, 2
    while os.path.exists(os.path.join(root(), s)):
        s, k = "%s-%d" % (base, k), k + 1
    return s


def _path(slug):
    return os.path.join(root(), slug, "series.json")


def _lines(v):
    if isinstance(v, str):
        v = v.splitlines()
    return [x.strip(" -•\t") for x in (v or []) if x and x.strip(" -•\t")]


def new(name, logline="", tone="", fmt="sketch", cast=(), place=None, gags=(), rules=()):
    if not (name or "").strip():
        raise ValueError("give the series a name")
    sr = {"slug": _slug(name), "name": name.strip()[:60], "logline": (logline or "").strip(),
          "tone": (tone or "").strip(), "format": fmt, "cast": [c for c in cast or [] if c][:3],
          "place": place or None, "gags": _lines(gags), "rules": _lines(rules),
          "episodes": [], "created": time.time()}
    save(sr)
    return sr


def save(sr):
    os.makedirs(os.path.dirname(_path(sr["slug"])), exist_ok=True)
    tmp = _path(sr["slug"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sr, f, indent=1)
    os.replace(tmp, _path(sr["slug"]))
    return sr


def load(slug):
    try:
        return json.load(open(_path(slug)))
    except Exception:
        return None


def update(slug, **fields):
    sr = load(slug)
    if not sr:
        raise ValueError("no such series")
    for k, v in fields.items():
        if k in ("gags", "rules"):
            v = _lines(v)
        if k in ("name", "logline", "tone", "format", "cast", "place", "gags", "rules"):
            sr[k] = v
    return save(sr)


def all_series():
    out = []
    for d in sorted(os.listdir(root())):
        sr = load(d)
        if sr:
            out.append(sr)
    return sorted(out, key=lambda s: -(s.get("created") or 0))


def episodes(sr):
    """The series' episodes as Quick jobs, oldest first (gone ones skipped)."""
    out = []
    for jid in sr.get("episodes") or []:
        j = quick.load(jid)
        if j:
            out.append(j)
    return out


def add_episode(slug, job):
    """A finished video joins the series. A Final re-shoot of an episode (the same script)
    takes that episode's place instead of counting twice."""
    sr = load(slug)
    if not sr or not job.get("id") or job["id"] in sr["episodes"]:
        return sr
    src = job.get("from_job")
    prev = quick.load(src) if src in sr["episodes"] else None
    if prev and job.get("dialogue") and prev.get("dialogue") == job.get("dialogue"):
        sr["episodes"][sr["episodes"].index(src)] = job["id"]
    else:
        sr["episodes"].append(job["id"])
    save(sr)
    return sr


def _names(slugs):
    out = []
    for s in slugs or []:
        meta = quick.fl.read_character(s) or {}
        out.append("%s%s" % (meta.get("name") or s, (" (%s)" % meta["feature"])
                             if meta.get("feature") else ""))
    return out


def brief(sr, recent=4):
    """The bible as a block for the writer's brief."""
    if not sr:
        return ""
    eps = [j for j in episodes(sr) if j.get("state") == "done"][-recent:]
    parts = ["SERIES: %s" % sr["name"]]
    if sr.get("logline"):
        parts.append("THE SHOW: %s" % sr["logline"])
    if sr.get("tone"):
        parts.append("TONE: %s" % sr["tone"])
    if sr.get("cast"):
        parts.append("REGULAR CAST: %s" % "; ".join(_names(sr["cast"])))
    if sr.get("rules"):
        parts.append("ALWAYS TRUE IN THIS SHOW:\n" + "\n".join("- " + r for r in sr["rules"]))
    if sr.get("gags"):
        parts.append("RUNNING GAGS (use at most ONE, and only if it fits naturally):\n" +
                     "\n".join("- " + g for g in sr["gags"]))
    if eps:
        parts.append("EARLIER EPISODES (don't repeat their situation or their jokes):\n" +
                     "\n".join("- %s: %s" % (j.get("title") or "", j.get("idea") or "")
                               for j in eps))
    parts.append("Keep every regular character exactly in character: same voice, same "
                 "attitude, same blind spot as in earlier episodes.")
    return "\n\n" + "\n".join(parts) + "\n"


def next_ideas(director, sr, n=3):
    """n one-line episode ideas that fit the bible and aren't done yet."""
    import formats
    f = formats.get(sr.get("format") or "sketch")
    ask = ("Pitch %d new episode ideas for this show, as one-line loglines. Each must be a "
           "fresh situation for the SAME regular characters, work as a %s (%s), be played "
           "completely straight, and not repeat an earlier episode.%s\n\nReturn JSON "
           '{"ideas": ["...", ...]}.' % (n, f["name"], f["shape"], brief(sr, recent=12)))
    try:
        if hasattr(director, "load"):
            director.load()
    except Exception:
        pass
    out = director._chat("You are the head writer of a short comedy series. You protect "
                         "its characters and never repeat an episode.", ask, IDEAS_SCHEMA,
                         temperature=0.9)
    ideas = [str(x).strip() for x in (out or {}).get("ideas") or [] if str(x).strip()]
    done = {(j.get("idea") or "").strip().lower() for j in episodes(sr)}
    return [i for i in ideas if i.lower() not in done][:n]
