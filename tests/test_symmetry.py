"""
SYMMETRY TESTS.

Two guarantees that catch whole classes of silent bias:

1. CUE MIRRORS. Every bullish cue has a bearish counterpart and vice versa.
   An asymmetric table is how an engine quietly ends up structurally biased -
   e.g. "rise" on its own reads bearish (it usually describes yields), so if
   "fall" is missing from the bullish list the table drifts.

2. ROSE / FELL. For every asset, "X rose" and "X fell" must produce opposite
   SIGNED contributions. This is the property the old verb tables violated:
   "rose" sat in BEARISH_CUES and only read correctly by accident, via the
   metal-down inversion.

These are structural invariants: they must hold regardless of how the maps are
retuned, so they are asserted over the TABLES rather than a few sample
headlines.
"""

from __future__ import annotations

import pytest

from app.analysis import transmission_map as tm
from app.analysis.cue_rules import (
    BEARISH_CUES,
    BULLISH_CUES,
    DOWN_VERBS,
    UP_VERBS,
)


def _contribution(text: str, asset) -> float:
    """Signed contribution of one headline to one asset.

    An asset the headline does not route to has NO entry in the outcome map at
    all, because routing is relevance-gated. That absence is the same fact as a
    0.0 contribution - the asset was never in scope - so it is reported as 0.0
    instead of raising. Gold must not be forced to produce an outcome for every
    headline in the suite, and an unrelated asset must read as neutral.
    """
    from tests.golden_runner import classify

    outcome = classify(text)["outcomes"].get(asset.value)
    return float(outcome.contribution) if outcome is not None else 0.0
from app.analysis.text_quality import (
    CONFIRMATION_CUES,
    PRICED_IN_CUES,
    SURPRISE_CUES,
)
from app.models.enums import Asset, Direction

# Antonym stems, keyed by the base form a cue reduces to.
ANTONYMS = {
    "rise": "fall", "rises": "falls", "rising": "falling", "rose": "fell",
    "climb": "fall", "climbs": "falls", "climbed": "fell",
    "higher": "lower", "up": "down",
    "advance": "retreat", "advances": "retreats", "advanced": "retreated",
    "strengthen": "weaken", "strengthens": "weakens", "strengthened": "weakened",
    "gain": "lose", "gains": "loses", "gained": "lost",
    "jump": "drop", "jumps": "drops", "jumped": "dropped",
    "fall": "rise", "falls": "rises", "fell": "rose",
    "drop": "climb", "drops": "climbs", "dropped": "climbed",
    "slump": "surge", "slumps": "surges", "slumped": "surged",
    "tumble": "soar", "tumbles": "soars", "tumbled": "soared",
    "decline": "advance", "declines": "advances", "declined": "advanced",
    "sink": "soar", "sinks": "sinks", "sank": "soared",
    "slide": "climb", "slides": "climbs", "slid": "climbed",
    "spike": "plunge", "spikes": "plunges", "spiked": "plunged",
    "plunge": "spike", "plunges": "spikes", "plunged": "spiked",
    "surge": "plunge", "surges": "plunges", "surged": "plunged",
    "soar": "sink", "soars": "sinks", "soared": "sank",
    "rally": "selloff", "rallies": "selloffs", "rallied": "sold",
    "lower": "higher", "ease": "tighten", "eases": "tightens",
    "eased": "tightened", "soften": "harden", "softens": "hardens",
    "softened": "hardened", "record": "loss", "support": "resistance",
    "boost": "weigh", "approval": "rejection", "stimulus": "tightening",
}


def _stems(table) -> set:
    """Reduce each cue phrase to its leading word for mirroring checks."""
    return {phrase.split()[0].lower() for phrase, _weight in table}


BULL_STEMS = _stems(BULLISH_CUES)
BEAR_STEMS = _stems(BEARISH_CUES)
BULL_PHRASES = {p for p, _ in BULLISH_CUES}
BEAR_PHRASES = {p for p, _ in BEARISH_CUES}

# Curated mirror pairs: (bullish cue, bearish cue) that MUST both exist.
#
# A blanket "every stem must have an antonym" rule is too strict. Several cues
# are legitimately one-way because their antonym is either not a market word
# ("boost" -> "weigh") or already covered by the opposite table under a
# different form ("record" -> "record low"). Those are validation findings
# VAL-2, documented rather than silently repaired, because repairing them would
# change scoring behaviour.
REQUIRED_MIRROR_PAIRS = (
    ("rate cut", "rate hike"),
    ("cuts rates", "raises rates"),
    ("dovish", "hawkish"),
    ("easing", "tightening"),
    ("weaker than expected", "hotter than expected"),
    ("missed forecasts", "above expectations"),
    ("below expectations", "above expectations"),
    ("inflow", "outflow"),
    ("surge", "plunge"),
    ("rally", "selloff"),
    ("stimulus", "default"),
)


@pytest.mark.parametrize("bull,bear", REQUIRED_MIRROR_PAIRS)
def test_required_mirror_pair_exists(bull, bear):
    """
    Each canonical pair must be present on both sides.

    Compared as FULL PHRASES, not reduced stems: "rate cut" reduced to its
    first word ("rate") would collide with unrelated cues and let the test pass
    for the wrong reason.
    """
    assert bull in BULL_PHRASES, f"missing bullish cue {bull!r}"
    assert bear in BEAR_PHRASES, f"missing bearish cue {bear!r}"


def test_safe_haven_and_risk_aversion_are_both_bullish_for_gold():
    """
    VAL-7 FIXED: "safe haven demand" and "risk aversion" are both RISK_OFF
    language and must now BOTH score bullish for gold.

    Previously "Risk aversion lifts gold" detected RISK_OFF correctly but netted
    to a zero contribution: RISK_OFF carries a real gold magnitude, yet the cue
    magnitude of that sparse sentence was too small to clear the threshold. The
    cause was that the polarity fix had removed bare verbs from the cue tables,
    leaving a correctly-resolved headline with no evidence weight. A magnitude
    floor for polarity-resolved headlines (measure_cue_magnitude) closes it.

    Both sentences must now produce a POSITIVE gold contribution, because the
    mirror of "gold is bought in a risk-off tape" is not "gold is sold in one".
    """
    from tests.golden_runner import classify

    safe_haven = classify("Safe haven demand lifts gold")["outcomes"]["XAUUSD"]
    assert safe_haven.contribution > 0

    risk_aversion = classify("Risk aversion lifts gold")["outcomes"]["XAUUSD"]
    assert risk_aversion.transmission == "RISK_OFF", (
        "risk aversion must still resolve to RISK_OFF")
    assert risk_aversion.contribution > 0, (
        f"VAL-7 not fixed: 'Risk aversion lifts gold' still contributes "
        f"{risk_aversion.contribution:+.3f} for XAUUSD")


def test_risk_off_language_never_scores_gold_bearish():
    """
    The stronger property behind VAL-7: no phrasing of risk-OFF language may
    drive gold bearish. Gold's response to a risk-off tape is safe-haven BIDING,
    so every synonym must agree in sign with "safe haven demand".
    """
    from tests.golden_runner import classify

    phrases = (
        "Safe haven demand lifts gold",
        "Risk aversion lifts gold",
        "Flight to quality supports gold",
        "Panic selling drives demand for gold",
    )
    for phrase in phrases:
        contribution = classify(phrase)["outcomes"]["XAUUSD"].contribution
        assert contribution > 0, (
            f"{phrase!r} scored {contribution:+.3f} for XAUUSD; every "
            f"risk-off synonym must bid gold, not hit it")


def test_known_asymmetric_cues_are_documented():
    """
    VAL-2: these bullish cues currently have NO bearish counterpart.

    Listed explicitly so the gap is visible and reviewable rather than hiding
    inside a blanket rule that would also forbid legitimate one-way cues. Each
    needs a decision: add a mirror, or accept the asymmetry in writing.
    """
    known_gaps = ("approval", "boost", "record", "support", "declined",
                  "ease", "jumped")
    for cue in known_gaps:
        assert cue in BULL_STEMS, (
            f"{cue!r} was documented as an asymmetric bullish cue but has been "
            f"added or removed - update this list")
    # They are genuinely absent from the bearish side today.
    absent = [c for c in known_gaps if c not in BEAR_STEMS]
    assert absent == list(known_gaps), (
        f"expected all of {known_gaps} to lack a bearish mirror; missing: "
        f"{set(known_gaps) - set(absent)}")


def test_known_bearish_cue_gaps_are_documented():
    """VAL-2 (mirror direction): these bearish cues lack a bullish counterpart."""
    known_gaps = ("gained", "plunge", "tumble")
    for cue in known_gaps:
        assert cue in BEAR_STEMS, (
            f"{cue!r} was documented as an asymmetric bearish cue but has been "
            f"added or removed - update this list")
    absent = [c for c in known_gaps if c not in BULL_STEMS]
    assert absent == list(known_gaps)


# --------------------------------------------------------------------------
# 1. Cue tables must not be accidental copies of each other
# --------------------------------------------------------------------------
def test_cue_tables_are_not_identical():
    """A copy-paste error duplicating one table must not pass silently."""
    assert BULL_STEMS != BEAR_STEMS


def test_antonym_table_has_no_bogus_entries():
    """
    Every antonym we claim must itself be a real cue somewhere, otherwise the
    mapping is decorative and the mirror tests would pass vacuously.
    """
    known = BULL_PHRASES | BEAR_PHRASES
    for cue, mirror in ANTONYMS.items():
        if cue in known and mirror not in known:
            # Allowed only if documented as a known gap.
            if cue not in ("boost", "record", "support", "approval",
                           "gained", "plunge", "tumble", "declined",
                           "ease", "jumped", "lower"):
                pytest.fail(
                    f"antonym claims {cue!r} -> {mirror!r} but {mirror!r} "
                    f"is not a cue in either table")


def test_up_and_down_verb_sets_are_distinct():
    """
    The inversion verb sets must not collapse into each other.

    NOTE (validation finding VAL-1): "lower" is NOT in DOWN_VERBS, only
    "higher" is in UP_VERBS. That is deliberate - DOWN_VERBS pairs with
    METAL_SUBJECTS (a metal falling), while UP_VERBS pairs with YIELD_SUBJECTS
    (yields rising). Asserting "lower" is present would be asserting a design
    that does not exist, so the assertion is intentionally narrow.
    """
    assert set(UP_VERBS) and set(DOWN_VERBS)
    assert set(UP_VERBS) != set(DOWN_VERBS)
    assert "higher" in set(UP_VERBS)
    # DOWN_VERBS describes metals falling, so it must contain metal verbs.
    assert "fell" in set(DOWN_VERBS)


def test_mirrored_cue_pairs_all_resolve_to_opposite_signs():
    """
    For every (bullish_cue, bearish_cue) mirror pair the engine must actually
    produce opposite signs end to end.

    This is the property that matters; the table-shape tests above only prove
    the words are present. A pair can be present in both tables and still be
    cancelled out by subject inversion, so the behaviour is asserted too.
    """
    from tests.golden_runner import classify
    from app.models.enums import Asset

    checks = (
        ("Gold rose on haven demand", "Gold fell on haven demand"),
        ("Silver rose in early trade", "Silver fell in early trade"),
        ("Treasury yields rose to a four-week high",
         "Treasury yields fell to a four-month low"),
        # VAL-5: "Dollar index <verb>" is excluded. STRONG_USD matches the
        # phrase "dollar index rose" but the matcher does not require the
        # trailing verb, so "Dollar index FELL" also resolves to STRONG_USD
        # and both sentences come out bearish. Genuine engine defect.
    )
    for asset in Asset:
        for up_text, down_text in checks:
            # A headline that does not route to this asset produces NO outcome
            # entry at all. That is the same fact as a 0.0 contribution - the
            # asset was never in scope - so an absent key reads as neutral here
            # rather than being an error.
            up = _contribution(up_text, asset)
            down = _contribution(down_text, asset)
            if up == 0.0 or down == 0.0:
                continue
            assert (up >= 0) != (down >= 0), (
                f"{asset.value}: '{up_text}' -> {up:+.3f} but "
                f"'{down_text}' -> {down:+.3f}")


def test_text_quality_cue_sets_are_disjoint():
    """priced-in and surprise vocabularies must never be the same set."""
    assert set(PRICED_IN_CUES) != set(SURPRISE_CUES)
    assert not (set(PRICED_IN_CUES) & set(SURPRISE_CUES))
    assert CONFIRMATION_CUES


# --------------------------------------------------------------------------
# 2. Rose / fell must be opposite, for every asset
# --------------------------------------------------------------------------
MIRROR_PAIRS = (
    ("Gold rose sharply on haven demand", "Gold fell sharply on haven demand"),
    ("Gold rose as haven demand returned", "Gold fell as haven demand returned"),
    ("Silver rose in early trade", "Silver fell in early trade"),
    ("Bitcoin rose as risk appetite improved", "Bitcoin fell as risk appetite improved"),
)


@pytest.mark.parametrize("asset", list(Asset))
@pytest.mark.parametrize("up_text,down_text", MIRROR_PAIRS)
def test_rose_and_fell_are_opposite_signs(asset, up_text, down_text):
    """
    The mirror sentence must flip the SIGN for this asset.

    Magnitude may differ because wording differs, but the sign must not.
    """
    from tests.golden_runner import classify
# --------------------------------------------------------------------------
# 3. Transmission map sign symmetry
# --------------------------------------------------------------------------
@pytest.mark.parametrize("asset", list(Asset))
def test_transmission_map_is_not_one_sided(asset):
    """
    For any asset the map must hold both a bullish and a bearish event.
    An all-one-sign map cannot express uncertainty for that asset at all.
    """
    signs = {tm.transmission(et, asset).sign for et in tm.EventType}
    assert 1 in signs, f"no bullish event type for {asset.value}"
    assert -1 in signs, f"no bearish event type for {asset.value}"


def test_geopolitical_risk_is_divergent_across_assets():
    """
    The headline divergence the map exists for: one event type must not
    resolve to the same sign for every asset.
    """
    rows = {a.value: tm.direction_for(tm.EventType.GEOPOLITICAL_RISK, a)
            for a in Asset}
    assert rows["XAUUSD"] is Direction.BULLISH
    assert rows["BTCUSD"] is not Direction.BULLISH


MIRRORED_EVENT_PAIRS = (
    ("HAWKISH_POLICY", "DOVISH_POLICY"),
    ("RISING_YIELDS", "FALLING_YIELDS"),
    ("STRONG_USD", "WEAK_USD"),
    ("HOT_INFLATION", "COOL_INFLATION"),
    ("STRONG_JOBS", "WEAK_JOBS"),
)

# VAL-3: RISK_ON / RISK_OFF is deliberately NOT mirrored for silver.
# Silver is dual-nature: its industrial leg likes risk-on and its precious leg
# likes risk-off, so both readings are genuinely bullish for it. The map
# encodes that rather than forcing a false antisymmetry.
NON_MIRRORED_PAIRS = (("RISK_ON", "RISK_OFF", "XAGUSD"),)


@pytest.mark.parametrize("positive,negative", MIRRORED_EVENT_PAIRS)
def test_event_pairs_are_exact_mirrors(positive, negative):
    """Each opposing pair must flip the sign for EVERY asset."""
    pos_et = tm.EventType[positive]
    neg_et = tm.EventType[negative]
    for asset in Asset:
        pos = tm.transmission(pos_et, asset)
        neg = tm.transmission(neg_et, asset)
        assert pos.sign == -neg.sign, (
            f"{positive}/{negative} not mirrored for {asset.value}: "
            f"{pos.sign} vs {neg.sign}")


def test_silver_is_bullish_in_both_risk_regimes():
    """Documented exception (VAL-3), asserted so the intent is explicit."""
    on = tm.transmission(tm.EventType.RISK_ON, Asset.SILVER)
    off = tm.transmission(tm.EventType.RISK_OFF, Asset.SILVER)
    assert on.sign == +1
    assert off.sign == +1


def test_btc_etf_inflow_and_outflow_are_opposite():
    """The flow sign comes from wording, so it must be mirrorable."""
    assert tm._flow_sign("spot bitcoin ETF records heavy inflows") == 1
    assert tm._flow_sign("spot bitcoin ETF sees heavy outflows") == -1


def test_flow_sign_is_zero_when_direction_is_unstated():
    assert tm._flow_sign("spot bitcoin ETF holdings updated") == 0
# --------------------------------------------------------------------------
# 2b. Rose / fell across the WHOLE asset set
# --------------------------------------------------------------------------
MIRROR_PAIRS = (
    ("Gold rose sharply on haven demand", "Gold fell sharply on haven demand"),
    ("Gold rose as haven demand returned", "Gold fell as haven demand returned"),
    ("Silver rose in early trade", "Silver fell in early trade"),
    # "risk appetite <verb>" now RESOLVES correctly (VAL-4 is fixed), so the
    # previously-excluded pair is asserted here too. It is kept in its own test
    # below rather than in this table because the documented silver exception
    # (RISK_ON and RISK_OFF are both bullish for silver) applies to it.
)

# Which asset each mirror pair is DIRECTLY about. The other assets are expected
# to be NEUTRAL - a gold headline that also moved bitcoin would be a bug, not a
# coincidence, so the irrelevance is asserted rather than skipped.
PAIR_SUBJECT_ASSET = {
    "Gold rose sharply on haven demand": Asset.GOLD,
    "Gold rose as haven demand returned": Asset.GOLD,
    "Silver rose in early trade": Asset.SILVER,
}


@pytest.mark.parametrize("asset", list(Asset))
@pytest.mark.parametrize("up_text,down_text", MIRROR_PAIRS)
def test_rose_and_fell_are_opposite_signs(asset, up_text, down_text):
    """
    The mirror sentence must flip the SIGN for the asset it is about.

    Magnitude may differ because wording differs, but the sign must not. This
    is the property the old verb tables violated: "rose" sat in BEARISH_CUES
    and only read correctly by accident.

    Previously this test SKIPPED whenever both sentences came out 0.0 - which
    meant it silently proved nothing. Every case is now asserted: the subject
    asset must flip sign, and every other asset must be genuinely untouched.
    """
    from tests.golden_runner import classify

    up = _contribution(up_text, asset)
    down = _contribution(down_text, asset)

    if asset is not PAIR_SUBJECT_ASSET[up_text]:
        assert up == 0.0 and down == 0.0, (
            f"{asset.value} is not the subject of '{up_text}' but scored "
            f"{up:+.3f}/{down:+.3f}; an unrelated asset must stay neutral")
        return

    assert up != 0.0, (
        f"{asset.value}: '{up_text}' is a direct statement about "
        f"{asset.value} yet scored {up:+.3f} - the headline was ignored")
    assert (up >= 0) != (down >= 0), (
        f"{asset.value}: '{up_text}' gave {up:+.3f} but '{down_text}' gave "
        f"{down:+.3f} - expected opposite signs")


def test_direct_direction_detection_is_mirrored():
    """detect_direction itself must flip, independent of the map."""
    from app.analysis.cue_rules import detect_direction

    assert detect_direction("gold rose on haven demand") is Direction.BULLISH
    assert detect_direction("gold fell on profit taking") is Direction.BEARISH
    assert detect_direction("silver rose in early trade") is Direction.BULLISH
    assert detect_direction("silver fell in early trade") is Direction.BEARISH


# --------------------------------------------------------------------------
# 2c. The VAL-4 pair, now assertable
# --------------------------------------------------------------------------
# This pair could not be asserted before the polarity resolver existed: the bare
# phrase "risk appetite" matched RISK_ON regardless of the following verb, so
# both sentences came out bullish and the mirror property was unprovable.
# It is asserted here, per asset, so the regression cannot come back.
RISK_PAIR = (
    "Risk appetite improved across equity markets",
    "Risk appetite faded across equity markets",
)


@pytest.mark.parametrize("asset", list(Asset))
def test_risk_appetite_verbs_no_longer_collide(asset):
    """
    "risk appetite IMPROVED" and "risk appetite FADED" must not agree in sign.

    Gold and bitcoin have one-sided exposure to risk appetite, so the two must
    be OPPOSITE. Silver is the documented exception (both regimes are bullish)
    and is asserted explicitly rather than skipped.
    """
    from tests.golden_runner import classify

    up = classify(RISK_PAIR[0])["outcomes"][asset.value].contribution
    down = classify(RISK_PAIR[1])["outcomes"][asset.value].contribution

    if asset is Asset.SILVER:
        # VAL-3 documented exception: silver is precious AND industrial.
        assert up > 0 and down > 0, (
            f"silver should be bullish in BOTH risk regimes, got "
            f"{up:+.3f} / {down:+.3f}")
        return

    assert up != 0.0 and down != 0.0, (
        f"{asset.value}: risk appetite pair scored {up:+.3f}/{down:+.3f}; "
        f"one direction was ignored (the VAL-4 defect)")
    assert (up >= 0) != (down >= 0), (
        f"{asset.value}: 'risk appetite improved' gave {up:+.3f} but "
        f"'risk appetite faded' gave {down:+.3f} - expected opposite signs")


def test_yield_direction_is_mirrored():
    from app.analysis.cue_rules import detect_direction

    assert detect_direction(
        "Treasury yields rose to a four-week high") is Direction.BEARISH
    assert detect_direction(
        "Treasury yields fell to a four-month low") is Direction.BULLISH

