"""CHARACTER VOCABULARY, expanded (2026-10-17). Ryan: "maybe we can expand the dropdown options
significantly in the characters. I want everything from a fashion model to a mailman. all kinds
of options."

forge_lab.CHAR_OPTIONS stays ONE flat list per axis (rolls, the composer and the guards in
tools/character_matrix.py read it); extend() appends these after the originals — originals
first, in order, no duplicates. groups() is how the editor shows a long axis: a submenu per
group (Subject ▸ Jobs ▸ a mail carrier) instead of an 80-row popup.

Rules the guards hold every value to: Wardrobe never starts with "wearing" (the composer adds
it); Subjects are ADULTS; no real people, no brands. Accessories follow the existing
"wearing … / holding …" shape.
"""

GROUPS = {
    "Subject": [
        ("Jobs", [
            "a mail carrier", "a line cook", "a nurse", "a firefighter", "an airline pilot",
            "a flight attendant", "a plumber", "a janitor", "a librarian", "a dentist",
            "a surgeon", "a lab scientist", "a farmer", "a lumberjack", "a fisherman",
            "a construction worker", "a security guard", "a mall cop", "a crossing guard",
            "a barista", "a waiter", "a butler", "a hotel concierge", "a real-estate agent",
            "a used-car salesman", "a CEO", "an accountant", "a news anchor",
            "a TV weatherman", "a game-show host", "a fitness instructor", "a yoga teacher",
            "a lifeguard", "a park ranger", "a zookeeper", "a beekeeper", "a tattoo artist",
            "a hairdresser", "a florist", "a butcher", "a baker", "a taxi driver",
            "a long-haul trucker", "an astronaut", "a judge", "a detective", "a magician",
            "a mime", "a rodeo clown", "a wedding singer", "an opera singer",
            "a ballet dancer", "a sumo wrestler", "a pro wrestler", "a jockey",
            "a chess grandmaster", "a museum guard", "a tour guide",
            "a cruise-ship entertainer", "a mall Santa", "a club DJ", "a street performer",
            "a referee", "a sommelier", "a pharmacist", "a dog groomer",
        ]),
        ("Scenes & tribes", [
            "a punk rocker", "a goth", "a skateboarder in his thirties", "a surfer",
            "a biker", "a cowboy", "a cowgirl", "an aging hippie", "a pro gamer",
            "a tech-startup founder", "a wellness influencer", "a gym bro",
            "a country-club golfer", "a Florida retiree", "a suburban soccer mom",
            "a suburban dad", "a raver", "a metalhead", "a pop idol", "a rapper",
            "a lounge crooner", "a drag queen", "a bodybuilder", "a marathon runner",
            "a birdwatcher", "a conspiracy podcaster", "a cosplayer", "a cottagecore baker",
        ]),
        ("Eras & legends", [
            "a Roman senator", "a medieval knight", "a Viking warrior", "a pirate captain",
            "a samurai", "a Victorian gentleman", "a Victorian lady", "a 1920s flapper",
            "a 1950s housewife", "a 1970s disco dancer", "a 1980s aerobics instructor",
            "a 1990s slacker", "an Egyptian pharaoh", "a Renaissance painter",
            "a Wild West sheriff", "a monk", "a nun", "a 1960s astronaut",
            "a court jester", "a powdered-wig aristocrat",
        ]),
        ("Runway", [
            "a fashion model", "a runway model mid-stride", "an aging supermodel",
            "a haughty fashion editor", "a celebrity stylist",
        ]),
    ],
    "Wardrobe": [
        ("Uniforms", [
            "a postal uniform with a mail satchel", "chef's whites and a tall toque",
            "nurse's scrubs and clogs", "a firefighter's turnout gear",
            "an airline pilot's uniform with wings and a cap",
            "a flight attendant's uniform with a neck scarf",
            "a security guard's uniform with a radio on the belt",
            "a waiter's black vest and bow tie", "a butler's tailcoat and white gloves",
            "a hi-vis vest and a hard hat", "a lab coat and safety goggles",
            "a ranger's uniform with a wide-brimmed hat", "a full beekeeper suit and veil",
            "a white spacesuit with the helmet under one arm", "a judge's black robe",
            "a trench coat and a fedora", "a referee's striped shirt and whistle",
            "a lifeguard's red shorts and tank top", "a mime's striped shirt, braces and "
            "white gloves", "a pro-wrestling singlet and a sequined cape",
            "a jockey's bright silks and helmet", "a Santa suit", "a fast-food uniform "
            "with a paper visor", "a mustard-yellow sport coat and a loud tie",
            "a tuxedo with tails", "a sommelier's apron and a tasting cup on a chain",
        ]),
        ("Eras", [
            "a Roman toga and a laurel wreath", "a knight's chainmail and surcoat",
            "Viking furs and a leather belt", "a pirate captain's long coat and tricorn hat",
            "lacquered samurai armour", "a 1920s beaded flapper dress and headband",
            "a 1950s polka-dot swing dress and apron",
            "a 1970s disco jumpsuit with flared legs",
            "an 80s neon aerobics leotard with leg warmers",
            "a 90s flannel shirt over ripped jeans", "a Victorian frock coat and top hat",
            "a monk's brown hooded habit", "a nun's black-and-white habit",
            "a pharaoh's gold collar and striped headdress", "a powdered wig and a silk "
            "frock coat",
        ]),
        ("Scenes & tribes", [
            "a studded leather jacket and spiked hair", "goth black lace and platform boots",
            "a wetsuit unzipped to the waist over a rash guard", "biker leathers and a "
            "bandana", "a tie-dye shirt and round sunglasses", "an esports jersey and a "
            "gaming headset", "a fleece vest over a button-down shirt",
            "a velour tracksuit and gold chains", "a golf polo, plaid trousers and a visor",
            "a sequined stage jumpsuit", "matching beige athleisure",
            "a cowboy's denim, chaps and a big belt buckle", "rave gear: neon mesh and "
            "glow sticks", "a drag ballgown with a towering wig",
            "head-to-toe camouflage hunting gear", "a cosplay suit of foam armour",
        ]),
        # ADULT characters only (both groups) — see ADULT_ONLY_GROUPS / conflict()
        ("Swimwear", [
            "a classic black one-piece swimsuit", "a retro high-waisted polka-dot bikini",
            "a red string bikini", "a white bikini with a sarong tied at the hip",
            "a 1950s pin-up swimsuit with a halter neck", "a sporty racerback swimsuit",
            "a high-cut 1980s one-piece swimsuit in neon pink", "a 1970s crochet bikini",
            "a gold metallic bikini", "a leopard-print one-piece swimsuit",
            "a cut-out black monokini", "a sporty tankini and board shorts",
            "a cherry-print halter bikini", "a ruffled off-the-shoulder bikini",
            "a lifeguard-red one-piece swimsuit", "a triangle bikini and a bucket hat",
            "a wrap-front one-piece swimsuit under an open beach kaftan",
            "a retro skirted swimsuit and a flowered bathing cap",
            "a Victorian striped knee-length bathing costume",
            "tight Speedo swim trunks", "a Speedo and a heavy gold chain",
            "neon swim briefs", "baggy board shorts and no shirt",
            "Hawaiian-print board shorts and no shirt",
            "short 1970s swim trunks and aviator sunglasses",
            "a 1920s striped men's bathing costume",
        ]),
        ("Lingerie & sleepwear", [
            "a black lace lingerie set", "a red satin lingerie set under an open silk robe",
            "a white lace bodysuit", "a silk slip nightgown",
            "a vintage corset and a silk robe", "a fluffy bathrobe over lingerie",
            "a black satin bralette and high-waisted briefs", "a pastel lace lingerie set",
            "a 1950s bullet-bra lingerie set with a garter belt",
            "a silk camisole and shorts set", "a long satin robe over a lace slip",
            "a feather-trimmed marabou robe over a satin slip",
            "a vintage silk kimono robe over lingerie", "a red lace teddy",
            "a leopard-print satin slip", "an old-fashioned white cotton nightgown",
            "satin boxer shorts and an open silk robe",
            "a velvet smoking jacket over silk boxers",
        ]),
        ("Runway", [
            "a couture ballgown with an enormous train",
            "head-to-toe monogrammed streetwear",
            "a sculptural runway look made of moulded plastic",
            "a suit made entirely of patchwork denim",
            "a gown of crinkled metallic foil",
        ]),
    ],
    "Accessory": [
        ("Holding", [
            "holding a clipboard", "holding a megaphone", "holding a tiny dog",
            "holding a bouquet of flowers", "holding a gold trophy", "holding a briefcase",
            "holding a baguette", "holding a rubber chicken", "holding a microphone",
            "holding a fishing rod", "holding a cat", "holding a leaf blower",
            "holding a smartphone on a selfie stick", "holding a giant novelty cheque",
            "holding a tray of champagne flutes", "holding a stack of mail",
            "holding a single egg very carefully", "holding a framed photo of themselves",
        ]),
        ("Wearing", [
            "wearing aviator sunglasses", "wearing a monocle", "wearing a top hat",
            "wearing a cowboy hat", "wearing a hard hat", "wearing a small crown",
            "wearing a headset microphone", "wearing a fanny pack",
            "wearing a whistle on a lanyard", "wearing a lanyard of ID badges",
            "wearing pearls", "wearing a heavy gold chain", "wearing a name tag",
            "wearing a sash", "wearing oversized headphones", "wearing a neck brace",
        ]),
    ],
    "Setting": [
        ("Everyday", [
            "a suburban cul-de-sac", "an empty strip-mall parking lot",
            "a beige office cubicle", "a DMV waiting room", "a fluorescent supermarket aisle",
            "a laundromat", "a gas station at night", "a diner booth", "a fast-food counter",
            "a gym full of mirrors", "a bowling alley", "a hotel lobby", "an airport gate",
            "a city bus", "a subway platform", "a backyard barbecue", "a wedding reception",
            "a corporate conference stage", "a TV news studio", "a game-show set",
            "a hospital corridor", "a public library", "a museum gallery",
            "a car dealership lot", "a post office counter", "a hair salon",
        ]),
        ("Out there", [
            "a Florida motel pool", "a desert highway", "a misty pine forest",
            "a snowy mountain summit", "a tropical beach", "a cornfield",
            "a narrow Venice canal", "a Parisian street", "a Tokyo crossing at night",
            "a grand baroque square in Vienna", "a Hollywood red carpet",
            "a Las Vegas casino floor", "a cruise-ship deck", "a farm with a red barn",
            "a lighthouse on a cliff", "a golf course", "a stadium field",
        ]),
        ("Eras & fantasy", [
            "a medieval castle hall", "a Roman forum", "a pirate ship deck",
            "a Wild West saloon", "a 1950s diner", "a 1970s disco floor", "a 1980s mall",
            "a space-station corridor", "the surface of the moon", "a Victorian parlour",
            "an Egyptian tomb",
        ]),
    ],
    "Lighting": [
        ("Mood", [
            "harsh noon sun", "fluorescent office lighting", "neon signs at night",
            "on-camera flash, red-eye snapshot", "candlelight", "a single stage spotlight",
            "blue-hour dusk", "fog with strong backlight", "bright TV studio lighting",
            "sunset backlight", "a flickering fluorescent tube", "disco lights",
        ]),
    ],
}

GROUP_FOR_ORIGINALS = "Classic"

# ---- ADULTS ONLY: swimwear and lingerie never go on a minor, in any path ----------------
# The Subject list holds a few non-adult entries ("a child of about six", "a willowy teenage
# model") for comedy. The composer, the rolls and Make still all check this pair, so no prompt
# the app writes or renders can put a minor in swimwear or lingerie.
import re as _re
ADULT_ONLY_GROUPS = ("Swimwear", "Lingerie & sleepwear")
ADULT_ONLY_GROUP = ADULT_ONLY_GROUPS[0]          # (the original single group's name, kept)
_MINOR = _re.compile(
    r"\b(child|children|kid|kids|boy|boys|girl|girls|teen|teens|teenage|teenager|teenagers|"
    r"baby|babies|toddler|minors?(?![- ]league)|schoolgirl|schoolboy|underage|preteen|juvenile|"
    r"(?:[1-9]|1[0-7])[- ]?(?:year|yr)s?[- ]old|of about (?:[1-9]|1[0-7])\b)", _re.I)
_SKIN_WEAR = _re.compile(
    r"\b(lingerie|bikini|bikinis|swimsuit|swimsuits|swimwear|swim trunks|speedo|bra|bralette|"
    r"panties|underwear|thong|negligee|nightie|nightgown|corset|garter|garters|bodysuit|"
    r"teddy|boxers|briefs|bathing suit|board shorts and no shirt|topless|shirtless|monokini|tankini|bathing costume|bathing costumes|swim briefs|camisole|garter belt|bullet-bra|satin slip|silk slip|lace slip|trunks and no shirt|bathing cap|boxer shorts)\b", _re.I)


def is_minor(text):
    return bool(_MINOR.search(text or ""))


def is_adult_only(text):
    """Swimwear / lingerie (the group's values, or the words anywhere in a prompt)."""
    t = text or ""
    return t in _adult_only_values() or bool(_SKIN_WEAR.search(t))


def _adult_only_values():
    return {v for g, vs in GROUPS["Wardrobe"] if g in ADULT_ONLY_GROUPS for v in vs}


def conflict(subject_or_prompt, wardrobe_or_prompt=None):
    """True when a minor and swimwear/lingerie meet — in the picks, or in one prompt text."""
    a = subject_or_prompt or ""
    b = a if wardrobe_or_prompt is None else wardrobe_or_prompt
    return is_minor(a) and is_adult_only(b)     # the values that were there before: their own submenu


def extend(char_options):
    """CHAR_OPTIONS with every GROUPS value appended to its axis (originals first, in order,
    nothing duplicated). Axes and their order are unchanged."""
    out = []
    for axis, vals in char_options:
        merged = list(vals)
        seen = set(merged)
        for _g, more in GROUPS.get(axis, []):
            for v in more:
                if v not in seen:
                    merged.append(v)
                    seen.add(v)
        out.append((axis, merged))
    return out


def groups(axis, vals):
    """[(group, [values])] for the editor's submenus — None when the axis is short enough
    for a plain menu. Every value appears exactly once; originals sit in 'Classic'."""
    g = GROUPS.get(axis)
    if not g or len(vals) <= 24:
        return None
    grouped = {v for _n, vs in g for v in vs}
    classic = [v for v in vals if v not in grouped]
    return ([(GROUP_FOR_ORIGINALS, classic)] if classic else []) + [
        (n, [v for v in vs if v in vals]) for n, vs in g]
