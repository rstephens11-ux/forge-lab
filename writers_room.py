"""THE WRITERS' ROOM — how Quick writes a comedy script with the local writer (Gemma 4 31B
since 2026-10-24; the writer bake-off picked it and the plan-first prompt below).

From the review panel's comedy writer (2026-10-17), then the bake-off (2026-10-23, claude.ai
project doc writer-bakeoff-2026-10-23.md): the old "find the GAME" room wrote abstract,
jargon-heavy scenes ("statistical identity crisis"); a plain prompt that plans a situation a
stranger gets in two seconds and ONE joke scored higher with every model.

  B. DRAFTS    3 drafts, each PLAN FIRST: situation, joke, three concrete escalations and
               the button, then the lines ending on that button word for word
  C. JUDGE     each draft scored ALONE against a 6-point rubric (side-by-side judging is
               position-biased); totals recomputed in code; penalties for ignoring the idea
               and for banned patterns
  D. PUNCH-UP  six alternative last lines (callback, reversal, admission, top,
               understatement, status flip), the editor picks
  E. CUT       over budget: shorten MIDDLE lines only, hook and button kept verbatim
  YOU PICK    Quick's default: 3 drafts, shown to you side by side with their plans; no
               judge (drafts_for_pick). The steps below run only when nobody's there to pick
               (the night shift).
  F. COLD READ a reader who sees ONLY the script (no idea, no plan) says what happens and
               which lines lost them; each draft loses points for that, and the scene that
               will be shot has those lines rewritten (kept only if it then reads clearer)

(A. the separate GAME step is retired: find_game is kept for anyone who calls it, and a game
passed to draft() still goes in its ask.) Every step falls back: no usable draft -> the old
one-shot director.dialogue(); a failed judge/punch-up/cut keeps what it had. Tk-free.
"""
import re

WORDS_PER_SECOND = 2.4

# THE WRITER (2026-10-24, the bake-off's winning prompt - claude.ai project doc
# writer-bakeoff-2026-10-23.md). The old head-writer prompt ("find the GAME...") scored 3.83 of
# 10 with four blind judges, this plain one 4.50, and with Gemma 4 31B 6.53. The old scripts
# lost viewers on jargon and abstract games ("statistical identity crisis").
SYSTEM_WRITE = (
    "You write short comedy videos that people share. You write the way real people talk: "
    "short, everyday words. Your scenes are simple: one clear situation, one joke, played "
    "straight and pushed further each line. The characters never know they are funny and "
    "never explain the joke. Every word in `text` is spoken aloud; actions go only in "
    "`direction`. Write numbers as words. Every speaker must be one of the given cast names "
    "exactly (upper-case). Answer with JSON only.")

_LINES_ITEMS = {"type": "object", "required": ["speaker", "text"],
                "properties": {"speaker": {"type": "string", "minLength": 1},
                               "direction": {"type": "string"},
                               "text": {"type": "string", "minLength": 1}}}
DIALOGUE_SCHEMA = {"type": "object", "required": ["title", "lines"], "properties": {
    "title": {"type": "string", "minLength": 1, "maxLength": 120},
    "lines": {"type": "array", "minItems": 2, "items": _LINES_ITEMS}}}

# ---------------------------------------------------------------------------- A. game
GAME_SCHEMA = {"type": "object", "required": ["games", "best"], "properties": {
    "games": {"type": "array", "minItems": 3, "maxItems": 3, "items": {
        "type": "object", "required": ["odd_one", "straight_one", "game", "steps", "button"],
        "properties": {"odd_one": {"type": "string"}, "straight_one": {"type": "string"},
                       "game": {"type": "string"},
                       "steps": {"type": "array", "minItems": 3, "maxItems": 3,
                                 "items": {"type": "string"}},
                       "button": {"type": "string"}}}},
    "best": {"type": "integer"}}}
SYSTEM_GAME = ("You are the head writer in a sketch-comedy room. You do not write lines yet; "
               "you find the GAME of the scene. Answer with JSON only.")
GAME_ASK = """FORMAT: {shape}

THE IDEA: {idea}

CAST:
{cast_block}

Find three DIFFERENT games for this scene. A game is the ONE unusual pattern that repeats
and gets bigger, e.g. "she never admits fault, so he apologises for bigger and bigger
things" or "he treats the haunting as a parking violation".

For each game give:
- odd_one: which cast name behaves unusually, and the one sentence of logic they never break.
- straight_one: which cast name reacts like a normal person, and what they want.
- game: the pattern in one sentence.
- steps: three moments where the game repeats, each BIGGER and more specific than the last.
  Use the jobs, places and objects in THE IDEA.
- button: the final line idea - a reversal, an admission, or a callback - under 8 words.

Then set "best" to the number (1, 2 or 3) of the game that is funniest, simplest to
understand in two seconds, and most about THE IDEA."""

# ---------------------------------------------------------------------------- B. drafts
# PLAN FIRST, then the lines (the bake-off's prompt B): the plan is a situation a stranger gets
# in two seconds, ONE joke, three concrete escalations and the button - in that order, so the
# button is written before the lines that lead to it.
DRAFT_SCHEMA = {"type": "object",
                "required": ["situation", "joke", "escalations", "button", "title", "lines"],
                "properties": {"situation": {"type": "string"}, "joke": {"type": "string"},
                               "escalations": {"type": "array", "items": {"type": "string"}},
                               "button": {"type": "string"},
                               "title": {"type": "string", "minLength": 1, "maxLength": 60},
                               "lines": {"type": "array", "minItems": 2,
                                         "items": _LINES_ITEMS}}}
DRAFT_ASK = """{brief}

CAST (every speaker MUST be one of: {names}):
{cast_block}
{game_block}
Before writing lines, fill in the plan:
- situation: one plain sentence a stranger understands in two seconds - who, where, what
  is going wrong.
- joke: the ONE funny thing about it, in one plain sentence (e.g. "the doorbell is so
  polite it refuses him in nicer and nicer ways", "the dog does dog things and she
  keeps treating them as great interview answers").
- escalations: three moments where the joke gets bigger. Each one is a concrete, visual,
  everyday thing (an object, an action, a number) - not a concept.
- button: the last line, under eight words. Surprising, but obvious once you hear it.{improve}
{avoid}
Then write the lines: about {n} lines, about {words} words in total.
- Line one sets up the situation so a stranger gets it.
- Every line answers the line before it.
- Plain words only. NO jargon, no technical or corporate words, no made-up terms, no
  statistics-speak ("algorithm", "data", "parameters", "identity", "optimise",
  "percent") unless a character would really say it out loud in that situation.
- Absurd is fine, confusing is not: a viewer must be able to say why it's funny.
- `text` is only words the speaker says out loud. Never a sound or an action written as
  words ("Woof!", "Beep beep", "*wags tail*", "(sighs)"): a sound or an action goes in
  `direction`. A speaker who cannot talk gets no lines.
- A cast member marked VOICE ONLY is heard, never seen (a device, a car, a voice from a
  speaker): it talks like anyone else, but it has no body - no hands, no face.
- End on the button, word for word.
Return JSON only."""

# ---------------------------------------------------------------------------- C. judge
RUBRIC = ("hook", "game", "listening", "heighten", "specific", "button", "straight")
JUDGE_SCHEMA = {"type": "object", "required": list(RUBRIC) + ["weakest_line", "total"],
                "properties": {k: {"type": "integer"} for k in
                               list(RUBRIC) + ["weakest_line", "total"]}}
SYSTEM_JUDGE = ("You are a tough sketch-comedy script editor. You score scripts honestly "
                "and harshly; most first drafts score 2 or 3. Answer with JSON only.")
JUDGE_ASK = """THE IDEA: {idea}
FORMAT: {format_name}

SCRIPT:
{numbered}

Score each from 1 (bad) to 5 (great):
- hook: from line 1 alone, does a stranger know WHO, WHERE and WHAT IS WRONG? 1 = vague or
  a greeting; 5 = instantly clear with a concrete detail.
- game: is there ONE clear pattern that every line plays? 1 = separate one-liners or the
  topic changes; 5 = one game, start to finish, all about THE IDEA.
- listening: does every line reply directly to the line before it? 1 = two monologues
  interleaved; 5 = each line uses something from the previous one.
- heighten: does the game get bigger each time it repeats? 1 = same level or sideways;
  5 = three clear steps up.
- specific: real names, numbers, objects from THE IDEA's world? 1 = generic insults or
  vague claims; 5 = precise and vivid.
- button: is the last line the biggest laugh, a reversal or callback, short? 1 = a pun,
  a restatement or a fizzle; 5 = it reframes the whole scene.
- straight: (one-person scripts: 5) does ONE character stay the normal, reasonable one
  until the last two lines, so the other's oddness has something to bounce off? 1 = both
  are equally odd, or they swap roles midway; 5 = a clear straight character throughout.
- weakest_line: the number of the single weakest line.
- total: the sum of the seven scores."""

# ---------------------------------------------------------------------------- D. punch-up
PUNCH_SCHEMA = {"type": "object", "required": ["options"], "properties": {
    "options": {"type": "array", "minItems": 6, "maxItems": 6, "items": {"type": "string"}}}}
PUNCH_ASK = """Here is a finished comedy scene. Its LAST line is the button.

{numbered}

Write six alternative LAST lines, said by {speaker}, each a different kind of button:
1. a CALLBACK - an earlier detail returns with a new meaning;
2. a REVERSAL - the straight one turns out to be worse than the odd one;
3. an ADMISSION - someone accidentally confesses the real truth;
4. a TOP - the game's biggest, most specific step yet;
5. an UNDERSTATEMENT - the calmest possible reaction to the worst news;
6. a STATUS FLIP - the one who was losing wins in one line.
Each under eight words, spoken aloud, deadpan, numbers as words. JSON only."""
PICK_SCHEMA = {"type": "object", "required": ["best", "why"],
               "properties": {"best": {"type": "integer"}, "why": {"type": "string"}}}
PICK_ASK = """SCENE (without its last line):
{numbered}

Possible last lines:
{options}

Which last line gets the biggest laugh as the final line of THIS scene - it must follow
from the line before it and pay off the game? Return {{"best": number, "why": "one
sentence"}}."""

# ---------------------------------------------------------------------------- F. cold read
# (test run 2026-10-18, "the dialogue makes no sense": the judge scored "I am returning
# this single grape for negligence." / "Technically speaking, the bunch is one item." 33 of
# 35 — it read the script knowing THE IDEA and the game, and filled the gaps itself. A
# viewer has neither. The cold read sees ONLY what's on screen.)
COLD_SCHEMA = {"type": "object", "required": ["what_happens", "confusing", "makes_sense"],
               "properties": {
                   "what_happens": {"type": "string"},
                   "confusing": {"type": "array", "items": {
                       "type": "object", "required": ["line", "why"],
                       "properties": {"line": {"type": "integer"},
                                      "why": {"type": "string"}}}},
                   "makes_sense": {"type": "integer"}}}
SYSTEM_COLD = ("You are an ordinary person scrolling short videos. You have read no notes "
               "and know nothing about the scene except what you see and hear. You are "
               "honest about what you did not follow. Answer with JSON only.")
COLD_ASK = """A short comedy video. This is everything you see and hear, in order
(what people do is in brackets):

{seen}

1. what_happens: in one plain sentence, what happens in this scene?
2. confusing: every line a normal viewer would NOT follow on one watch - a reply that does
   not answer the line before it, a word that does not fit ("returning a grape for
   negligence"), a jump in logic the scene never explains, a joke that needs notes you
   don't have. Absurd is fine when the scene makes its own logic clear; confusing is not.
   Give the line number and why, in a few words. Empty when every line follows.
3. makes_sense: 1 = I couldn't say what happened; 3 = I got the gist but some lines lost
   me; 5 = every line follows from the one before and I got every joke."""
CLARIFY_ASK = """This comedy scene loses viewers. A viewer who saw it cold said:
- what happens: {what}
- lines they did not follow:
{why}

THE GAME the scene is meant to play: {game}

CAST (every speaker MUST be one of: {names})

SCENE:
{numbered}

Rewrite ONLY the lines listed so a stranger follows every step on one watch: plain words
over clever ones, each reply answering the line before it, the scene's logic said out
loud once. Keep the same characters, the same joke and game, the same number of lines and
about the same length. Keep every other line exactly as written{keep_last}.
Return {{title, lines:[{{speaker, direction, text}}]}} as JSON only."""

CUT_ASK = ("This scene runs {words} words; the target is {budget}. Cut it to about {budget} "
           "words by removing or shortening MIDDLE lines only. Keep line one and the last "
           "line exactly as written. Keep the game.\n\nCAST (every speaker MUST be one of: "
           "{names})\n\nSCENE:\n{numbered}\n\nReturn {{title, lines:[{{speaker, direction, "
           "text}}]}} as JSON only.")

# patterns the panel's writer flagged: random insults, filler the model reaches for
BANNED = re.compile(r"you call that a|rent[- ]free|\bsuperior\b|\blol\b|\bfunny\b|"
                    r"owe me rent", re.I)
_STOP = set("""a an the and or but of to in on at by for with about over from into is are was
were be been it its this that these those who whose which what where when why how their his
her they them he she we you i my your our argue argues arguing argument fault whose own very
just really""".split())


# ---------------------------------------------------------------------------- helpers
# the EDIT adds time the words don't: handles around every cut (~0.45 s a line), the hold
# after the last line and the reaction (~2 s). Budgeting words for the full length made a
# 30 s ask cut to 38 s (round-2 review, the short-form editor).
EDIT_OVERHEAD_PER_LINE = 0.45
EDIT_OVERHEAD_END = 2.0
CUT_OVER = 1.1                  # ask for a cut once the words run 10 % over budget


def n_lines(seconds, cast=2, rhythm=None):
    """About one line per 3.8 seconds of video (a setup needs room before its payoff, and
    the cuts take time). Fewer lines also means fewer shots to render. `rhythm` = a
    format's own (seconds per line, most lines) - formats.rhythm()."""
    per, most = rhythm or (3.8, 10)
    return max(3 if cast > 1 else 2, min(int(most), int(round(float(seconds) / per))))


def budget(seconds, cast=2, rhythm=None):
    """Words that fit `seconds` of FINISHED video once the edit's own time is taken out."""
    n = n_lines(seconds, cast, rhythm)
    spoken = float(seconds) - EDIT_OVERHEAD_PER_LINE * n - EDIT_OVERHEAD_END
    return max(8, int(max(spoken, float(seconds) * 0.5) * 2.5))


def length_rule(seconds, cast=2, rhythm=None):
    return ("\n\nLENGTH: %d seconds spoken - about %d words in total, in %d lines. Lines "
            "are UNEVEN: setups up to fourteen words, payoffs one to six words, the button "
            "under eight words. Never write two long lines in a row."
            % (int(seconds), budget(seconds, cast, rhythm), n_lines(seconds, cast, rhythm)))


def idea_nouns(idea):
    """The content words of the idea (lighthouse, keeper, pigeon, trainer, fog)."""
    ws = re.findall(r"[a-z][a-z'-]+", (idea or "").lower())
    out = []
    for w in ws:
        w = w.strip("'-")
        if len(w) > 2 and w not in _STOP and not w.endswith("ly") and w not in out:
            out.append(w)
    return out


def coverage(lines, idea):
    """How many of the idea's content words the script actually uses (stems match)."""
    text = " ".join((li.get("text") or "") for li in lines).lower()
    return sum(1 for w in idea_nouns(idea) if w[:5] in text)


def numbered(lines):
    return "\n".join("%d. %s: %s" % (i + 1, li.get("speaker", ""), li.get("text", ""))
                     for i, li in enumerate(lines))


def seen(lines):
    """The script as a viewer gets it: who says what, and what they do."""
    out = []
    for i, li in enumerate(lines):
        d = (li.get("direction") or "").strip().strip("()[] ")
        out.append("%d. %s%s: %s" % (i + 1, li.get("speaker", ""),
                                     " [%s]" % d if d and d.lower() != "beat" else "",
                                     li.get("text", "")))
    return "\n".join(out)


def words_in(lines):
    return sum(len((li.get("text") or "").split()) for li in lines)


def _cast_block(cast):
    return "\n".join("  %s: %s" % (k, v) for k, v in sorted(cast.items()))


def _validate(out, cast):
    try:
        import director as _d
        return _d.validate_dialogue(out, cast)
    except Exception:
        lines = (out or {}).get("lines") or []
        return [] if len(lines) >= 2 else ["need at least 2 lines"]


# ---------------------------------------------------------------------------- the steps
def find_game(director, shape, idea, cast):
    out = director._chat(SYSTEM_GAME, GAME_ASK.format(shape=shape, idea=idea or "(open)",
                                                      cast_block=_cast_block(cast)),
                         GAME_SCHEMA, temperature=0.9)
    games = [g for g in (out or {}).get("games") or [] if isinstance(g, dict) and g.get("game")]
    if not games:
        raise ValueError("no game came back")
    i = int((out or {}).get("best") or 1) - 1
    return games[i] if 0 <= i < len(games) else games[0]


def _game_block(game):
    if not game:
        return ""
    st = list(game.get("steps") or []) + ["", "", ""]
    return ("\nTHE GAME (play this and nothing else):\n- %s\n- %s\n- game: %s\n"
            "- it heightens like this: 1) %s 2) %s 3) %s\n- button idea: %s\n"
            % (game.get("odd_one", ""), game.get("straight_one", ""), game.get("game", ""),
               st[0], st[1], st[2], game.get("button", "")))


def plan_text(d):
    """The plan a draft was written from, in one line: "situation | joke"."""
    return " | ".join(x for x in ((d or {}).get("situation", ""), (d or {}).get("joke", ""))
                      if (x or "").strip())


def _avoid_block(avoid):
    """The jokes the other drafts already use: this one must find a DIFFERENT one (2026-10-24:
    Gemma's three doorbell drafts were one script three times - same opening, same joke)."""
    js = [j.strip() for j in avoid or [] if (j or "").strip()]
    if not js:
        return ""
    return ("- OTHER DRAFTS ALREADY USE THESE JOKES - yours must be a clearly DIFFERENT funny "
            "thing about the same situation, with a different first line and a different "
            "button:\n" + "\n".join("    * %s" % j for j in js) + "\n")


def draft(director, brief, cast, seconds, game=None, rhythm=None, avoid=None):
    """One PLAN-first draft (situation, joke, three escalations, the button, then the lines),
    validated (one retry with the problems), the button forced to be the last line. Raises
    when it can't be made valid. `brief` is the head of the ask: the format's shape and THE
    IDEA (formats.writer_brief), plus any show bible / product / taste notes."""
    names = ", ".join(sorted(cast))
    ask = DRAFT_ASK.format(brief=brief.strip(), names=names, cast_block=_cast_block(cast),
                           game_block=_game_block(game),
                           improve=" You may improve the button idea." if game else "",
                           n=n_lines(seconds, len(cast), rhythm),
                           words=budget(seconds, len(cast), rhythm),
                           avoid=_avoid_block(avoid))
    problems = None
    for _ in range(2):
        a = ask if not problems else (ask + "\n\nYour previous attempt was REJECTED:\n- " +
                                      "\n- ".join(problems) + "\nReturn corrected JSON.")
        out = director._chat(SYSTEM_WRITE, a, DRAFT_SCHEMA, temperature=0.85)
        lines = [dict(li) for li in (out or {}).get("lines") or [] if isinstance(li, dict)]
        btn = ((out or {}).get("button") or "").strip()
        if lines and btn and btn.lower().strip(" .!?") not in \
                (lines[-1].get("text") or "").lower():
            lines[-1]["text"] = btn                 # the button IS the last line
        res = {"title": (out or {}).get("title") or "Untitled", "lines": lines,
               "hook": (out or {}).get("hook") or (lines[0].get("text") if lines else ""),
               "button": btn, "situation": (out or {}).get("situation") or "",
               "joke": (out or {}).get("joke") or "",
               "escalations": [e for e in (out or {}).get("escalations") or []
                               if isinstance(e, str)]}
        problems = _validate(res, cast)
        if not problems:
            return res
    raise ValueError("draft rejected: %s" % "; ".join(problems or []))


def score(director, d, idea, format_name):
    """(total, scores) for one draft, judged alone; the total recomputed in code, minus 3
    when it ignores the idea and 2 per banned pattern."""
    v = director._chat(SYSTEM_JUDGE, JUDGE_ASK.format(idea=idea or "(open)",
                                                      format_name=format_name,
                                                      numbered=numbered(d["lines"])),
                       JUDGE_SCHEMA, temperature=0.1) or {}
    sc = {k: max(1, min(5, int(v.get(k) or 1))) for k in RUBRIC}
    total = sum(sc.values())
    if idea and len(idea_nouns(idea)) >= 2 and coverage(d["lines"], idea) < 2:
        total -= 3
        sc["penalty_idea"] = -3
    bad = sum(1 for li in d["lines"] if BANNED.search(li.get("text") or ""))
    if bad:
        total -= 2 * bad
        sc["penalty_banned"] = -2 * bad
    rep = repeat_penalty(d["lines"])
    if rep:
        total -= rep
        sc["penalty_repeat"] = -rep
    sc["weakest_line"] = v.get("weakest_line")
    return total, sc


_STOP = set("""a an the and or but so to of in on at for with from by is are was were be been
it its this that these those i you he she we they me him her us them my your his our their
not no do does did have has had will would can could should just very too then than as if
what who why how when where there here all any some one two""".split())


def repeat_penalty(lines):
    """2 points when the BUTTON leans on a word the scene has already used twice (the
    round-2 review: "load-bearing" three times, the last time as the button), and 2 when
    one body-part gag word is said three or more times (CRAFT: mention it once at most)."""
    import re as _re
    words = [[w for w in _re.findall(r"[a-z][a-z'-]+", (li.get("text") or "").lower())
              if w not in _STOP and len(w) > 2] for li in lines]
    if not words:
        return 0
    pen = 0
    before = {}
    for ws in words[:-1]:
        for w in set(ws):
            before[w] = before.get(w, 0) + 1
    if any(before.get(w, 0) >= 2 for w in set(words[-1])):
        pen += 2
    body = _re.compile(r"\b(ears?|nose|neck|jaw|chin|head|hands?|feet|eyebrows?|teeth|"
                       r"forehead|arms?|legs?|eyes?|moustache|mustache|hair|mouth)\b")
    counts = {}
    for li in lines:
        for m in body.findall((li.get("text") or "").lower()):
            k = m.rstrip("s")
            counts[k] = counts.get(k, 0) + 1
    if any(c >= 3 for c in counts.values()):
        pen += 2
    return pen


def strip_speaker(text, names=()):
    """A line's text without a leading speaker label ("ELENA: Returned." -> "Returned.").
    The punch-up options came back labelled, and the label was spoken and captioned."""
    t = (text or "").strip()
    for _ in range(2):
        m = re.match(r"^\s*([A-Za-z][A-Za-z .'\-]{0,30}?)\s*[:\u2014-]\s+(.+)$", t)
        if not m:
            break
        lab = m.group(1).strip().lower()
        if lab in {n.strip().lower() for n in names if n} or (not names and lab.isalpha()):
            t = m.group(2).strip()
        else:
            break
    return t


def punch_up(director, d):
    """Six alternative buttons, the editor picks (the original is option 1)."""
    lines = d["lines"]
    spk = lines[-1].get("speaker", "")
    out = director._chat(SYSTEM_WRITE, PUNCH_ASK.format(numbered=numbered(lines), speaker=spk),
                         PUNCH_SCHEMA, temperature=1.0) or {}
    names = {li.get("speaker", "") for li in lines}
    opts = [strip_speaker(o, names) for o in out.get("options") or []
            if isinstance(o, str) and o.strip()]
    if not opts:
        return d, None
    allo = [lines[-1].get("text", "")] + opts[:6]
    pick = director._chat(SYSTEM_JUDGE, PICK_ASK.format(
        numbered=numbered(lines[:-1]),
        options="\n".join("%d. %s" % (i + 1, o) for i, o in enumerate(allo))),
        PICK_SCHEMA, temperature=0.1) or {}
    i = int(pick.get("best") or 1) - 1
    if 0 < i < len(allo):
        new = dict(d, lines=[dict(li) for li in lines])
        new["lines"][-1]["text"] = allo[i]
        return new, allo[i]
    return d, None


def cold_read(director, d):
    """{"makes_sense": 1-5, "confusing": [(line, why)], "what": str} from a reader who sees
    only the script."""
    v = director._chat(SYSTEM_COLD, COLD_ASK.format(seen=seen(d["lines"])), COLD_SCHEMA,
                       temperature=0.1) or {}
    n = len(d["lines"])
    conf = []
    for c_ in v.get("confusing") or []:
        try:
            k = int(c_.get("line"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= k <= n and k not in [x[0] for x in conf]:
            conf.append((k, (c_.get("why") or "").strip()))
    try:
        ms = max(1, min(5, int(v.get("makes_sense") or 3)))
    except (TypeError, ValueError):
        ms = 3
    if conf and ms > 4:
        ms = 4                                  # "5" with lines it didn't follow isn't a 5
    return {"makes_sense": ms, "confusing": sorted(conf),
            "what": (v.get("what_happens") or "").strip(),
            "_lines": [li.get("text") for li in d["lines"]]}


def clear_enough(cr):
    return cr["makes_sense"] >= 4 and not cr["confusing"]


def clarify(director, d, cast, cr, game_text=""):
    """Rewrite only the lines the cold read didn't follow. Returns the new draft, or `d`
    when the rewrite isn't a valid scene of about the same shape."""
    lines = d["lines"]
    bad = {k for k, _w in cr["confusing"]}
    out = director._chat(SYSTEM_WRITE, CLARIFY_ASK.format(
        what=cr.get("what") or "(couldn't say)",
        why="\n".join("  line %d: %s" % (k, w or "didn't follow") for k, w in
                      cr["confusing"]) or "  (the whole thing - nothing quite follows)",
        game=game_text or "(as written)", names=", ".join(sorted(cast)),
        numbered=numbered(lines),
        keep_last="" if len(lines) in bad else ", including the last line"),
        DIALOGUE_SCHEMA, temperature=0.5) or {}
    new = [dict(li) for li in out.get("lines") or [] if isinstance(li, dict)]
    if not new or abs(len(new) - len(lines)) > 1:
        return d
    names = set(cast) | {li.get("speaker", "") for li in lines}
    for li in new:
        li["text"] = strip_speaker(li.get("text"), names)
    if len(lines) not in bad and len(new) == len(lines):
        new[-1] = dict(lines[-1])               # an unflagged button stays word for word
    res = dict(d, lines=new)
    if _validate(res, cast) or words_in(new) > words_in(lines) * 1.3 + 4:
        return d
    return res


def cut(director, d, cast, seconds, rhythm=None):
    """Over budget: shorten the MIDDLE; the hook and the button come back verbatim."""
    lines = d["lines"]
    out = director._chat(SYSTEM_WRITE, CUT_ASK.format(
        words=words_in(lines), budget=budget(seconds, len(cast), rhythm), names=", ".join(sorted(cast)),
        numbered=numbered(lines)), DIALOGUE_SCHEMA, temperature=0.4) or {}
    new = [dict(li) for li in out.get("lines") or [] if isinstance(li, dict)]
    if len(new) < 2:
        return d
    new[0] = dict(lines[0])
    new[-1] = dict(lines[-1])
    res = dict(d, lines=new)
    return res if not _validate(res, cast) and words_in(new) < words_in(lines) else d


def drafts_for_pick(director, brief, cast, seconds, idea="", shape="", n=3, say=None,
                    log=None, rhythm=None):
    """YOU pick (2026-10-18: the judge scored a script that made no sense 33 of 35, so
    Ryan reads the drafts himself). The game, then `n` drafts, each cut to length when it
    runs long; no judge, no punch-up. -> (game sentence, [{title, hook, lines}]). Raises when
    no draft can be made."""
    say = say or (lambda m: None)
    log = log or (lambda m: None)
    game = None                     # no separate game step: each draft plans its own
    out, seen_, jokes = [], set(), []
    names = set(cast)
    for k in range(max(1, n)):
        say("writing draft %d of %d" % (k + 1, n))
        try:
            d = draft(director, brief, cast, seconds, game, rhythm=rhythm, avoid=jokes)
        except Exception as exc:
            log("draft %d failed: %s" % (k + 1, exc))
            continue
        jokes = jokes + [d.get("joke") or ""]
        if words_in(d["lines"]) > budget(seconds, len(cast), rhythm) * CUT_OVER:
            try:
                d = cut(director, d, cast, seconds, rhythm=rhythm)
            except Exception as exc:
                log("couldn't shorten draft %d (%s)" % (k + 1, exc))
        lines = [dict(li, text=strip_speaker(li.get("text"), names)) for li in d["lines"]]
        key = tuple((li.get("speaker"), li.get("text")) for li in lines)
        if key in seen_:
            continue
        seen_.add(key)
        out.append({"title": d.get("title") or "Untitled", "hook": d.get("hook") or "",
                    "plan": plan_text(d), "joke": d.get("joke") or "", "lines": lines})
        log("draft %d plan: %s" % (k + 1, plan_text(d) or "(none)"))
    if not out:
        raise ValueError("no usable draft")
    return (out[0].get("plan") or ""), out


def write(director, brief, cast, seconds, idea="", shape="", format_name="", drafts=3,
          say=None, log=None, rhythm=None):
    """The whole room -> {title, lines, room:{game, scores, punch}}. Raises only when no
    draft at all can be made (the caller then uses the old one-shot writer)."""
    say = say or (lambda m: None)
    log = log or (lambda m: None)
    room = {}
    game = None                     # no separate game step: each draft plans its own
    ds = []
    for k in range(max(1, drafts)):
        say("writing draft %d of %d" % (k + 1, drafts))
        try:
            ds.append(draft(director, brief, cast, seconds, game, rhythm=rhythm,
                            avoid=[x.get("joke") for x in ds]))
        except Exception as exc:
            log("draft %d failed: %s" % (k + 1, exc))
    if not ds:
        raise ValueError("no usable draft")
    best, scores = ds[0], []
    if len(ds) > 1:
        say("scoring the drafts")
        ranked = []
        for i, d in enumerate(ds):
            try:
                t, sc = score(director, d, idea, format_name)
            except Exception as exc:
                t, sc = -99, {"error": str(exc)}
            try:                       # a viewer who can't follow it won't laugh at it
                cr = cold_read(director, d)
                d["_cold"] = cr
                if isinstance(sc, dict) and t > -99:
                    pen = 2 * (5 - cr["makes_sense"]) + len(cr["confusing"])
                    if pen:
                        t -= pen
                        sc["penalty_unclear"] = -pen
                    sc["makes_sense"] = cr["makes_sense"]
            except Exception as exc:
                log("no cold read for draft %d (%s)" % (i + 1, exc))
            scores.append({"draft": i + 1, "total": t, "scores": sc})
            ranked.append((t, sc.get("button", 0) if isinstance(sc, dict) else 0,
                           sc.get("game", 0) if isinstance(sc, dict) else 0, -i, d))
        ranked.sort(key=lambda r: r[:4], reverse=True)
        best = ranked[0][4]
        log("drafts scored %s - kept draft %d"
            % ([s["total"] for s in scores], -ranked[0][3] + 1))
    room["scores"] = scores
    say("punching up the last line")
    try:
        best, new_btn = punch_up(director, best)
        if new_btn:
            room["punch"] = new_btn
            log("new button: %s" % new_btn)
    except Exception as exc:
        log("no punch-up (%s)" % exc)
    if words_in(best["lines"]) > budget(seconds, len(cast), rhythm) * CUT_OVER:
        say("tightening it to the length")
        try:
            best = cut(director, best, cast, seconds, rhythm=rhythm)
        except Exception as exc:
            log("the cut failed (%s) - kept the long draft" % exc)
    # COLD READ the scene that will be shot; rewrite the lines a viewer wouldn't follow
    say("checking a viewer can follow it")
    try:
        cr = best.get("_cold")
        if not cr or cr.get("_lines") != [li.get("text") for li in best["lines"]]:
            cr = cold_read(director, best)
        room["cold"] = {"makes_sense": cr["makes_sense"], "what": cr["what"],
                        "confusing": cr["confusing"]}
        log("cold read: %d/5%s" % (cr["makes_sense"], "".join(
            " | line %d: %s" % (k, w) for k, w in cr["confusing"])))
        if not clear_enough(cr):
            say("making it make sense")
            new = clarify(director, best, cast, cr, plan_text(best))
            if new is not best:
                cr2 = cold_read(director, new)
                log("after the rewrite: %d/5%s" % (cr2["makes_sense"], "".join(
                    " | line %d: %s" % (k, w) for k, w in cr2["confusing"])))
                if (cr2["makes_sense"], -len(cr2["confusing"])) > \
                        (cr["makes_sense"], -len(cr["confusing"])):
                    best = new
                    room["cold"] = {"makes_sense": cr2["makes_sense"], "what": cr2["what"],
                                    "confusing": cr2["confusing"], "rewrote": True}
    except Exception as exc:
        log("no cold read (%s)" % exc)
    if plan_text(best):
        room["plan"] = plan_text(best)
    return {"title": best.get("title") or "Untitled", "lines": best["lines"], "room": room}
