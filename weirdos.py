"""WEIRDO — invent a whole funny character in one press (2026-10-17).

Ryan: "character feels very limited with the surprise me only including the drop downs. maybe
there should be another button for weirdos specifically where the prompt would focus on the AI
character that's a little weird like jeanphil" — then: "I dont want it only to be european
style. should include everything. i just want it to be funny / ironic / ridiculous / potential
to go viral".

Surprise me rolls from the editor's dropdown vocabulary (normal in every axis, one trait). A
WEIRDO is a FORMAT, not a roll: the shape Gustav / Augustin / Ludvig were built in
(tools/format_char.py), opened up to any world —

    WHO        an ordinary-sounding adult (a dentist, a mall Santa, a medieval tax collector)
    THE GAG    ONE ridiculous physical thing, rendered photoreal (it must read in a thumbnail)
    THE LOOK   wardrobe + a setting that makes the gag funnier
    THE BIT    a signature action that loops in 5 s, performed completely straight
    THE LINE   a quotable catchphrase
    THE HOOK   one sentence: why someone would share it
    BIBLE      who they are + the voice (Dialogue designs the voice from it)

The rig's writer invents it (a random flavour + two random "sparks" go in, so two presses
never come back alike). When the writer is unreachable the built-in pools roll one, and the
window SAYS so. compose() writes the still prompt with the exact skeleton the format
characters used (full length, flat deadpan expression, documentary-real finish) — the
"slightly-off AI photo" look is the joke's delivery, so it stays.

Tk-free, stdlib. The Director is passed in (tests pass a fake); nothing here imports forge_lab.
"""
import random
import re

FLAVOURS = [
    ("Anything", "any world, any era, any job — surprise me"),
    ("Jean-Phil (deadpan European dignity)",
     "formal old-world European dress, a grand square, total dignity, one silent repeated "
     "routine performed for nobody"),
    ("Corporate absurdity", "an office worker, manager or salesperson treating something "
                            "ridiculous as completely routine business"),
    ("Suburban dad energy", "a dad, coach or HOA member in the suburbs, utterly sincere"),
    ("Wellness influencer", "a lifestyle / wellness / fitness influencer, serene and "
                            "self-serious"),
    ("Out of time", "someone from another era (medieval, Victorian, 1970s) behaving as if "
                    "nothing is unusual in a modern place"),
    ("Small-town official", "a mayor, crossing guard, DMV clerk or park ranger with enormous "
                            "pride in a tiny job"),
    ("Retired legend", "a retired athlete, action hero, magician or rock star still doing the "
                       "act for nobody"),
    ("Nature documentary", "a person filmed like wildlife: a ranger or naturalist presenting "
                           "themselves as the specimen"),
    ("Kids' TV host", "an over-cheerful children's TV presenter (an adult) with something "
                      "deeply off about them"),
    ("Gym philosopher", "a bodybuilder or gym regular who speaks only in grave life lessons"),
    ("Old money", "an aristocrat or heiress, effortlessly superior, in a place far beneath "
                  "them"),
    ("Cryptid with a day job", "a person who looks almost like a folklore creature, holding "
                               "down a completely normal job"),
    ("Local news", "a weather presenter, field reporter or anchor committing too hard"),
    ("Holiday seasonal", "a mall Santa, Easter bunny performer or haunted-house actor, "
                         "off-season"),
    ("Uncanny AI", "an AI-generated-looking person whose ONE wrong detail is the kind AI "
                   "photos get wrong, played completely straight"),
]

# ---------------------------------------------------------------- offline pools (adults only)
SUBJECTS = [             # (who, pronoun, what they wear) — the outfit belongs to the job
    ("a middle-aged dentist", "he", "pale blue scrubs, a head mirror and clogs"),
    ("a retired lifeguard in his seventies", "he",
     "a sun-faded lifeguard tank top, red shorts and a whistle"),
    ("a wellness influencer in her thirties", "she",
     "matching beige athleisure, holding a giant water bottle"),
    ("a suburban dad", "he", "a polo shirt tucked into khaki shorts, white sneakers and a visor"),
    ("a medieval tax collector", "he", "a wool tunic, a belt pouch and leather boots"),
    ("a corporate middle manager", "she", "a grey pantsuit and a lanyard full of badges"),
    ("a grandmother who bowls competitively", "she",
     "a pastel bowling shirt with her name stitched on it"),
    ("a cruise-ship magician", "he", "a sequined burgundy tuxedo jacket and a top hat"),
    ("a local TV weatherman", "he", "a grey suit, holding a remote clicker"),
    ("a park ranger", "she", "a ranger's uniform with a wide-brimmed hat"),
    ("a mall Santa, off-season", "he", "a velvet Santa suit worn with sandals"),
    ("a Victorian governess", "she", "a high-necked black dress with a cameo brooch"),
    ("a competitive bodybuilder", "he", "a tank top, a weightlifting belt and wrist wraps"),
    ("a school crossing guard", "she", "a hi-vis vest over a cardigan, holding a stop sign"),
    ("a fashion model", "she", "an avant-garde runway gown made of crinkled foil"),
    ("a mailman", "he", "a postal uniform with a satchel full of letters"),
    ("a real-estate agent", "she", "a power suit with huge 80s shoulder pads"),
    ("a lounge singer", "he", "a ruffled shirt open at the collar and a velvet blazer"),
    ("a minor-league baseball umpire", "he", "an umpire's black uniform and chest protector"),
]
FEATURES = [
    "a neck as long as a giraffe's, rising a full metre above the shoulders",
    "tiny T-rex arms that barely reach past the ribs",
    "a single enormous unbroken eyebrow that spans the whole forehead like a hedge",
    "hands the size of dinner plates",
    "a head that is almost perfectly cube-shaped",
    "eyes set impossibly far apart, nearly at the temples",
    "a smile so wide it reaches from ear to ear, showing every tooth",
    "legs twice the normal length, so the torso sits up near a doorframe",
    "a nose like a ski jump, swooping out and up",
    "a forehead as tall as the rest of the face combined",
    "front teeth like a row of oversized piano keys",
    "a chin like the blade of a shovel",
    "a head far too small for the body, like a tennis ball on a wardrobe",
    "shoulders over a metre wide, like a coat hanger",
    "an enormous wild mane of hair that stands straight up as if in a gale",
    "ears so big they fold over like a basset hound's",
    "a moustache so long its ends trail on the ground",
]
WARDROBES = [           # sparks for the writer (the offline roll uses each subject's own)
    "a short-sleeved dress shirt, a clip-on tie and a lanyard",
    "a sun-faded lifeguard tank top, red shorts and a whistle",
    "matching beige athleisure and a giant water bottle",
    "a polo shirt tucked into khaki shorts, white sneakers and a visor",
    "a black tailcoat, white bow tie and white gloves",
    "a sequined burgundy tuxedo jacket",
    "a hi-vis vest over a cardigan, holding a stop sign",
    "a ranger's uniform with a wide-brimmed hat",
    "a velvet Santa suit, off-season, with sandals",
    "a power suit with huge 80s shoulder pads",
    "a weatherman's grey suit and a remote clicker",
    "a medieval tunic, belt pouch and leather boots",
]
SETTINGS = [
    "an empty strip-mall parking lot at noon",
    "a beige office cubicle under fluorescent lights",
    "a DMV waiting room with plastic chairs",
    "a Florida motel swimming pool",
    "a suburban cul-de-sac with identical houses",
    "a local TV news studio in front of a green screen",
    "a quiet airport gate at dawn",
    "a bowling alley with neon lanes",
    "a grand baroque square in Vienna with an ornate fountain",
    "a misty pine forest trailhead",
    "an empty shopping-mall food court",
    "a sun-bleached beach boardwalk",
]
ACTIONS = [
    "points at the camera, holds it, and slowly lowers the finger",
    "does one tiny celebratory fist pump, then returns to total stillness",
    "takes a long sip from a mug while maintaining unbroken eye contact",
    "presents an empty space with both hands like a game-show prize",
    "nods slowly in agreement with something nobody said",
    "checks a wristwatch, sighs, and checks it again",
    "gives a thumbs-up that lasts far too long",
    "turns slowly in a full circle, then faces the camera again",
    "salutes and holds it a beat too long",
    "conducts an invisible orchestra with total concentration",
]
LINES = [
    "This is fine. This is normal.", "We don't talk about it.", "Again. From the top.",
    "I've seen worse. I haven't, but I've heard of worse.", "That's above my pay grade.",
    "Stay hydrated.", "Rules are rules.", "No further questions.",
    "I'm not mad. I'm documenting.", "Let's circle back.",
]

FRAMING = "full body shot, the whole figure head to feet in frame"
FINISH = ("Natural light, soft shadows. Documentary realism, visible skin pores and fabric "
          "texture, fine 35mm film grain, matte surfaces, natural colour, no cosmetic "
          "retouching, no glow, not CGI, not illustration.")
_CHILD = re.compile(r"\b(child|children|kid|kids|boy|girl|teen|teenager|teenage|baby|toddler|"
                    r"minor(?![- ]league)|schoolgirl|schoolboy|underage|young (boy|girl))\b", re.I)

SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "string"} for k in (
        "name", "subject", "pronoun", "look", "feature", "wardrobe", "setting", "action",
        "catchphrase", "hook", "who", "voice", "pov", "want", "blind_spot", "habit")},
    "required": ["name", "subject", "pronoun", "look", "feature", "wardrobe", "setting",
                 "action", "catchphrase", "hook", "who", "voice", "pov", "want",
                 "blind_spot", "habit"],
}

SYSTEM = (
    "You invent original comedic characters for a short-form cartoon / AI-video series. Each "
    "one is a FORMAT that could go viral: an instantly readable visual gag, a bit that loops in "
    "five seconds, a quotable line, all played completely straight. You write production "
    "notes, not jokes: concrete, visual, specific. Every character is an invented ADULT — "
    "never a real or famous person, never a child, nothing sexual, no slurs, no real brands.")


def _pron(p):
    p = (p or "").strip().lower()
    return p if p in ("he", "she", "they") else "they"


def realism(pron):
    """The clause that keeps an exaggerated body part reading as REAL tissue (the format
    ledger's most-repeated failure is a smooth, liquified-looking gag)."""
    pos = {"he": "his", "she": "her", "they": "their"}[_pron(pron)]
    return ("yet it reads as real human tissue, with the same visible pores, fine wrinkles and "
            "natural texture as the rest of %s body — nothing smoothed, stretched or airbrushed"
            % pos)



def _gag_noun(feat):
    """'ears so large they drag below his chin' -> 'ears' — the thing the gag IS."""
    f = (feat or "").strip()
    for cut in (" so ", " that ", " which ", " like ", " with ", ","):
        j = f.find(cut)
        if j > 0:
            f = f[:j]
    f = re.sub(r"^(an?|the|his|her|their)\s+", "", f.strip(), flags=re.I)
    return f or "feature"

def compose(spec):
    """The full still prompt — the format skeleton (full length, deadpan, documentary real).
    No gag -> an ordinary person (compose_person)."""
    if not (spec.get("feature") or "").strip():
        return compose_person(spec)
    pr = _pron(spec.get("pronoun"))
    subj, verb_be, verb_wear, verb_stand = {
        "he": ("He", "is", "wears", "stands"), "she": ("She", "is", "wears", "stands"),
        "they": ("They", "are", "wear", "stand")}[pr]
    pos = {"he": "his", "she": "her", "they": "their"}[pr]
    feat = (spec.get("feature") or "").strip().rstrip(".")
    look = (spec.get("look") or "").strip().rstrip(".")
    return ("A full-length photograph of an invented person, %s. %s %s %s%s. %s %s %s. %s, %s. "
            "The %s: exaggerated far past normal, the first thing anyone notices in the "
            "picture, impossible to miss even as a tiny thumbnail. "
            "%s expression is flat and completely serious — the joke is never acknowledged. "
            "Standing straight and facing the camera. %s %s in %s. %s"
            % (FRAMING, subj, verb_be, spec.get("subject", "a person"),
               (", " + look) if look else "", subj, verb_wear,
               spec.get("wardrobe", "ordinary clothes"), feat[:1].upper() + feat[1:],
               realism(pr), _gag_noun(feat), pos.capitalize(), subj, verb_stand,
               spec.get("setting", "an ordinary place"), FINISH))


# ------------------------------------------------------------- ordinary PEOPLE (Quick's default)
# Ryan, 2026-10-17: "i dont think all the characters in the quick mode need to be weirdos. if i
# use the prompt one old man talking to a middle aged woman on a city street then that doesnt
# mean they should be weirdos, only if specified or generated using the weirdo button".
# A PERSON is the same record minus the gag: the comedy comes from who they are (pov / want /
# blind_spot / habit), not from their body.
PERSON_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "string"} for k in (
        "name", "subject", "pronoun", "look", "wardrobe", "setting", "action", "who", "voice",
        "pov", "want", "blind_spot", "habit")},
    "required": ["name", "subject", "pronoun", "look", "wardrobe", "setting", "action",
                 "who", "voice", "pov", "want", "blind_spot", "habit"],
}

PERSON_SYSTEM = (
    "You cast real-looking, ordinary people for a short comedic video. They look like anyone "
    "you'd pass on the street - normal proportions, nothing exaggerated, no gimmick in how "
    "they look. What makes them funny is who they are: a strong point of view, a want and a "
    "blind spot. Every character is an invented ADULT - never a real or famous person, never "
    "a child, nothing sexual, no slurs, no real brands.")


def compose_person(spec):
    """The still prompt for an ORDINARY person: real proportions, an expression that fits
    them (not the weirdo's fixed deadpan)."""
    pr = _pron(spec.get("pronoun"))
    subj, verb_be, verb_wear, verb_stand = {
        "he": ("He", "is", "wears", "stands"), "she": ("She", "is", "wears", "stands"),
        "they": ("They", "are", "wear", "stand")}[pr]
    pos = {"he": "his", "she": "her", "they": "their"}[pr]
    look = (spec.get("look") or "").strip().rstrip(".")
    return ("A full-length photograph of an invented, ordinary-looking person, %s. %s %s %s%s. "
            "%s %s %s. Normal human proportions, nothing exaggerated or stylised - someone you "
            "would pass on the street. %s expression is natural and fits who %s %s. %s %s in "
            "%s, facing the camera. %s"
            % (FRAMING, subj, verb_be, spec.get("subject", "a person"),
               (", " + look) if look else "", subj, verb_wear,
               spec.get("wardrobe", "ordinary clothes"), pos.capitalize(), subj.lower(),
               verb_be, subj, verb_stand, spec.get("setting", "an ordinary place"), FINISH))


def roll_person(rng=None):
    """The OFFLINE person (the writer is unreachable): an ordinary adult from the pools."""
    r = rng or random
    subj, pron, wear = r.choice(SUBJECTS)
    spec = roll(r)
    spec.update({"subject": subj, "pronoun": pron, "wardrobe": wear, "feature": "",
                 "catchphrase": "", "hook": "", "flavour": "person",
                 "action": "talks with an easy, natural gesture",
                 "voice": "a natural, everyday speaking voice",
                 "who": "%s with strong opinions about ordinary things."
                        % (subj[0].upper() + subj[1:])})
    spec["name"] = r.choice({"he": ["Walt", "Frank", "Dev", "Marcus", "Ray"],
                             "she": ["Linda", "Rosa", "Meg", "Priya", "Joan"]}.get(
                                 pron, ["Sam", "Robin"]))
    return spec


def invent_person(director, brief="", avoid=(), model=None):
    """The writer casts one ordinary person. `avoid` = names already in the cast."""
    user = (
        "Cast ONE person.\n\n"
        "WHAT RYAN TYPED (honour every concrete thing in it - age, sex, job, place): %s\n"
        "ALREADY IN THE CAST (do NOT repeat these names): %s\n\n"
        "RULES:\n"
        "- name: a first name (or first + last) that sounds right for them.\n"
        "- subject: WHO, a short noun phrase starting with a/an, taken from the role if one "
        "is given (\"an old man\", \"a middle-aged woman\").\n"
        "- pronoun: he, she or they.\n"
        "- look: age, hair, skin, build in one short phrase - ordinary, realistic.\n"
        "- wardrobe: everyday clothes that fit who they are, no word 'wearing'. setting: the "
        "place from the idea if it names one, else one concrete ordinary place.\n"
        "- action: a natural 5-second gesture they'd make while talking, verb first, no "
        "subject, no his/her.\n"
        "- who: two sentences of character bible - what they do all day and how they behave "
        "under pressure. Not the same worldview as anyone ALREADY IN THE CAST. voice: how "
        "they sound when they talk (for voice design), one sentence.\n"
        "- pov: ONE sentence starting 'Believes' - the strong, slightly wrong way they see "
        "the world, which they defend in any argument.\n"
        "- want: what they want from the other person in this video, five to ten words.\n"
        "- blind_spot: the one thing about themselves they cannot see.\n"
        "- habit: one verbal habit that shows the POV in how they talk.\n"
        "Return the JSON object only."
        % ((brief or "").strip() or "(nothing)",
           "; ".join(a for a in avoid if a)[:600] or "(nobody yet)"))
    out = director._chat(PERSON_SYSTEM, user, PERSON_SCHEMA, temperature=0.9, model=model)
    out = dict(out or {}, feature="x", catchphrase="", hook="")
    spec = _clean(out, "person")
    spec["feature"] = ""                              # a person has no gag
    return spec


def _clean(spec, flavour):
    out = {k: re.sub(r"\s+", " ", str(spec.get(k) or "")).strip() for k in SCHEMA["required"]}
    out["pronoun"] = _pron(out["pronoun"])
    out["wardrobe"] = re.sub(r"^(is\s+|are\s+)?wear(ing|s)?\s+", "", out["wardrobe"], flags=re.I)
    out["setting"] = re.sub(r"^(in|at)\s+", "", out["setting"], flags=re.I)
    # the action loops on a 5 s clip and must fit any body: verb first, no gendered pronoun
    out["action"] = re.sub(r"\b(his|her)\b", "their", out["action"], flags=re.I)
    out["action"] = re.sub(r"^(he|she|they)\s+", "", out["action"], flags=re.I)
    out["catchphrase"] = out["catchphrase"].strip().strip('"“”')
    out["flavour"] = flavour
    for k in ("subject", "feature", "wardrobe", "setting", "action"):
        if not out[k]:
            raise ValueError("the writer left %r empty" % k)
    blob = " ".join(out[k] for k in ("subject", "look", "who"))
    if _CHILD.search(blob):
        raise ValueError("the writer described a minor — characters are adults only")
    if not out["name"]:
        out["name"] = out["subject"].split()[-1].capitalize()
    return out


def roll(rng=None, flavour="Anything"):
    """The OFFLINE weirdo: one from each pool. Used when the writer can't be reached."""
    r = rng or random
    subj, pron, wear = r.choice(SUBJECTS)
    return _clean({
        "name": r.choice({"he": ["Dale", "Gustav", "Terry", "Duncan", "Rolf", "Clive",
                                 "Barry", "Lionel"],
                          "she": ["Brenda", "Marlene", "Ingrid", "Pam", "Shirley", "Delphine",
                                  "Gail", "Yvonne"]}.get(pron, ["Sam", "Robin"])),
        "subject": subj, "pronoun": pron, "look": "", "feature": r.choice(FEATURES),
        "wardrobe": wear, "setting": r.choice(SETTINGS),
        "action": r.choice(ACTIONS), "catchphrase": r.choice(LINES),
        "hook": "the gag reads in a thumbnail, the bit loops, the line is quotable",
        "who": "%s who has never once acknowledged %s own %s." % (
            subj[0].upper() + subj[1:], {"he": "his", "she": "her"}.get(pron, "their"),
            "situation"),
        "voice": "a flat, dry, completely sincere speaking voice"}, flavour)


def invent(director, brief="", flavour="Anything", avoid=(), rng=None, model=None):
    """The writer invents one. `avoid` = gags/names already in the cast. Raises on failure
    (the window then rolls from the pools and SAYS so)."""
    r = rng or random
    fl_desc = dict(FLAVOURS).get(flavour) or dict(FLAVOURS)["Anything"]
    if flavour == "Anything" and "THEIR ROLE" in (brief or ""):
        # a role from the idea: no random flavour on top ("Local news" turned the lighthouse
        # keeper into a fog forecast analyst)
        fl_desc = dict(FLAVOURS)["Anything"]
    elif flavour == "Anything":
        flavour_pick = r.choice([f for f, _d in FLAVOURS[1:]])
        fl_desc = "%s (%s)" % (flavour_pick, dict(FLAVOURS)[flavour_pick])
    sparks = r.sample(FEATURES, 2) + [r.choice(SETTINGS), r.choice(SUBJECTS)[0]]
    user = (
        "Invent ONE character.\n\n"
        "FLAVOUR: %s\n"
        "WHAT RYAN TYPED (honour every concrete thing in it; may be empty): %s\n"
        "SPARKS (loose inspiration only — ignore if they don't fit): %s\n"
        "ALREADY IN THE CAST (do NOT repeat these gags or names): %s\n\n"
        "RULES:\n"
        "- name: a first name (or first + last) that sounds right for them.\n"
        "- subject: WHO, an ordinary-sounding ADULT role, a short noun phrase starting with "
        "a/an (\"a mall Santa\", \"a medieval tax collector\"). The gag is NOT in it.\n"
        "- pronoun: he, she or they.\n"
        "- look: hair / skin / age details in one short phrase, ordinary.\n"
        "- feature: EXACTLY ONE ridiculous physical thing about their body, specific and "
        "visible at full length in a thumbnail. Never two. Not a costume or prop.\n"
        "- wardrobe: clothes only, no word 'wearing'. setting: one concrete place that makes "
        "the gag funnier.\n"
        "- action: a 5-second signature bit that LOOPS, verb first, no subject, no his/her, "
        "deadpan and specific.\n"
        "- catchphrase: one short quotable line they'd say, totally sincere.\n"
        "- hook: one sentence on why someone would share it.\n"
        "- who: two sentences of character bible, consistent with pov and blind_spot - what "
        "they do all day and how they behave under pressure. Do NOT give them the same "
        "worldview as anyone ALREADY IN THE CAST. voice: how they sound when they talk (for "
        "voice design), one sentence.\n"
        "- pov: ONE sentence starting 'Believes' - the strong, slightly wrong way they see the "
        "world, which they will defend in any argument (\"Believes every problem is secretly "
        "their fault and can be fixed by apologising.\"). It must come from THEIR ROLE's "
        "adjective if one is given (nervous, overconfident...).\n"
        "- want: what they want from the other person in this video, in five to ten words.\n"
        "- blind_spot: the one thing about themselves they cannot see, which the comedy will "
        "expose.\n"
        "- habit: one verbal habit that shows the POV in how they talk (\"apologises before "
        "every sentence\", \"calls every failure a strategy\").\n"
        "Return the JSON object only."
        % (fl_desc, (brief or "").strip() or "(nothing)", "; ".join(sparks),
           "; ".join(a for a in avoid if a)[:900] or "(nobody yet)"))
    out = director._chat(SYSTEM, user, SCHEMA, temperature=1.0, model=model)
    return _clean(out, flavour)
