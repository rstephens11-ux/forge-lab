#!/usr/bin/env python3
"""format_char.py — the FORMAT-CHARACTER bench: formal-European dress, one improbable
physical feature, one repeated silent motion performed completely straight, a grand
European square, a verbal catchphrase.

The brief is in qwen-image-local-forge → references/weird-character-ledger.md ("Format
characters (the Jean Phil vein)"). This tool exists because a format character is a
DIFFERENT shape from the weirdo roster: not a silhouette gag but a copyable FORMAT whose
motion is the product and whose feature is the mascot.

    bundle <slug>            write characters/<slug>/ (character.json + bible.json + NOTES.md)
    seeds  <slug> [n] [scale] render n t2i seeds into drafts/  (scale Full|Fast|Draft)
    pick   <slug> <png>      save an already-rendered png as the character's still

⚠️ A 3-seed spread at Full is ~90 s a seed — pin the seed you keep, never re-roll one.
⚠️ Full-length framing is REQUIRED: these go into r2v, and r2v needs the whole figure.
"""
import json
import os
import sys

sys.path.insert(0, os.path.expanduser("~/forge-lab"))
os.environ.setdefault("FORGE_URL", "http://forge:8188")
os.environ.setdefault("FORGE_LAB_NODIRECTOR", "1")

import forge_lab as fl                                     # noqa: E402

DRAFTS = os.path.expanduser("~/forge-lab/drafts")

# ---------------------------------------------------------------------------
# The skeleton. Swap SUBJECT / HAIR / SKIN / FEATURE / WARDROBE / SETTING / POSE only.
# ---------------------------------------------------------------------------
FRAMING = "full body shot, the whole figure head to feet in frame"
POSE = "standing straight and facing the camera, arms relaxed at his sides"
FINISH = ("Overcast natural daylight, soft shadows, no direct sun. Documentary realism, "
          "visible skin pores and fabric texture, fine 35mm film grain, matte surfaces, "
          "natural colour, no cosmetic retouching, no glow, not CGI, not illustration.")
EXPRESSION = ("His expression is flat, stern and completely serious — he never smiles, and "
              "the joke is never acknowledged.")

# ⚠️ Every feature sentence carries the realism clause INTO the featured region. An
# over-scaled region without it comes back as a smooth, textureless tell that reads as
# liquify rather than a photographed body (the ledger's most-repeated pitfall).
REALISM = ("yet it reads as real human tissue, with the same visible pores, fine wrinkles "
           "and natural texture as the rest of his face — nothing smoothed, stretched or "
           "airbrushed")

# ⚠️ The face clause above is wrong for a LIMB. An extra arm has to claim arm
# tissue, or a second pair comes back as smooth plastic prosthetics.
LIMB_REALISM = ("yet all four read as real human tissue, with the same visible pores, fine "
                "hairs and natural texture as one another — nothing smoothed, stretched or "
                "airbrushed")


CHARACTERS = {
    # ------------------------------------------------------------------ Gustav
    "gustav": {
        "name": "Gustav",
        "subject": "an elderly man",
        "hair": "with a short white moustache",
        "skin": "with ruddy, weathered skin",
        "feature": ("an enormous wild mane of silver-white hair that stands up off his head "
                    "and fans out in every direction, as if he were standing in a gale, %s"
                    % REALISM),
        "wardrobe": ("a black tailcoat, a white dress shirt, a white waistcoat, a white bow "
                     "tie and white gloves"),
        "setting": ("a grand baroque stone square in Vienna, an ornate fountain behind him "
                    "and a pale classical facade rising beyond"),
        "action": "conducts an invisible orchestra with total concentration",
        "catchphrase": "No, no. Again — from the top.",
        "bible_who": ("A retired Viennese orchestra conductor of enormous and entirely "
                      "unearned dignity. He treats an empty city square as his concert hall "
                      "and the pigeons as his players. He is immaculately turned out and "
                      "utterly serious."),
        "bible_absurd": ("His hair is a storm of silver that never lies down, no matter the "
                         "weather, and he has never once mentioned it."),
        "bible_voice": ("A dry, precise, slightly weary Central-European baritone — the voice "
                        "of a man who has corrected the same mistake a thousand times."),
        "source": ("picked from drafts/vp_pick_gustav.png (= vp_gustav-03), the cleanest read "
                   "of the format-character test; ledger calls it 'best of the set'"),
    },
    # --------------------------------------------------------------- Augustin
    "augustin": {
        "name": "Augustin",
        "subject": "an elderly man",
        "hair": "with short cropped grey hair",
        "skin": "with ruddy, weathered skin",
        "feature": ("a pair of grotesquely enormous, fleshy, saucer-like ears that hang down "
                    "well past his jaw like a basset hound's, %s" % REALISM),
        "wardrobe": ("a three-piece tweed suit with a gold pocket-watch chain and a walking "
                     "cane"),
        "setting": ("a square in Prague's Old Town, the astronomical clock tower rising "
                    "behind him"),
        "action": "bows deeply to nobody, three times, without hurrying",
        "catchphrase": "One moment. Still not quite right.",
        "bible_who": ("An elderly Prague gentleman of inflexible politeness who conducts his "
                      "entire life as a series of formal courtesies owed to an audience that "
                      "is not there. Nothing hurries him. Nothing embarrasses him."),
        "bible_absurd": ("His ears are vast slabs of hanging cartilage, and he bows to empty "
                         "pavements as though a room full of people were watching."),
        "bible_voice": ("A courtly, unhurried, gravelly baritone with an old-world cadence — "
                        "every sentence finished, no sentence raised."),
        "source": "rolled to be distinct from Gustav: ears not hair, bowing not conducting, Prague not Vienna",
    },
# ------------------------------------------------------------------ Ludvig
    # Rolled in place of Augustin (Ryan's call, 2026-10-07: "use a different character
    # than the big ear guy"). Same format, different silhouette AND a different motion —
    # bone instead of hair (Gustav), a waltz instead of a conducting, indoors not out.
    "ludvig": {
        "name": "Ludvig",
        "subject": "an elderly man",
        "hair": "with a neat white moustache and thin white hair",
        "skin": "with pale, finely wrinkled skin",
        "feature": ("an extraordinarily tall, elongated, loaf-shaped skull that rises high "
                    "above his brow like a sugarloaf, %s" % REALISM),
        "wardrobe": "a deep-green velvet evening jacket with a silk scarf and patent shoes",
        "setting": ("a vast empty opera-house foyer, a marble staircase sweeping up behind "
                    "him and a chandelier high overhead"),
        "action": "waltzes alone with an invisible partner",
        "catchphrase": "You're doing it wrong. Both of you.",
        "bible_who": ("An elderly gentleman of an opera house that is always empty. He "
                      "behaves as though a full house were watching from the balconies, and "
                      "he has never once performed for anyone."),
        "bible_absurd": ("His skull is a long tall loaf of bone that rises far above his "
                         "brow, and his hair is arranged around it as if this were normal."),
        "bible_voice": ("A soft, precise, slightly amused old-world baritone, as if he is "
                        "correcting a partner only he can hear."),
        "source": ("rolled to replace the big-ear pick (Ryan: 'use a different character "
                   "than the big ear guy'); tall loaf skull reads at FULL LENGTH, which the "
                   "r2v reference needs"),
    },
    # --------------------------------------------------------------------- Armand
    # Rolled 2026-10-11 — Ryan: "a weirdo named armand. he should have 4 arms. i want him
    # to be a slick watch salesman." The broken dial is the LIMB COUNT and nothing else:
    # everything around it is a well-dressed man at work, played completely straight.
    # ⚠️ Wardrobe must NOT cover the featured region — a jacket renders two ordinary
    # sleeves and re-hides the lower pair behind them. So: waistcoat, sleeves rolled, and
    # every forearm bare with a watch on every wrist (which is also the sales motion).
    "armand": {
        "name": "Armand",
        "subject": "a well-groomed middle-aged man",
        "hair": "with slicked-back black hair and a thin pencil moustache",
        "skin": "with smooth, tanned skin",
        "feature": ("four arms in total: a second, fully formed pair of human arms grows "
                    "from his ribs below the first, hanging folded across his waist like the "
                    "lower arms of a man who has always had them, %s" % LIMB_REALISM),
        "pick_feature": "four arms in total",
        "wardrobe": ("a cream waistcoat over a white shirt with the sleeves rolled up to the "
                     "elbow, dark trousers and black patent shoes"),
        "setting": ("a covered nineteenth-century European shopping arcade, an iron-and-glass "
                    "roof overhead, brass watch-shop fronts glinting on either side and a "
                    "polished marble floor underfoot"),
        "pose": ("standing straight and facing the camera, his upper pair of arms held out "
                 "from his sides with the palms turned up and his lower pair folded across "
                 "his waist, all four bare forearms in view"),
        "action": ("presents a gold wristwatch in turn with each of his four hands, one "
                   "after another, without hurrying"),
        "catchphrase": "Genuine. Everything you see is genuine.",
        "bible_who": ("A watch seller of a covered European arcade that has never had a "
                      "customer and in which he has never stopped selling. He is immaculately "
                      "turned out, entirely serious, and treats every passer-by as a man "
                      "already halfway to buying."),
        "bible_absurd": ("He has four arms, he has never once mentioned it, and he uses all "
                         "four with total economy — as though everyone did."),
        "bible_voice": ("A smooth, low, unhurried salesman's baritone — every sentence "
                        "delivered as a closing line."),
        "source": ("rolled for Ryan 2026-10-11: 'a weirdo named armand. he should have 4 arms. "
                   "i want him to be a slick watch salesman' — one broken volume dial (limb "
                   "count), deadpan, nothing else weird"),
    },
}


def compose(spec):
    """The full still prompt for a format character at FULL LENGTH."""
    return ("A full-length photograph of an invented person, %s. He is %s, %s, %s. He wears "
            "%s. %s. %s He stands in %s. %s"
            % (FRAMING, spec["subject"], spec["hair"], spec["skin"], spec["wardrobe"],
               spec["feature"].capitalize(),
               EXPRESSION + " " + spec.get("pose", POSE), spec["setting"], FINISH))


def specs(slug):
    s = CHARACTERS.get(slug)
    if not s:
        raise SystemExit("unknown format character %r (known: %s)"
                         % (slug, ", ".join(sorted(CHARACTERS))))
    return s


# ---------------------------------------------------------------------------
def cmd_bundle(argv):
    """Write characters/<slug>/ from an already-rendered still."""
    slug = argv[0]
    spec = specs(slug)
    still = argv[1] if len(argv) > 1 else None
    prompt = compose(spec)
    meta = {
        "name": spec["name"],
        "prompt": prompt,
        "seed": "-1",
        "lora": None,
        "lora_strength": "1.0",
        "picks": {"Style": "photorealistic",
                  "Framing": FRAMING,
                  "Feature": spec.get("pick_feature") or spec["feature"].split(",")[0]},
        "note": "FORMAT character (Jean Phil vein) — the motion is the product, the feature is the mascot",
        "action": spec["action"],
    }
    d = fl.write_character(slug, meta, still_src=still)
    bible = {"who": spec["bible_who"], "absurd_thing": spec["bible_absurd"],
             "voice": spec["bible_voice"],
             "running_bits": [spec["action"],
                              "Delivers the catchphrase \"%s\" to nobody in particular."
                              % spec["catchphrase"]],
             "never": ["Never smile.",
                       "Never acknowledge that anything about him is unusual.",
                       "Never rush — every movement is deliberate and complete."],
             "slug": slug}
    with open(os.path.join(d, "bible.json"), "w") as f:
        json.dump(bible, f, indent=1)
    with open(os.path.join(d, "NOTES.md"), "w") as f:
        f.write("# %s — format character\n\n" % spec["name"])
        f.write("**Format:** formal European dress + ONE improbable physical feature + one "
                "repeated silent motion performed completely straight + a grand European "
                "square + a catchphrase. Silent and legible in one second.\n\n")
        f.write("- **Feature (the mascot):** %s\n" % spec["feature"])
        f.write("- **Motion (the product):** %s\n" % spec["action"])
        f.write("- **Setting:** %s\n" % spec["setting"])
        f.write("- **Catchphrase:** \"%s\"\n" % spec["catchphrase"])
        f.write("- **Source:** %s\n\n" % spec["source"])
        f.write("Still: `still.png` (full-length — required, r2v needs the whole figure).\n")
        f.write("Bible: `bible.json` — read by the Director as the identity block.\n")
    print("bundled %s -> %s" % (slug, d))
    return d


def cmd_seeds(argv):
    """Render N t2i seeds into drafts/ so all of them can be looked at."""
    slug = argv[0]
    n = int(argv[1]) if len(argv) > 1 else 3
    scale = argv[2] if len(argv) > 2 else "Full"
    spec = specs(slug)
    prompt = compose(spec)
    w, h = fl.char_still_size(FRAMING, scale)
    print("seeds: %s  canvas %dx%d  scale %s" % (slug, w, h, scale), flush=True)
    out = []
    for i in range(n):
        pid, seed = fl.submit("t2i", {"prompt": prompt, "width": w, "height": h,
                                      "resolution": max(w, h), "steps": 25, "seed": -1,
                                      "prefix": "fmt_%s_%02d" % (slug, i)}, None)
        meta = fl.wait_and_collect(pid, timeout=1800)
        if not meta or meta.get("error"):
            print("seed %d FAILED: %r" % (i, (meta or {}).get("error")), flush=True)
            continue
        src = None
        for f_ in (meta.get("images") or []):
            p = os.path.join(fl.RUNS, meta["id"], f_["file"])
            if os.path.exists(p):
                src = p
                break
        if not src:
            print("seed %d: no output" % i, flush=True)
            continue
        dst = os.path.join(DRAFTS, "fmt_%s_%02d.png" % (slug, i))
        import shutil
        shutil.copyfile(src, dst)
        print("seed %d SAVED %s  (seed=%s pid=%s)" % (i, dst, seed, pid), flush=True)
        out.append((dst, seed))
    print("DONE %d/%d" % (len(out), n))
    return out


def main(argv):
    if not argv or argv[0] not in ("bundle", "seeds", "pick"):
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "bundle":
        cmd_bundle(rest)
    elif cmd == "seeds":
        cmd_seeds(rest)
    elif cmd == "pick":
        slug, png = rest[0], rest[1]
        fl.write_character(slug, fl.read_character(slug) or {}, still_src=png)
        print("still set for %s -> %s" % (slug, png))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))