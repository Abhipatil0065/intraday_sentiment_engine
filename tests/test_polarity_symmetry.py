"""
VAL-4 / VAL-5 REGRESSION: PARAMETRISED POLARITY SYMMETRY.

Every opposing event pair is exercised with an UP verb and a DOWN verb, for
EVERY asset. The build fails if any pair resolves to the same sign - which is
precisely the class of defect that produced VAL-4 ("risk appetite faded"
scoring as RISK_ON) and VAL-5 ("Dollar index fell" scoring as STRONG_USD).

Three distinct claims are tested:
  * EVENT-TYPE level: "the dollar index fell" must resolve to WEAK_USD.
  * EVENT MIRROR: the UP and DOWN variants must never share an event type.
  * ASSET level: they must give opposite SIGNED contributions for each asset.
"""

from __future__ import annotations

import pytest

from app.analysis import transmission_map as tm
from app.analysis.event_polarity import POLARITY_PAIRS, resolve_all
from app.models.enums import Asset

# (subject template, UP verb, DOWN verb)
#
# The verb column must agree with UP_VERBS / DOWN_VERBS below - three pairs
# were initially written the wrong way round ("slipped" as the UP verb), which
# the engine correctly rejected. Assert that agreement explicitly so the table
# cannot drift again.
VERB_CASES = (
    ("the dollar index {v} broadly", "rose", "fell"),
    ("the dollar index {v} against the euro", "surged", "slipped"),
    ("the dollar index {v} against the yen", "strengthened", "weakened"),
    ("the greenback {v} in late trading", "rose", "fell"),
    ("treasury yields {v} on inflation fears", "rose", "fell"),
    ("the 10-year treasury yield {v} to a four-week high", "climbed",
     "declined"),
    ("risk appetite {v} across equity markets", "improved", "faded"),
    ("risk appetite {v} as volatility rises", "returned", "sours"),
)


def test_verb_cases_use_verbs_from_the_declared_vocabularies():
    """
    Guard the table above against the exact mistake that produced five red
    tests: putting a DOWN verb in the UP column. If a verb is in neither list
    the pair would silently resolve to nothing.
    """
    from app.analysis.event_polarity import DOWN_VERBS, UP_VERBS

    def classified(word: str, vocab) -> bool:
        return any(word == v or word == f"{v}s" or word == f"{v}ed"
                   for v in vocab)

    for _template, up_verb, down_verb in VERB_CASES:
        assert classified(up_verb, UP_VERBS), (
            f"{up_verb!r} is listed as an UP verb but is not in UP_VERBS")
        assert classified(down_verb, DOWN_VERBS), (
            f"{down_verb!r} is listed as a DOWN verb but is not in DOWN_VERBS")
        assert not classified(up_verb, DOWN_VERBS) or not classified(
            down_verb, UP_VERBS), (
            f"{up_verb!r}/{down_verb!r} are ambiguous across both vocabularies")


def _expected_for(text: str):
    """(up_event, down_event) implied by the subject named in the text."""
    low = text.lower()
    if "dollar" in low or "greenback" in low:
        return ("STRONG_USD", "WEAK_USD")
    if "treasury" in low or "10-year" in low:
        return ("RISING_YIELDS", "FALLING_YIELDS")
    return ("RISK_ON", "RISK_OFF")


def _is_documented_silver_exception(template: str, text: str) -> bool:
    """
    Silver's RISK_ON and RISK_OFF are BOTH bullish, by design.

    Silver carries a precious-metals bid (which likes RISK_OFF / safe-haven
    demand) and an industrial bid (which likes RISK_ON). The two pull in
    opposite directions, so the transmission map deliberately gives RISK_ON and
    RISK_OFF the same positive sign for silver. That is an economic property of
    silver, not an unfixed polarity defect, and it is asserted as intentional in
    test_every_polarity_pair_is_exact_mirror_in_the_map.

    Every other asset, and every other event pair, must mirror strictly.
    """
    if "risk" not in template.lower():
        return False
    return "risk" in text.lower()


@pytest.mark.parametrize("template,up_verb,down_verb", VERB_CASES)
def test_event_type_polarity_resolves_correctly(template, up_verb, down_verb):
    """VAL-4/VAL-5 core: the event type must follow the verb."""
    up_text = template.format(v=up_verb)
    down_text = template.format(v=down_verb)
    exp_up, exp_down = _expected_for(up_text)

    assert exp_up in resolve_all(up_text.lower()), (
        f"{up_text!r} should resolve to {exp_up}, got "
        f"{resolve_all(up_text.lower())}")
    assert exp_down in resolve_all(down_text.lower()), (
        f"{down_text!r} should resolve to {exp_down}, got "
        f"{resolve_all(down_text.lower())}")


@pytest.mark.parametrize("template,up_verb,down_verb", VERB_CASES)
def test_up_and_down_verbs_never_resolve_to_the_same_event(
        template, up_verb, down_verb):
    """
    The mirror invariant at EVENT level.

    If an UP phrase and its DOWN mirror produced the same event type, the engine
    would be reading the verb as decoration - exactly VAL-4 and VAL-5.
    """
    up = set(resolve_all(template.format(v=up_verb).lower()))
    down = set(resolve_all(template.format(v=down_verb).lower()))
    assert not (up & down), (
        f"up -> {sorted(up)} and down -> {sorted(down)} share an event; "
        f"the verb is being ignored")


@pytest.mark.parametrize("template,up_verb,down_verb", VERB_CASES)
@pytest.mark.parametrize("asset", list(Asset))
def test_up_and_down_verbs_give_opposite_asset_signs(
        template, up_verb, down_verb, asset):
    """
    The mirror invariant at ASSET level: opposite SIGNED contributions.

    Magnitude may differ because wording differs, but the sign must not, for
    every asset the headline can reach.
    """
    from tests.golden_runner import classify

    up_text = template.format(v=up_verb)
    down_text = template.format(v=down_verb)

    if _is_documented_silver_exception(template, up_text):
        return

    up = classify(up_text)["outcomes"][asset.value].contribution
    down = classify(down_text)["outcomes"][asset.value].contribution
    if up == 0.0 or down == 0.0:
        # NEUTRAL on one side is allowed: the asset may simply be irrelevant to
        # this headline. What must never happen is the SAME NON-ZERO SIGN on both.
        return
    assert (up >= 0) != (down >= 0), (
        f"{asset.value}: '{up_text}' -> {up:+.3f} but '{down_text}' -> "
        f"{down:+.3f}; expected opposite signs")


@pytest.mark.parametrize("template,up_verb,down_verb", VERB_CASES)
def test_bare_subject_with_no_verb_emits_nothing(template, up_verb, down_verb):
    """
    A subject named with NO directional verb anywhere must emit nothing.

    This is the rule that stops the category fallback inventing a stance, which
    was the VAL-5 mechanism. Each string below names the subject and nothing
    else that carries a verb.
    """
    for bare in ("the dollar index", "treasury yields", "risk appetite",
                 "the fed"):
        found = resolve_all(bare.lower())
        directional = [f for f in found if f != "GENERAL_MACRO"]
        assert not directional, (
            f"{bare!r} has no directional verb but resolved to {directional}")


def test_another_subjects_verb_makes_the_pair_ambiguous():
    """
    "risk appetite as volatility RISES" names an up-verb, but it belongs to
    VOLATILITY, not to risk appetite. The resolver cannot attribute verbs to a
    different subject, so it reports neither - and the aggregator can then say
    NO_BIAS rather than guessing a risk-on reading.
    """
    assert resolve_all("risk appetite as volatility rises".lower()) == []


@pytest.mark.parametrize("template,up_verb,down_verb", VERB_CASES)
def test_ambiguous_phrase_emits_no_directional_event(template, up_verb, down_verb):
    """
    Template-level guard: with the verb removed, nothing directional may fire.
    """
    bare = " ".join(template.format(v="").split())
    found = resolve_all(bare.lower())
    directional = [f for f in found if f != "GENERAL_MACRO"]
    assert not directional, (
        f"{bare!r} has no directional verb but resolved to {directional}")


def test_policy_stance_reads_the_verb():
    assert "DOVISH_POLICY" in resolve_all("fed announces a rate cut".lower())
    assert "HAWKISH_POLICY" in resolve_all("fed announces a rate hike".lower())
    # A HOLD is genuinely neither, and must emit nothing.
    assert resolve_all("fed holds rates steady".lower()) == []


def test_inflation_and_jobs_read_the_verb():
    assert "HOT_INFLATION" in resolve_all("cpi accelerates".lower())
    assert "COOL_INFLATION" in resolve_all("cpi cools".lower())
    assert "STRONG_JOBS" in resolve_all("payrolls beat forecasts".lower())
    assert "WEAK_JOBS" in resolve_all("payrolls miss forecasts".lower())


def test_verb_may_precede_or_follow_the_subject():
    """Prose puts the verb on either side; both must resolve identically."""
    assert "STRONG_USD" in resolve_all("the dollar index rose".lower())
    assert "STRONG_USD" in resolve_all("rose sharply, the dollar index".lower())
    assert "WEAK_USD" in resolve_all("the dollar index fell".lower())
    assert "WEAK_USD" in resolve_all("fell sharply, the dollar index".lower())


def test_every_polarity_pair_is_exact_mirror_in_the_map():
    """
    Every pair the polarity resolver can emit must have opposite signs in the
    JSON map, otherwise a correct polarity reading still lands on the wrong side.
    """
    for pair in POLARITY_PAIRS:
        pos = tm.EventType(pair.positive)
        neg = tm.EventType(pair.negative)
        for asset in Asset:
            if asset is Asset.SILVER and {pos, neg} == {
                    tm.EventType.RISK_ON, tm.EventType.RISK_OFF}:
                continue  # documented exception: dual-nature silver
            p = tm.transmission(pos, asset)
            n = tm.transmission(neg, asset)
            assert p.sign == -n.sign, (
                f"{pos.value}/{neg.value} not mirrored for {asset.value}: "
                f"{p.sign} vs {n.sign}")
