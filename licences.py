"""LICENCES — the "paid work" switch (roadmap 2026-10-18, item 11).

Fish S2-Pro (a voice engine) is Research / Non-Commercial only (AGENTS.md). When Ryan marks
the work as PAID (an ad, a client), anything that would send lines to Fish refuses with a
plain message instead of quietly making voices he can't use. The one choke point is
audio/client.tts_batch, so Dialogue, Quick, Voices and the Episode all go through it.

The switch is settings.json "paid_work" (Quick -> More options). Tk-free, no imports from the
app, so the audio client can use it.
"""
import json
import os

# engine -> what it is, for the messages and the export's licence note
NON_COMMERCIAL = {"fish": "Fish S2-Pro (voices)"}
ENGINE_NAMES = {"qwen": "Qwen3-TTS", "fish": "Fish S2-Pro",
                "drama3": "Drama 3 (int4, Fish Audio Community Licence)"}
# NOT in NON_COMMERCIAL on purpose: Drama 3 ships under the Fish Audio Community Licence v1.0,
# which DOES allow commercial use up to USD 500k/yr revenue (agency/studio work for clients
# included). Two conditions it does impose, which are not this switch's job to police but are
# worth knowing: no hosted TTS/voice-cloning SERVICE, and no cloning a real person without
# consent.


class PaidWorkBlocked(RuntimeError):
    pass


def settings_path():
    return os.path.join(os.path.expanduser(os.environ.get("FORGE_LAB_DATA") or "~/forge-lab"),
                        "settings.json")


def _read():
    try:
        d = json.load(open(settings_path()))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def paid_work():
    return bool(_read().get("paid_work"))


def set_paid_work(on):
    d = _read()
    d["paid_work"] = bool(on)
    os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
    with open(settings_path(), "w") as f:
        json.dump(d, f, indent=1)
    return d["paid_work"]


def check(engine):
    """Raise PaidWorkBlocked when paid work is on and `engine` isn't licensed for it."""
    if engine in NON_COMMERCIAL and paid_work():
        raise PaidWorkBlocked(
            "Paid work is on, and %s is non-commercial — its voices can't go in paid "
            "work. Switch the voice engine to Qwen3-TTS, or turn Paid work off (Quick → "
            "More options)." % NON_COMMERCIAL[engine])


def note(voice_engine=None):
    """The export's licence line."""
    eng = voice_engine or "qwen"
    ok = eng not in NON_COMMERCIAL
    return ("Made for paid work: %s\nVoices: %s — %s\n" % (
        "yes" if paid_work() else "no", ENGINE_NAMES.get(eng, eng),
        "ok for commercial use" if ok else "NON-COMMERCIAL ONLY (research / personal)"))
