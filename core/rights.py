"""
Consumer-facing explanation layer.
==================================

An inspector wants "Rule 18(2)".  A shopper standing in a queue wants to
know whether they are being overcharged and what to do about it.  Same
scan, same finding, two different renderings - and the second one has to
be in a language the shopper actually reads.

The mapping is keyed by rule ID, which is what keeps this a data problem.
Adding Kannada means adding a column to ``rights.json``; it does not mean
touching any code, and it cannot silently drift out of step with the rule
engine because the keys are the engine's own rule IDs.
"""

from __future__ import annotations

import json
import os

RIGHTS_PATH = os.path.join(os.path.dirname(__file__), "rights.json")

with open(RIGHTS_PATH, "r", encoding="utf-8") as fh:
    DATA = json.load(fh)

LANGUAGES = DATA["languages"]
DEFAULT_LANG = "en"


def normalise(lang):
    return lang if lang in LANGUAGES else DEFAULT_LANG


def ui(lang):
    """UI strings for the consumer view."""
    return DATA["ui"].get(normalise(lang), DATA["ui"][DEFAULT_LANG])


def _lookup(rule_id):
    """Find the rights entry for a rule ID, tolerating field suffixes.

    The engine emits IDs like ``FONT_TOO_SMALL_MRP`` and
    ``MISSING_CONSUMER_CARE``; the rights table keys the first as its family
    (``FONT_TOO_SMALL``) and the second exactly.  Try exact, then progressively
    shorter prefixes, then fall back to the generic entry so a shopper is
    never shown a blank panel.
    """
    table = DATA["rights"]
    if rule_id in table:
        return table[rule_id]
    parts = rule_id.split("_")
    for cut in range(len(parts) - 1, 0, -1):
        candidate = "_".join(parts[:cut])
        if candidate in table:
            return table[candidate]
    return table["DEFAULT"]


def explain(violation, lang=DEFAULT_LANG):
    """Turn one engine violation into a plain-language rights panel."""
    lang = normalise(lang)
    entry = _lookup(violation["rule_id"])
    text = entry.get(lang) or entry[DEFAULT_LANG]
    return {
        "rule_id": violation["rule_id"],
        "citation": violation["citation"],
        "severity": violation["severity"],
        "right": text["right"],
        "action": text["action"],
        "technical": violation["description"],
    }


def explain_all(violations, lang=DEFAULT_LANG, include_advisory=False):
    """Rights panels for a whole scan, de-duplicated by rule family."""
    seen, panels = set(), []
    for violation in violations:
        if violation["severity"] == "advisory" and not include_advisory:
            continue
        key = id(_lookup(violation["rule_id"]))
        if key in seen:
            continue
        seen.add(key)
        panels.append(explain(violation, lang))
    return panels
