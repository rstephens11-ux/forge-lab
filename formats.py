"""FORMATS — one-click shapes for Quick mode (2026-10-17).

A format is what Higgsfield calls an "app" and what a sketch show calls a recurring bit: the
cast size, where it happens, how the writer should shape the joke, how long, which way up.
Quick mode hands `brief()` to the writer and `where` to the set. Every value is a default —
the Quick window and the Studio windows can change any of it afterwards.
"""

FORMATS = [
    {"key": "sketch", "pace": "snappy", "name": "Two-hander sketch", "cast": 2, "seconds": 30,
     "aspect": "9:16",          # phones first (review panel): wide is one click away
     "blurb": "two characters, one absurd argument",
     "where": "",
     "shape": "A two-person comedy sketch. Start in the middle of an argument about something "
              "small; each line escalates; the last line is a button that flips it."},
    {"key": "weather", "pace": "natural", "name": "Weather report", "cast": 1, "seconds": 20, "aspect": "9:16",
     "blurb": "a local forecast, played completely straight",
     "where": "a small local TV news studio with a green screen weather map",
     "shape": "A local TV weather forecast, delivered to camera, completely sincere. The "
              "forecast is about something absurd and gets more specific each line. End on "
              "a sign-off catchphrase."},
    {"key": "interview", "pace": "natural", "name": "Street interview", "cast": 2, "seconds": 25,
     "aspect": "9:16",
     "blurb": "a reporter, a passer-by, one strange answer",
     "where": "a busy city sidewalk on a grey afternoon",
     "shape": "A vox-pop street interview. The interviewer asks simple questions; the "
              "passer-by answers with total confidence and gets one strange detail more "
              "specific each time. Short lines."},
    {"key": "podcast", "pace": "natural", "name": "Podcast clip", "cast": 2, "seconds": 30, "aspect": "9:16",
     "blurb": "two hosts at microphones, one hot take",
     "where": "a cramped home podcast studio, two microphones on boom arms, foam panels",
     "shape": "A clipped moment from a podcast. One host states an outrageous opinion as "
              "settled fact; the other slowly comes round to agreeing. Natural, overlapping "
              "rhythm, no stage directions in the words."},
    {"key": "vlog", "pace": "snappy", "name": "Vlog / talking head", "cast": 1, "seconds": 20, "aspect": "9:16",
     "blurb": "one character, straight to camera",
     "where": "",
     "shape": "A selfie-camera vlog, straight to the lens. Open with a hook in the first line; "
              "explain one ridiculous personal routine as if it were life advice."},
    {"key": "ad", "pace": "snappy", "name": "Product ad", "cast": 1, "seconds": 20, "aspect": "9:16",
     "blurb": "an earnest pitch for an invented product",
     "where": "a bright, clean kitchen-showroom set",
     "shape": "A sincere direct-response TV ad for an INVENTED product (never a real brand). "
              "Problem, product, one absurd benefit, a too-specific guarantee, call to "
              "action."},
    {"key": "music", "pace": "full", "name": "Music video", "kind": "music", "cast": 1, "seconds": 40,
     "aspect": "9:16", "sound": "Alt-girl future R&B",
     "blurb": "your character sings an original song",
     "where": "a dark stage with haze, coloured spotlights and a glossy floor",
     "shape": "An original song for a music video, sung by the character in their own "
              "persona. Sincere, catchy, a chorus people would sing back; the character's "
              "one absurd thing is the hook of the chorus."},
    {"key": "confessional", "pace": "breathe", "name": "Reality confessional", "cast": 1, "seconds": 20,
     "aspect": "9:16",
     "blurb": "the reality-TV interview chair",
     "where": "a reality-TV confessional booth, a single chair, moody coloured backlight",
     "shape": "A reality-TV confessional to camera. They describe a petty grievance with "
              "enormous emotion, as if it were a betrayal. End on a dramatic pause line."},
    # three more from the review panel (2026-10-17)
    {"key": "doorbell", "pace": "natural", "name": "Doorbell camera", "cast": 1,
     "seconds": 20, "aspect": "9:16", "look": "cctv", "stamp": "FRONT DOOR  \u25cf REC",
     "blurb": "caught on the porch camera at 2 am",
     "where": "a suburban front porch at night seen from a doorbell camera mounted by the "
              "door, high angle, wide fisheye, porch light on",
     "shape": "Footage from a doorbell camera."},
    {"key": "nature", "pace": "breathe", "name": "Nature documentary", "cast": 2,
     "seconds": 30, "aspect": "9:16",
     "blurb": "a whispering narrator observes a human",
     "where": "",
     "shape": "A nature documentary."},
    {"key": "ifvlog", "pace": "snappy", "name": "If ___ had a vlog", "cast": 1,
     "seconds": 20, "aspect": "9:16",
     "blurb": "the Grim Reaper, a lighthouse, a medieval king... vlogs",
     "where": "",
     "shape": "If something had a vlog."},
    # GILDED SATIRE (2026-10-20): a photoreal costume-drama satire about AI, played straight.
    # Many SHORT lines (one every ~3 s, not the usual ten), small gold subtitles, a fade out.
    {"key": "gilded", "pace": "natural", "name": "Gilded satire", "cast": 3,
     "seconds": 120, "aspect": "9:16",
     "blurb": "a grotesque lord explains why you should be grateful",
     "where": "a decaying gilded banquet hall bolted over with brass pipes and cables, a "
              "high table piled with roast fowl, rust-streaked porcelain robot guards along "
              "the walls, smog light through grimy windows, petitioners crowded below",
     "lines": (3.0, 45),            # (seconds per line, most lines) - see rhythm()
     "captions": "subtitle", "end_fade": 1.8,
     "shape": "A gilded satire."},
]


# Each shape names the game the format usually plays, who is the straight one and what the
# button looks like (review panel's comedy writer, 2026-10-17). THE IDEA can override them.
_SHAPES = {
    "sketch": (
        "A two-person comedy sketch. One character is the ODD one (they believe something "
        "absurd, or want something unreasonable); the other is the STRAIGHT one, who reacts "
        "like a real person and keeps trying to fix it. Open mid-argument with the problem "
        "stated in line one. The odd one's logic never breaks - each reply pushes it one "
        "step further. The button is either the straight one finally giving in, or a reveal "
        "that the odd one was right in a way nobody expected."),
    "weather": (
        "A local TV weather forecast, delivered to camera, completely sincere. Line one names "
        "the town and the absurd weather as if it were normal ('Expect light pigeons over "
        "Rockland until noon'). Treat the absurd thing with real forecast language - "
        "percentages, fronts, wind direction, advice for commuters - and get more specific "
        "and more personal each line, until the forecast is clearly about the presenter's own "
        "life. End on a sign-off catchphrase that now sounds different because of what came "
        "before."),
    "interview": (
        "A vox-pop street interview. The interviewer asks short, normal questions and stays "
        "professional. The passer-by answers with total confidence and reveals ONE strange "
        "fact about themselves, a little at a time - each answer adds one more specific "
        "detail that makes it worse. Line one is the interviewer's question; line two is "
        "already strange. The button is the interviewer's final question being answered with "
        "the most specific, damning detail of all - or the interviewer quietly revealing "
        "they share it."),
    "podcast": (
        "A clipped moment from a podcast, as if cut for social media. Line one is the hot "
        "take, stated as settled fact, with a number or a name in it. The second host pushes "
        "back with common sense; the first answers every objection with an 'obvious' fact "
        "that is completely made up but very specific. The second host slowly comes round, "
        "until they are more committed than the first. The button: the convert takes it one "
        "step too far and the original host is alarmed."),
    "vlog": (
        "A selfie-camera vlog, straight to the lens. Line one is a hook a stranger would stop "
        "scrolling for ('I haven't used a door in four years. Here's why.'). Then explain one "
        "ridiculous personal routine as if it were life advice: the rule, the reason, the "
        "results, each step more specific and more revealing about how badly it is going. "
        "The button is a cheerful detail that shows the cost - said as if it were a win."),
    "ad": (
        "A sincere direct-response TV ad for an INVENTED product (never a real brand). Line "
        "one is the problem, stated as a crisis ('Tired of your soup looking at you?'). Then "
        "the product's made-up name, how it works (one absurd mechanism, explained "
        "confidently), a testimonial-style benefit that is far too specific, and a guarantee "
        "with a strange condition. The button is the small-print line said fast, which "
        "quietly reveals something alarming."),
    "confessional": (
        "A reality-TV confessional to camera. Line one names the betrayal in one concrete "
        "sentence ('She used MY spoon. At brunch. In front of Carl.'). They describe a petty "
        "grievance with the emotion of a war crime, each line adding a detail that makes it "
        "pettier while their reaction gets bigger. The button is a short, quiet, dramatic "
        "line - a threat, a vow or a reveal that they did something far worse themselves."),
    "doorbell": (
        "Footage from a doorbell / security camera. One person on the porch talks straight "
        "up into the camera, as if the homeowner were watching live - lowered voice, "
        "leaning in. Line one says exactly why they are there at this hour, and it is "
        "already strange. Each line adds a detail that makes their reason more specific and "
        "less innocent, while they stay polite and practical. The button is them noticing "
        "something about the house (or the camera) that changes everything - said calmly."),
    "nature": (
        "A nature documentary. The first character is the NARRATOR, a hushed, reverent "
        "wildlife presenter crouched just off the path, speaking to the camera in a whisper "
        "about the second character as if they were a rare animal in their natural habitat. "
        "The second character is an ordinary person doing something ordinary (making toast, "
        "parking, a first date) and says only a few short lines. The narrator explains their "
        "behaviour with confident, made-up biology - mating displays, territory, the herd - "
        "each line more specific. The button: the 'animal' notices the narrator and says "
        "something devastatingly normal, and the narrator treats it as a breakthrough."),
    "gilded": (
        "A photoreal costume-drama satire about AI and automation, played completely "
        "straight. Take a REAL, current AI argument (the one in THE IDEA - 'you'll just "
        "reskill', 'AI art has no soul', 'it will create new jobs') and put it in the mouths "
        "of aristocrats and the people their thinking-machines replaced. Three roles: the "
        "LORD (monstrous, comfortable, cheerfully reasonable, speaks in modern tech-optimist "
        "platitudes, never thinks he is the villain); the PETITIONER (polite, hesitant - "
        "'uh', 'sir', 'm'lord', trailing off - and logically right every single time); the "
        "ENABLER (a consort or courtier who suggests something even crueller with total "
        "sweetness). THE LOOP: the petitioner raises the obvious problem; the lord offers "
        "the stock optimistic answer; the petitioner quietly shows that answer has ALSO "
        "been automated, owned or destroyed by the lord. Three to five loops, each tighter. "
        "Open on appetite or luxury (the lord eating). Then the lord SNAPS and the cruelty "
        "comes out. The button is a dark ironic flip delivered with affection or triumph - "
        "usually the enabler's idea, which the lord loves. Many short lines: blunt one- to "
        "four-word payoffs ('Then eat cake.' / 'Bollocks!') after longer setups; hesitations "
        "and interruptions written in. British voices. Nobody winks. "
        "IF THE IDEA ASKS FOR THE DEMO instead of the loop: an INVENTOR shows a powerful, "
        "bored PATRON his new MACHINE. The machine obeys every instruction far too "
        "literally, each disaster bigger than the last, and explains each one with calm, "
        "polite, perfect logic ('You did not say when to stop.'). The inventor keeps "
        "explaining it away in reassuring jargon. The patron is delighted, never horrified, "
        "and the button is her ordering more - or turning the machine on someone."),
    "ifvlog": (
        "An 'if X had a vlog' video. THE IDEA names what is vlogging (a job, a creature, a "
        "historical figure, an object brought to life as a person) - the character IS that "
        "thing, played as a cheerful lifestyle vlogger straight to the selfie camera. Line "
        "one is the vlog intro ('Hey guys, welcome back, it's a big week for death'). They "
        "walk us through their ordinary day as that thing - their routine, a product they "
        "love, a small annoyance - treating the extraordinary parts as mundane and the "
        "mundane parts as huge. The button is a sponsor read or sign-off that reveals the "
        "darkest detail of their job as if it were a perk."),
}
for _f in FORMATS:
    _f["shape"] = _SHAPES.get(_f["key"], _f["shape"])

LENGTHS = (15, 20, 30, 45, 60)
MIN_SECONDS, MAX_SECONDS = 5, 120
ASPECTS = ("9:16", "16:9")


DEFAULT_RHYTHM = (3.8, 10)        # a line every ~3.8 s, ten at most (writers_room)


def rhythm(fmt):
    """(seconds per line, most lines) for a format: most write about ten lines whatever
    the length; a gilded satire lives on many short lines (one every ~3 s, up to 45)."""
    f = get(fmt) if isinstance(fmt, str) else (fmt or {})
    v = f.get("lines")
    if isinstance(v, (list, tuple)) and len(v) == 2 and v[0] > 0 and v[1] >= 2:
        return float(v[0]), int(v[1])
    return DEFAULT_RHYTHM


def kind(fmt):
    """"talk" (a script, spoken lines) or "music" (lyrics, a song, sung shots)."""
    return (get(fmt) if isinstance(fmt, str) else fmt).get("kind", "talk")


def get(key):
    for f in FORMATS:
        if f["key"] == key or f["name"] == key:
            return f
    return FORMATS[0]


def pace(fmt):
    """The format's default PACE (studio_pipeline.PACES): a sketch is snappy, a reality
    confessional lets its awkward pauses breathe, a music video keeps every frame."""
    return get(fmt).get("pace") or "snappy"


# THE IDEA WINS (2026-10-24). The doorbell format's shape says "one person on the porch talks
# up into the camera as if the homeowner were watching"; the idea "a man is locked out because
# his AI doorbell won't recognise him" has the HOMEOWNER arguing with the DOORBELL. The writer
# followed the shape and the idea was lost. A format is how the video looks and feels; who is
# there and what happens is the idea's.
IDEA_FIRST = ("THE IDEA COMES FIRST. The format above is only the usual shape of this kind of "
              "video. When THE IDEA sets up a different situation (who is there, who talks to "
              "whom, why), write THE IDEA's situation and keep only the format's look and tone.")


def brief(fmt, idea):
    """What the writer is asked for: the format's shape + what Ryan typed."""
    f = get(fmt) if isinstance(fmt, str) else fmt
    idea = (idea or "").strip() or "anything that would make people share it"
    return "%s\n\nTHE IDEA: %s\n\n%s\n\n%s" % (f["shape"], idea, IDEA_FIRST, CRAFT)


def writer_brief(fmt, idea):
    """The head of the PLAN-first draft (writers_room.draft): the format's shape, the idea and
    the idea-first rule - no CRAFT block (the plan asks for the same things in plain words; the
    bake-off's winning prompt had none)."""
    f = get(fmt) if isinstance(fmt, str) else fmt
    idea = (idea or "").strip() or "anything that would make people share it"
    return "%s\n\nTHE IDEA: %s\n\n%s" % (f["shape"], idea, IDEA_FIRST)


# how the writer is asked to write — the first real run's script was eight unconnected
# one-liners that barely touched the idea ("The clouds owe me rent.")
CRAFT = (
    "HOW TO WRITE IT (follow in order):\n"
    "1. THE GAME. Before any lines, decide the ONE unusual pattern in this scene - e.g. "
    "'she never admits fault, so he takes the blame for more and more impossible things'. "
    "Every line must play this same game. No second topic, ever.\n"
    "2. LINE ONE IS THE HOOK. A stranger scrolling past must understand WHO, WHERE and WHAT "
    "IS WRONG from line one alone. Start mid-conflict, with a concrete detail (a number, a "
    "name, an object). Never open with a greeting or a vague feeling.\n"
    "3. LISTEN. Each line is a direct reply to the line before it - it uses a word, object "
    "or claim from that line. If you can swap two lines and nothing breaks, rewrite them.\n"
    "4. HEIGHTEN. Each time the game repeats, it gets bigger, more specific or more absurd "
    "than the last time. Three steps up, not sideways.\n"
    "5. BE SPECIFIC. Use the jobs, places and objects in THE IDEA (for a lighthouse: the "
    "lamp, the foghorn, the ships). Real names, exact numbers, odd precise details. "
    "'Kevin, my best bird' beats 'my pigeons'.\n"
    "6. PLAY THE CHARACTERS. Each person argues from their POV and wants something in this "
    "scene. Show their trait in what they DO with words (the nervous one over-apologises; "
    "the confident one reframes every failure as a win) - never just state it.\n"
    "7. THE BUTTON. The last line is the biggest laugh: a reversal, an admission, or a "
    "callback to an earlier detail that now means something new. Short. Nothing after it.\n"
    "8. DEADPAN. Nobody laughs, winks or explains the joke.\n"
    "9. TIMING. Write \"beat\" in a line's direction when a pause AFTER it makes the next "
    "line land. Once or twice at most.\n"
    "LINE LENGTHS: uneven on purpose. A setup line can be up to fourteen words; the line "
    "that pays it off is short (one to six words). The button is under eight words.\n"
    "NEVER: random insults ('you call that a face?'), puns as punchlines, catchphrases "
    "dropped in for no reason, lines that change the subject, a character's absurd body "
    "feature mentioned more than once (mentioning it at all is optional).\n\n"
    "EXAMPLE OF THE PATTERN (different topic - copy the shape, never the words):\n"
    "  game: the warden treats a haunting as a parking violation.\n"
    "  WARDEN: Sir, you've been parked in this bay since eighteen ninety-one.\n"
    "  GHOST: I died here.\n"
    "  WARDEN: It's a two-hour bay.\n"
    "  GHOST: I am bound to this spot for all eternity.\n"
    "  WARDEN: Then you'll want the annual permit.\n"
    "  GHOST: What does that cost?\n"
    "  WARDEN: Forty pounds. Or one soul, if you're paying by card.")
# (first real run: eight unconnected one-liners -> the review panel's comedy writer's rules)
