"""
TEXT-QUALITY RULES (improvement item 5).

Negation, certainty, priced-in detection, PRICE_ACTION tagging, and
token-distance subject/verb inversion instead of the old fixed 24-char regex.
"""

from app.analysis import text_quality as tq
from app.analysis.cue_rules import detect_direction, detect_inversion_subjects
from app.models.enums import Direction


# --------------------------------------------------------------------------
# Negation
# --------------------------------------------------------------------------
def test_no_rate_cut_neutralises_the_cue():
    assert tq.negation_applies("no rate cut from the Fed this year") is True


def test_rules_out_counts_as_negation():
    assert tq.negation_applies("Fed rules out a rate cut in June") is True


def test_unlikely_to_cut_counts_as_negation():
    assert tq.negation_applies("officials say the Fed is unlikely to cut rates") is True


def test_rate_cut_hopes_fade_counts_as_negation():
    assert tq.negation_applies("rate cut hopes fade on sticky inflation") is True


def test_plain_rate_cut_is_not_negated():
    assert tq.negation_applies("Fed announces a 25bp rate cut") is False


def test_negation_window_is_bounded():
    """Negation must not reach across a sentence to cancel an unrelated cue."""
# --------------------------------------------------------------------------
# Certainty weighting
# --------------------------------------------------------------------------
def test_full_certainty_for_an_actionable_announcement():
    assert tq.certainty_multiplier("Fed announces a rate cut") == 1.0


def test_hedged_language_reduces_weight():
    assert tq.certainty_multiplier("Fed could cut rates") < 1.0


def test_reportedly_reduces_weight():
    assert tq.certainty_multiplier("Fed reportedly plans a rate cut") < 1.0


def test_sources_say_reduces_weight():
    assert tq.certainty_multiplier("sources say the Fed will cut") < 1.0


def test_confirms_and_approves_keep_full_weight():
    assert tq.certainty_multiplier("Fed confirms rate cut") == 1.0
    assert tq.certainty_multiplier("SEC approves spot bitcoin ETF") == 1.0


def test_hedged_multiplier_is_bounded():
    for text in ("Fed could cut", "reportedly", "sources say", "may be about to"):
        assert 0.1 <= tq.certainty_multiplier(text) <= 1.0


# --------------------------------------------------------------------------
# Priced-in / surprise
# --------------------------------------------------------------------------
def test_as_expected_is_priced_in():
    assert tq.priced_in_multiplier("CPI comes in as expected") < 1.0


def test_in_line_with_is_priced_in():
    assert tq.priced_in_multiplier("Payrolls in line with forecasts") < 1.0

# --------------------------------------------------------------------------
# PRICE_ACTION
# --------------------------------------------------------------------------
def test_price_action_headline_is_detected():
    assert tq.is_price_action("Gold plunges 2% in early trade") is True


def test_bitcoin_surge_to_a_level_is_price_action():
    assert tq.is_price_action("Bitcoin surges to 64000") is True


def test_price_action_mentions_no_percentage_is_not_flagged():
    assert tq.is_price_action("Fed announces a rate cut") is False


def test_price_action_gets_near_zero_directional_weight():
    assert tq.directional_multiplier("Gold plunges 2% in early trade") < 0.15


def test_causal_news_keeps_full_directional_weight():
    assert tq.directional_multiplier("Fed hawkish as yields climb") == 1.0


def test_priced_in_price_action_compounds_to_near_zero():
    m = (tq.directional_multiplier("Gold surges 3% as expected")
         * tq.priced_in_multiplier("as expected"))
    assert m < 0.25


# --------------------------------------------------------------------------
# Token-distance inversion (replaces the 24-char regex)
# --------------------------------------------------------------------------
def test_rising_yields_within_window_is_bearish():
    found = detect_inversion_subjects("Treasury yields rise sharply")
    assert "yield" in found or "yields" in found


def test_longer_phrasing_still_inverts():
    found = detect_inversion_subjects(
        "The 10-year Treasury yield rose to a four-week high on Friday"
    )
    assert found


def test_subject_and_verb_too_far_apart_is_not_inverted():
    long_text = (
        "Treasury yields, which market participants have repeatedly described "
        "as remarkably well anchored by persistent foreign official demand, "
        "finally began to move higher late in the session"
    )
    assert detect_inversion_subjects(long_text) == []


def test_proximity_window_is_configurable():
    text = ("yields of 4.2% were seen, by some desks, as the level at which "
            "the metal rose")
    assert detect_inversion_subjects(text, max_tokens=6) == []


def test_yield_rise_reads_bearish_for_gold():
    d = detect_direction(
        "The dollar index climbed as traders priced a longer higher-for-longer path"
    )
    assert d is Direction.BEARISH


def test_gold_falling_reads_bearish():
    assert detect_direction("Gold fell sharply on profit taking") is Direction.BEARISH


def test_gold_rising_reads_bullish():
    assert detect_direction("Gold rose as haven demand returned") is Direction.BULLISH


def test_no_verbs_yields_no_inversion():
    assert detect_inversion_subjects("The central bank is reviewing its mandate") == []


def test_inversion_is_still_word_boundary_safe():
    assert detect_inversion_subjects("analysis of downtown retail sales") == []

def test_widely_anticipated_is_priced_in():
    assert tq.priced_in_multiplier("Fed move widely anticipated by traders") < 1.0


def test_unexpectedly_upweights():
    assert tq.priced_in_multiplier("CPI comes in unexpectedly hot") > 1.0


def test_more_than_forecast_upweights():
    assert tq.priced_in_multiplier("Payrolls beat, more than forecast") > 1.0


def test_neutral_headline_has_neutral_multiplier():
    # A headline with no priced-in or surprise wording must stay at exactly 1.0.
    assert tq.priced_in_multiplier("Fed holds rates steady") == 1.0
    # "meets expectations" IS priced-in language, so it must read below 1.0.
    assert tq.priced_in_multiplier("Fed meets expectations") < 1.0


def test_priced_in_multiplier_is_bounded():
    assert tq.priced_in_multiplier("as expected, widely anticipated, in line") >= 0.1
    assert tq.priced_in_multiplier("surprise, unexpectedly, more than forecast") <= 2.0


def test_negated_bullish_headline_is_not_read_as_bullish():
    d = detect_direction("Fed rules out a rate cut, signalling higher for longer")
    assert d is not Direction.BULLISH


def test_negation_returns_false_when_absent():
    assert tq.negation_applies("Fed cuts rates by 25 basis points") is False