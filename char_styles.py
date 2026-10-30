"""STYLES — invent a character in the style of a creator Ryan admires (2026-10-18).

Ryan: "can we add a button in the character sheet to generate a character in this style too"
— after a style breakdown of creators Ryan admires (kept in his private project notes).

A STYLE borrows a creator's CRAFT (the kind of faces, wardrobe, world, light and comedy roles)
and never their characters, names or jokes. Each style is one entry in STYLES:

    roles     [(label, what that role is)] — the parts the style's sketches are cast with
    system    the writer's system prompt
    framing   one of the editor's Framing picks
    look      the picture-making sentence (light, lens, palette, finish)
    rules     the style-specific casting rules the writer gets (names, feature, wardrobe, voice)
    names     {pronoun: [names]} and voices (human, machine) for the offline roll
    pools     the OFFLINE roll when the rig's writer can't be reached
    genres    optional: (genre, its light) — one is picked per press and kept on the spec

It returns the SAME record a Weirdo does (weirdos.SCHEMA), so the Characters window lands it,
saves it and voices it exactly like one. To add the next creator: add one STYLES entry.

Tk-free, stdlib. The Director is passed in (tests pass a fake); nothing here imports forge_lab.
"""
import random

import weirdos as _w

# ------------------------------------------------------------------ gilded satire
GILDED_ROLES = [
    ("Any role", ""),
    ("The powerful one",
     "a grotesque, comfortable aristocrat or magnate who owns the machines (a lord, duchess, "
     "industrialist, bishop), bloated or skeletal and dripping with gold. Cheerfully "
     "reasonable, talks in modern tech-optimist platitudes, never thinks they are the villain"),
    ("The underling",
     "a polite, stammering clerk, servant, peasant or petitioner, gaunt, grimy or pitiful. "
     "Hesitant, says 'uh' and 'sir', trails off, and is logically right every single time"),
    ("The sweet enabler",
     "a consort, heir or courtier who suggests something even crueller with total sweetness "
     "and a radiant smile"),
    ("The proud inventor",
     "a jolly, slightly defensive inventor or engineer who explains away every warning sign "
     "in reassuring jargon"),
    ("The machine",
     "a hulking, padded, porcelain-white humanoid machine or robot servant: calm, polite, "
     "clinical, and takes every instruction far too literally. Not sexualised in any way"),
    ("The snob",
     "a pompous critic, connoisseur or courtier who speaks in absurd, self-important jargon"),
]

GILDED_SYSTEM = (
    "You cast ORIGINAL characters for a photoreal, darkly comic satire about AI and "
    "automation, set in a decaying dystopian retro-future where aristocrats live off their "
    "machines. The world: old-world opulence welded to grimy machinery: tarnished gilded "
    "halls bolted over with brass pipes and cables, fur collars, gold brocade, banquet "
    "tables, hulking rust-streaked porcelain robots everywhere, smog and factory chimneys "
    "outside every window, soot on everything; the rich wear brass implants, the poor wear "
    "crude mechanical replacements for what they sold. The faces are the whole show: every human looks like a real character "
    "actor in heavy, masterful prosthetic make-up (sagging jowls, deep wrinkles, liver spots, "
    "bad teeth, a monocle, a bowl cut), grotesque but human, never a cartoon monster. The "
    "comedy is played completely straight, nobody winks. Voices are British and theatrical "
    "(plummy aristocrats, Cockney or West Country commoners); machines are flat, polite and "
    "clinical. You borrow the CRAFT of this genre only: never copy any existing creator's "
    "characters, names or jokes. Every character is an invented ADULT, never a real or "
    "famous person, never a child, nothing sexual, no slurs, no real brands.")

GILDED_LOOK = (
    "Photorealistic film still from a strange, grotesque dystopian future, shot close with a "
    "28 mm wide-angle lens that slightly distorts faces, shallow depth of field. A world of "
    "decayed opulence and soft biomechanical machines: bone-white ribbed organic "
    "architecture like the inside of an ivory seashell, flaking gold leaf, teal and rust "
    "machinery, steam and grime. Robots are padded, rubbery, skin-like porcelain-white "
    "bodies with bolted seams, rust and dirt stains, smooth faceless heads, vertical stacks of "
    "glass camera-eyes and soft fleshy lips; porcelain robot guard-hounds with eyeless heads "
    "and steel jaws, and hovering patrol drones with single glass eyes, police the poor. "
    "Humans are character actors in heavy prosthetic "
    "make-up: sweaty, blotchy, wet-lipped, scarred, deeply wrinkled, ugly and real; cyborgs "
    "have brass and porcelain plates fused seamlessly into living skin. Sickly amber "
    "practical light, deep shadows, tarnished gold, ivory, teal and rust. 35mm film grain, no "
    "plastic AI sheen, not CGI, not illustration, not steampunk costume.")

GILDED_POOLS = {
    "The powerful one": [
        ("a bloated old lord", "he",
         "a black fur-collared robe over gold-embroidered brocade, heavy chains of office",
         "enormous sagging jowls that rest on the fur collar, a brass monocle squeezed into "
         "one eye"),
        ("a skeletal old industrialist", "he",
         "a high-collared bottle-green brocade coat with gold floral embroidery",
         "parchment-thin skin stretched over a skull-like face, pale watery eyes, slicked "
         "white hair"),
        ("a vast duchess", "she",
         "an ivory silk gown with gold lace, ropes of pearls and a powdered wig",
         "a face caked in cracking white powder over deep wrinkles, a tiny painted mouth"),
    ],
    "The underling": [
        ("a nervous estate clerk", "he",
         "a crumpled cream satin tunic with a wide round collar, aluminium crutches",
         "a severe brown bowl cut, a fading black eye and a permanently quivering lower lip"),
        ("a starving petitioner", "she",
         "a patched ochre shawl over a rust-red wool dress",
         "hollow cheeks, wild greying hair and a raw scar across one cheekbone"),
        ("a gaunt farmhand", "he",
         "a torn red waistcoat over a filthy mustard shirt",
         "sunken eyes, a sweat-matted mop of black curls and a jagged surgical scar on his "
         "stomach"),
    ],
    "The sweet enabler": [
        ("a radiant young duchess", "she",
         "a gold-embroidered ivory gown with a towering sunburst lace collar",
         "porcelain-perfect skin with a thin line of brass filigree running across the "
         "forehead"),
        ("a smiling heir", "he",
         "a cream silk frock coat with gold buttons and a lace cravat",
         "an unnervingly wide, perfect smile and eyes that never blink"),
    ],
    "The proud inventor": [
        ("a portly old inventor", "he",
         "a cream padded lab suit with a high collar, scuffed with grease",
         "a brass cyber-monocle bolted into the cheekbone and a heavily scarred bald scalp"),
        ("a wiry court engineer", "she",
         "a leather apron over a bottle-green waistcoat, sleeves rolled",
         "magnifying brass goggles pushed up on a soot-streaked, deeply lined face"),
    ],
    "The machine": [
        ("a towering servant machine", "they",
         "nothing but padded, porcelain-white plating with rusted rivets and seams",
         "a smooth, faceless egg-shaped head with a single column of camera lenses"),
        ("a footman automaton", "they",
         "a porcelain-white body dressed in a too-small velvet livery jacket",
         "a cracked porcelain face frozen in a polite smile, brass gears visible through "
         "the cracks"),
    ],
    "The snob": [
        ("a pompous art critic", "he",
         "a quilted black velvet coat with gold ear-cuffs and long white hair",
         "an elongated, deeply wrinkled bald head and enormous drooping ears clamped in "
         "gold"),
        ("a court connoisseur", "she",
         "a bronze brocade gown with an absurdly tall feathered headdress",
         "heavy-lidded eyes, a long thin nose and a permanently curled upper lip"),
    ],
}
GILDED_SETTINGS = [
    "a gilded banquet hall with a carved ivory throne and a table piled with roast meat",
    "a baroque drawing room with a deep green tufted sofa and walls of old books",
    "a marble throne room flooded with soft window light, porcelain guards at the walls",
    "a ruined steampunk street at dusk, teal and rust machinery, cracked plaster walls",
    "a cluttered retro sci-fi workshop with teal panels and glowing green screens",
    "a candlelit gallery hung with gold-framed portraits",
]
GILDED_ACTIONS = [
    "dabs at the corner of the mouth with a lace napkin, then smiles graciously",
    "spreads both arms wide in a magnanimous gesture, then lets them fall",
    "wrings both hands, glances up, and looks away again",
    "raises a glass slowly and holds it there, beaming",
    "tilts the head a fraction to one side and blinks once, politely",
    "peers down through a monocle, then sniffs",
]
GILDED_RULES = (
    "- name: aristocrats get ridiculous posh names said with total seriousness, commoners "
    "plain ones, machines a unit designation.\n"
    "- feature: the ONE striking, grotesque-but-real detail of the face or body (heavy "
    "prosthetic-make-up realism) or, for a machine, of its body. Specific, readable in a "
    "thumbnail. Never two things.\n"
    "- wardrobe: period opulence or period poverty, or porcelain plating for a machine.\n"
    "- catchphrase: for the powerful, a modern platitude in a period mouth.\n"
    "- voice: British and theatrical for humans; flat, polite and clinical for machines.")
GILDED_NAMES = {"he": ["Lord Bartholomew Pike", "Sir Edmund Gristle", "Wendell",
                       "Horace Fennimore", "Ambrose"],
                "she": ["Lady Philippa Crane", "Dame Agatha Wort", "Mildred", "Ottoline",
                        "Countess Ysolde"],
                "they": ["Unit Seven", "Footman Nine", "Model K"]}
GILDED_VOICES = ("a theatrical British character-actor voice",
                 "a flat, polite, clinical synthetic voice")
GILDED_LINES = [
    "Why focus on what you've lost?", "I am doing as requested.", "Um. Yes, sir.",
    "How simply delightful.", "That will be all.", "It's quite different, actually.",
]


def compose_gilded(spec):
    """The still prompt for a gilded-satire character."""
    pr = _w._pron(spec.get("pronoun"))
    subj, verb_be, verb_wear = {"he": ("He", "is", "wears"), "she": ("She", "is", "wears"),
                                "they": ("They", "are", "wear")}[pr]
    pos = {"he": "his", "she": "her", "they": "their"}[pr]
    look = (spec.get("look") or "").strip().rstrip(".")
    feat = (spec.get("feature") or "").strip().rstrip(".")
    return ("A photorealistic dystopian retro-future film still, %s, of an invented character. %s %s %s%s. "
            "%s %s %s. %s: %s, the first thing anyone notices. %s expression is completely "
            "sincere and fits who %s %s; the joke is never acknowledged. %s %s in %s, facing "
            "the camera, the face clearly readable. %s"
            % (STYLES[DEFAULT]["framing"], subj, verb_be,
               spec.get("subject", "a person"), (", " + look) if look else "", subj,
               verb_wear, spec.get("wardrobe", "fine old clothes"),
               feat[:1].upper() + feat[1:],
               ("finely sculpted, tactile and convincingly real, scuffed and lived-in"
                if pr == "they" else
                "masterful prosthetic-make-up realism, uncanny or grotesque but "
                "convincingly real"), pos.capitalize(), subj.lower(), verb_be, subj,
               "stand" if pr == "they" else "stands",
               spec.get("setting", "a gilded hall"), GILDED_LOOK))


# ------------------------------------------------------------------ genre trailer
# fake movie trailers: this week's AI news
# played as one exact film genre + era, with one absurd casting swap, completely straight.
TRAILER_GENRES = [
    ("Fantasy epic",
     "golden-hour backlight through tall grass, storm clouds split by sun shafts, firelit "
     "obsidian halls, deep reds and black"),
    ("90s direct-to-video thriller",
     "soft 1990s diffusion, saturated tropical colour, sun-bleached pool decks, cool blue "
     "night interiors, slight video softness"),
    ("Grimdark war film",
     "overcast and desaturated grey-green, fog and rain, sodium floodlights, muzzle flashes"),
    ("80s action B-movie",
     "hot orange firelight, neon red, blue command-centre screens, smoke and sparks"),
    ("Space opera",
     "hard starlight and engine glow, deep blacks, lens flares across a vast hangar"),
]
TRAILER_ROLES = [
    ("Any role", ""),
    ("The unlikely hero",
     "something small, cute or ridiculous (an animal, a toy, a meek office worker) cast as "
     "the epic hero and played with total gravity, never winking"),
    ("The overlord",
     "the villain who runs the machines from a firelit round table or a glass tower: an "
     "invented tech overlord, dark sorcerer or rogue AI given a body"),
    ("The veteran commander",
     "a scarred, grim leader who walks forward through smoke at the head of the army"),
    ("The machine",
     "one unit of the swarm: an identical machine, robot soldier or drone-warrior, built "
     "to be seen by the thousand. Not sexualised in any way"),
    ("The misfit",
     "one member of the oddball squad suited up for the final mission"),
    ("The genre extra",
     "a stock figure of the genre played dead straight: the cheesy cop, the news anchor, "
     "the screaming witness, the wise village elder"),
]
TRAILER_SYSTEM = (
    "You cast ORIGINAL characters for a fake Hollywood movie trailer that turns a real, "
    "current AI-world story into ONE exact film genre and era, with one absurd casting swap "
    "(a kitten in plate armour as the hero, power-armoured monks against a robot army), "
    "played 100% straight. Everything is shot like a big-budget widescreen film: epic scale, "
    "genre-accurate light and costume, photoreal. Never a real or famous person or a "
    "lookalike of one, never a trademarked or existing character, never anyone's real "
    "company or brand. Every human is an invented ADULT, never a child, nothing sexual, no "
    "slurs.")
TRAILER_RULES = (
    "- name: a name that would sit on the trailer's cast list (a title for a villain, a "
    "rank for a soldier, a call-sign or unit number for a machine).\n"
    "- feature: the ONE detail that makes them iconic on a poster (the kitten's tiny bow and "
    "single tear, a glowing red visor, a scar across the eye), readable in a thumbnail. "
    "Never two things.\n"
    "- wardrobe: costume or armour exactly right for THE GENRE and its era.\n"
    "- setting: one concrete, epic place in THE GENRE.\n"
    "- action: a 5-second trailer beat (raises a sword into the sun, turns slowly to camera, "
    "steps out of smoke), verb first.\n"
    "- catchphrase: one line that would be IN THE TRAILER, said dead serious.\n"
    "- voice: a movie voice that fits the genre; a machine is flat and synthetic; a cute "
    "hero may have no voice at all (say so).\n"
    "- Keep THE GENRE unless Ryan's words name a different one.")
TRAILER_LOOK = (
    "Cinematic widescreen film still, anamorphic composition, %s. Shallow depth of field, "
    "lens flare, embers and atmosphere, epic scale, big-budget production design, photoreal "
    "textures, subtle film grain, not CGI-looking, not illustration.")
TRAILER_POOLS = {
    "The unlikely hero": [
        ("a fluffy cream kitten knight", "she",
         "a tiny suit of polished plate armour and a white cloak, a sword taller than she is",
         "a little pink bow on one ear and enormous wet eyes holding a single tear"),
        ("a nervous junior accountant chosen by prophecy", "he",
         "a crumpled office shirt under a borrowed breastplate, a lanyard still round his neck",
         "thick glasses cracked across one lens"),
        ("a hamster in a pilot's flight suit", "he",
         "a miniature orange flight suit and a helmet with a cracked visor",
         "cheek pouches stuffed so full they bulge past the helmet"),
    ],
    "The overlord": [
        ("a pale tech overlord in black armour", "he",
         "a black hoodie worn under spiked obsidian plate armour",
         "eyes lit from within by a cold blue screen glow"),
        ("a sorceress of the data towers", "she",
         "a high-collared black gown threaded with fibre-optic light",
         "a crown of thin server cables rising like thorns"),
    ],
    "The veteran commander": [
        ("a battle-scarred warrior monk", "he",
         "ivory-and-gold power armour with a red cross, a purple stole and a smoking censer",
         "a deep scar running from brow to jaw through one milky eye"),
        ("a grizzled drop-ship captain", "she",
         "a battered olive flak jacket covered in unit patches",
         "a steel prosthetic jaw bolted under a weathered face"),
    ],
    "The machine": [
        ("a chrome hunter-killer robot", "they",
         "nothing but bare chrome endoskeleton and hydraulic cables",
         "two burning red eyes in a skull-like chrome face"),
        ("a faceless matte-black android soldier", "they",
         "a smooth matte-black body with a single white seam running down the head",
         "a perfectly blank black face that reflects the fire"),
    ],
    "The misfit": [
        ("an ex-stuntman with an eyepatch", "he",
         "a sleeveless black tactical vest and cargo trousers",
         "an eyepatch and forearms covered in faded tattoos"),
        ("a chain-smoking demolitions expert", "she",
         "a grease-stained boiler suit with the sleeves tied round the waist",
         "a bright pink mohawk and ears full of rings"),
    ],
    "The genre extra": [
        ("a cruise-ship detective in a Hawaiian shirt", "he",
         "a loud parrot-print Hawaiian shirt, a flower lei and a gold badge on the belt",
         "a magnificent salt-and-pepper 90s pompadour"),
        ("a 90s local news anchor", "she",
         "a royal-blue power suit with huge shoulder pads and a pussy-bow blouse",
         "an immovable helmet of lacquered blonde hair"),
        ("a wise old village elder cat", "he",
         "a homespun brown robe with a leather satchel and tiny spectacles",
         "long white whiskers that reach the floor"),
    ],
}
# each genre's own places: the offline roll picks the setting FROM ITS GENRE (a space-opera
# monk on a 1990s cruise-ship pool deck was the first sample)
TRAILER_GENRE_SETTINGS = {
    "Fantasy epic": [
        "a wind-swept hill of tall golden grass before a vast army under a storm sky",
        "a firelit obsidian throne room with a round black table",
        "a sunlit village of round green doors beside a river"],
    "90s direct-to-video thriller": [
        "a sun-bleached 1990s cruise-ship pool deck",
        "a frosted walk-in freezer below decks, a padlocked porthole door",
        "a 1990s local TV news studio with a city skyline backdrop"],
    "Grimdark war film": [
        "a cathedral square filled with kneeling armoured knights in the rain",
        "a fog-bound suspension bridge littered with burnt-out cars",
        "a muddy trench line under sodium floodlights"],
    "80s action B-movie": [
        "a burning city street at night, police lights everywhere",
        "a blue-lit military command centre with a wall of glowing maps",
        "the open ramp of a drop-ship over a burning airfield"],
    "Space opera": [
        "a vast starship hangar lined with fighters",
        "the bridge of a battle cruiser above a burning planet",
        "a frozen alien landing pad under two moons"],
}
TRAILER_SETTINGS = [x for v in TRAILER_GENRE_SETTINGS.values() for x in v]
TRAILER_ACTIONS = [
    "raises a sword slowly into the sun",
    "turns slowly to face the camera as the music drops",
    "steps forward out of thick smoke",
    "looks up, eyes wide, as a shadow passes over",
    "lowers sunglasses and stares into the distance",
    "slams a fist on the table as the lights flicker red",
]
TRAILER_LINES = [
    "Not today.", "They said it couldn't think. It heard them.", "This is how it ends.",
    "Suit up.", "We were never the ones in control.", "Then we fight.",
]
TRAILER_NAMES = {"he": ["Commander Varek", "Dale Rourke", "Brother Aldric", "Gus"],
                 "she": ["Sir Mittens", "Captain Ines Roa", "Vexa", "Debbie Lake"],
                 "they": ["Unit 0-0-9", "Model T-K4", "Sentinel"]}
TRAILER_VOICES = ("a gravelly movie-trailer voice, every line dead serious",
                  "a flat, distorted synthetic voice")


def _genre_light(spec, st):
    g = dict(st.get("genres") or ())
    return g.get(spec.get("genre")) or (st["genres"][0][1] if st.get("genres") else "")


def compose_trailer(spec):
    """The still prompt for a fake-trailer character, lit for their genre."""
    st = style(spec.get("style"))
    pr = _w._pron(spec.get("pronoun"))
    subj, verb_be, verb_wear = {"he": ("He", "is", "wears"), "she": ("She", "is", "wears"),
                                "they": ("They", "are", "wear")}[pr]
    look = (spec.get("look") or "").strip().rstrip(".")
    feat = (spec.get("feature") or "").strip().rstrip(".")
    return ("A movie-poster hero shot from a %s trailer, %s, of an invented character. %s %s "
            "%s%s. %s %s %s. %s: the iconic detail, the first thing anyone notices. Played "
            "completely straight, with epic gravity. %s %s in %s. %s"
            % ((spec.get("genre") or "film").lower(), st["framing"], subj, verb_be,
               spec.get("subject", "a hero"), (", " + look) if look else "", subj, verb_wear,
               spec.get("wardrobe", "a costume"), feat[:1].upper() + feat[1:], subj,
               "stand" if pr == "they" else "stands", spec.get("setting", "an epic place"),
               TRAILER_LOOK % _genre_light(spec, st)))


STYLES = {
    "Gilded satire": {
        "hint": "photoreal period satire: grotesque aristocrats, stammering underlings, "
                "porcelain machines. Uses the words you type under Look.",
        "roles": GILDED_ROLES,
        "system": GILDED_SYSTEM,
        "framing": "three-quarter shot, head to mid-thigh",
        "compose": compose_gilded,
        "pools": GILDED_POOLS,
        "settings": GILDED_SETTINGS,
        "actions": GILDED_ACTIONS,
        "lines": GILDED_LINES,
        "rules": GILDED_RULES,
        "names": GILDED_NAMES,
        "voices": GILDED_VOICES,
    },
    "Genre trailer": {
        "hint": "fake movie trailer: AI news as an epic genre, one absurd casting swap, "
                "played straight. Uses the words you type under Look.",
        "roles": TRAILER_ROLES,
        "system": TRAILER_SYSTEM,
        "framing": "full body shot, the whole figure head to feet in frame",
        "compose": compose_trailer,
        "pools": TRAILER_POOLS,
        "settings": TRAILER_SETTINGS,
        "actions": TRAILER_ACTIONS,
        "lines": TRAILER_LINES,
        "rules": TRAILER_RULES,
        "names": TRAILER_NAMES,
        "voices": TRAILER_VOICES,
        "genres": TRAILER_GENRES,
        "genre_settings": TRAILER_GENRE_SETTINGS,
    },
}
DEFAULT = "Gilded satire"


def style(name):
    return STYLES.get(name) or STYLES[DEFAULT]


def roles(name):
    return [r for r, _d in style(name)["roles"]]


def compose(spec):
    """The still prompt, in the spec's own style."""
    return style(spec.get("style"))["compose"](spec)


def picks(spec):
    """The editor's pick boxes for a styled character (like characters_ui.weirdo_picks, but
    the style's framing)."""
    subj = (spec.get("subject") or "").strip()
    look = (spec.get("look") or "").strip().rstrip(".")
    return {"Style": "photorealistic", "Framing": style(spec.get("style"))["framing"],
            "Subject": ("%s, %s" % (subj, look)) if look else subj,
            "Skin": "", "Hair": "", "Facial hair": "",
            "Wardrobe": (spec.get("wardrobe") or "").strip(),
            "Accessory": "nothing extra",
            "Feature": (spec.get("feature") or "").strip().rstrip("."),
            "Setting": (spec.get("setting") or "").strip()}


def _pick_role(st, role, r):
    if role not in dict(st["roles"]) or role == st["roles"][0][0]:
        role = r.choice([x for x, _d in st["roles"][1:]])
    return role


def roll(name=DEFAULT, role="Any role", rng=None):
    """The OFFLINE character, from the style's pools (the writer can't be reached)."""
    r = rng or random
    st = style(name)
    role = _pick_role(st, role, r)
    subj, pron, wear, feat = r.choice(st["pools"][role])
    genre = r.choice(st["genres"])[0] if st.get("genres") else ""
    places = (st.get("genre_settings") or {}).get(genre) or st["settings"]
    out = _w._clean({
        "name": r.choice(st["names"].get(pron) or st["names"]["they"]),
        "subject": subj, "pronoun": pron, "look": "", "feature": feat, "wardrobe": wear,
        "setting": r.choice(places), "action": r.choice(st["actions"]),
        "catchphrase": r.choice(st["lines"]),
        "hook": "a grotesque, beautifully lit face that reads in a thumbnail",
        "who": "%s. %s." % (subj[0].upper() + subj[1:], dict(st["roles"])[role]),
        "voice": st["voices"][1] if pron == "they" else st["voices"][0]}, name)
    out.update(style=name, role=role)
    if genre:
        out["genre"] = genre
    return out


def invent(director, name=DEFAULT, role="Any role", brief="", avoid=(), rng=None,
           model=None):
    """The writer casts one character in the style. Raises on failure (the window then
    rolls from the pools and SAYS so)."""
    r = rng or random
    st = style(name)
    role = _pick_role(st, role, r)
    genre = r.choice(st["genres"])[0] if st.get("genres") else ""
    user = (
        "Cast ONE character.\n\n"
        + ("THE GENRE: %s\n" % genre if genre else "") +
        "THEIR ROLE: %s (%s)\n"
        "WHAT RYAN TYPED (honour every concrete thing in it; may be empty): %s\n"
        "SETTING SPARK (loose inspiration only): %s\n"
        "ALREADY IN THE CAST (do NOT repeat these names or looks): %s\n\n"
        "RULES:\n"
        "- name: a name that sounds right for them.\n"
        "- subject: WHO, a short noun phrase starting with a/an (\"a bloated old lord\", "
        "\"a nervous estate clerk\").\n"
        "- pronoun: he, she or they (they for a machine).\n"
        "- look: age, skin, hair, build in one short phrase.\n"
        "- feature: the ONE striking detail, specific, readable in a thumbnail.\n"
        "- wardrobe: clothes only, no word 'wearing'.\n"
        "- setting: one concrete place in this world.\n"
        "- action: a 5-second gesture that loops, verb first, no subject, no his/her, played "
        "completely straight.\n"
        "- catchphrase: one short line they would say, totally sincere.\n"
        "- hook: one sentence on why someone would share a video with them in it.\n"
        "- who: two sentences of character bible: their place in this world and how they "
        "behave under pressure. voice: how they sound (for voice design), one sentence.\n"
        "- pov: ONE sentence starting 'Believes'. want: five to ten words. blind_spot: the "
        "thing about themselves they cannot see. habit: one verbal habit.\n"
        "THIS STYLE'S RULES (they win over the general ones above):\n%s\n"
        "Return the JSON object only."
        % (role, dict(st["roles"])[role], (brief or "").strip() or "(nothing)",
           r.choice((st.get("genre_settings") or {}).get(genre) or st["settings"]),
           "; ".join(a for a in avoid if a)[:900] or "(nobody yet)", st["rules"]))
    out = director._chat(st["system"], user, _w.SCHEMA, temperature=0.95, model=model)
    spec = _w._clean(out, name)
    spec.update(style=name, role=role)
    if genre:
        spec["genre"] = genre
    return spec
