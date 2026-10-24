"""Voice rules, including the evidence rule: no number or name the cited facts don't contain."""

import pytest

from lead_engine.voice import EXAMPLE, Voice, check_opener, grade, load_voice

FOUNDED = {"id": "f3", "kind": "founded", "value": {"year": "2009"},
           "quote": "Dana Whitlock founded Harbor Ledger Co. in 2009 after ten years as a restaurant controller."}
VOICE = Voice(max_characters=200, max_sentences=2, max_grade=9.0, banned_phrases=["I came across", "amazing"])


def check(text, cited=(FOUNDED,), recent=None, voice=VOICE, first_name="Dana"):
    return check_opener(text, voice=voice, cited=list(cited), company="Harbor Ledger Co.", market="Cedar Hollow, OR",
                        first_name=first_name, recent=recent)


def test_a_good_opener_passes():
    assert check("Running Harbor Ledger Co. since 2009 is a long stretch for any small business.") == []


@pytest.mark.parametrize("text,problem", [
    ("", "empty"),
    ("Running the firm since 2009 is a long stretch. " * 6, "characters"),
    ("Since 2009 \u2014 that is a long stretch.", "em dash"),
    ("Hi there, since 2009 is a long stretch.", "greeting"),
    ("Since {year} is a long stretch.", "placeholder"),
    ("Since 2009, see https://example.com for more.", "link"),
    ("I came across your firm, running since 2009.", "banned phrase"),
    ("Dana, running the firm since 2009 is a long stretch.", "first name"),
    ("Since 2009. That is long. Really long.", "sentences"),
    ("Institutional organizational administrative responsibilities accumulate continuously since 2009.",
     "reading level"),
])
def test_each_style_rule(text, problem):
    problems = check(text)
    assert any(problem in p for p in problems), problems


def test_invented_numbers_and_names_are_caught():
    problems = check("Running the firm since 2011 is a long stretch.")
    assert problems == ["the number 2011 is not in the cited evidence"]
    problems = check("Twelve years running the books in Lakeside is a long stretch.")
    assert '"twelve" is not in the cited evidence' in problems and '"Lakeside" is not in the cited evidence' in problems
    assert check("Ten years as a restaurant controller before 2009 is real experience.") == []  # all in the quote


@pytest.mark.parametrize("number", ["20", "200", "9", "20.09"])
def test_partial_numeric_matches_are_not_evidence(number):
    assert "the number %s is not in the cited evidence" % number in check("You have %s years in business." % number)


@pytest.mark.parametrize("text", ["Your 20th year in business is here.", "You have 20k clients.",
                                 "You have 3.5m clients.", "You have 20years at this."])
def test_digit_suffixes_cannot_evade_numeric_evidence(text):
    assert check(text)


def test_alphanumeric_company_names_remain_usable_when_quoted_exactly():
    assert check_opener("Shop42 has been at it since 2009.", voice=VOICE, cited=[FOUNDED], company="Shop42") == []


@pytest.mark.parametrize("number", ["20th", "20k"])
def test_numeric_suffixes_require_the_complete_published_form(number):
    fact = {**FOUNDED, "quote": "Our %s year is here and we have %s clients." % (number, number)}
    assert check("Your %s year is here." % number, cited=[fact]) == []
    assert check("Your 21st year is here.", cited=[fact])


def test_decimal_suffixes_cannot_be_assembled_from_separate_numbers():
    fact = {**FOUNDED, "quote": "We have 4.5m clients across 3 towns."}
    assert check("You have 3.5m clients.", cited=[fact])
    assert check("You have 4.5m clients.", cited=[fact]) == []


def test_numeric_comparisons_normalize_grouping_and_decimal_values():
    fact = {**FOUNDED, "quote": "We have 1,200 clients and 2.5 years in business."}
    assert check("You have 1200 clients.", cited=[fact]) == []
    assert check("You have 2.50 years in business.", cited=[fact]) == []
    assert check("You have 2 years in business.", cited=[fact])


def test_complete_numbers_in_the_company_name_are_allowed():
    assert check_opener("Shop 42 has been at it since 2009.", voice=VOICE, cited=[FOUNDED], company="Shop 42") == []


def test_company_and_market_words_are_allowed_but_an_opener_must_cite_a_fact():
    assert check("A Cedar Hollow firm since 2009 is a long stretch.") == []
    assert check("A Cedar Hollow firm is nice.", cited=()) == ["opener cites no fact"]


def test_a_company_name_with_a_period_is_one_sentence():
    assert check("Harbor Ledger Co. has been at it since 2009, and that kind of staying power is rare.") == []


def test_openings_must_differ_from_recent_openers():
    recent = ["Running Brightline Books since 2014 is a long stretch."]
    problems = check("Running Harbor Ledger Co. since 2009 is a long stretch.", recent=recent,
                     voice=Voice(distinct_opening_words=2, distinct_window=5))
    assert problems == []  # different company names make different openings
    problems = check("Running a small firm since 2009 is a long stretch.", recent=["Running a small shop is hard."],
                     voice=Voice(distinct_opening_words=3))
    assert problems == ['opener starts like a recent one: "running a small"']


def test_grade_is_flesch_kincaid():
    assert grade("The cat sat on the mat.") < 2
    assert grade("Institutional responsibilities accumulate continuously.") > 15


def test_the_example_voice_file_loads_and_its_templates_pass():
    voice = load_voice(EXAMPLE)
    values = {"founded": {"year": "2009"}, "serving": {"place": "Cedar Hollow", "year": "2012"},
              "specialty": {"specialty": "bookkeeping for restaurants and cafes"},
              "award": {"award": "Best Bookkeeper", "year": "2025"}}
    for kind, templates in voice.templates.items():
        fact = {"id": "f1", "kind": kind, "value": values[kind],
                "quote": "Harbor Ledger Co. %s" % " ".join(str(v) for v in values[kind].values())}
        for t in templates:
            ctx = {"company": "Harbor Ledger Co.", "city": "Cedar Hollow", **values[kind]}
            ctx.update({k + "_cap": str(v)[:1].upper() + str(v)[1:] for k, v in values[kind].items()})
            text = t.format(**ctx)
            assert check_opener(text, voice=voice, cited=[fact], company="Harbor Ledger Co.",
                                market="Cedar Hollow, OR", first_name="Dana") == [], text


def test_unknown_voice_keys_are_rejected(tmp_path):
    p = tmp_path / "voice.toml"
    p.write_text("max_characters = 200\nmax_lenght = 3\n")
    with pytest.raises(ValueError, match="max_lenght"):
        load_voice(p)


def test_a_first_name_inside_the_company_name_is_not_the_greeting():
    fact = {**FOUNDED, "quote": "Dana's Books opened in 2009."}
    problems = check_opener("Dana's Books has been at it since 2009.", voice=VOICE, cited=[fact],
                            company="Dana's Books", first_name="Dana")
    assert problems == []
