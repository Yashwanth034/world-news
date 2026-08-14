"""Tests for the editorial eligibility filter, headline/body
consistency check and the article-content junk gate.

Run with:  .venv/bin/python -m pytest src/test_editorial.py -q
"""
from src.editorial import editorial_eligibility, is_editorial_junk
from src.telegram_summarizer import check_headline_consistency
from src.telegram_briefing import is_headline_paraphrase
from src.article_extractor import article_junk_ratio


def story(title, summary="A relevant body sentence explains more. "
           "Another sentence adds the consequence."):
    return {
        "title": title,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Editorial eligibility: junk is rejected, real news is accepted
# ---------------------------------------------------------------------------


class TestEditorialFilter:
    def test_product_review_rejected(self):
        assert editorial_eligibility(
            story(
                "Review: Ring Video Doorbell 5 Pro — better, "
                "but still not for everyone"
            )
        ) is False

    def test_buying_guide_rejected(self):
        assert editorial_eligibility(
            story(
                "Best portable air conditioners of 2026: "
                "tested and ranked"
            )
        ) is False

    def test_video_game_review_rejected(self):
        assert editorial_eligibility(
            story(
                "Elden Ring 2 review: a masterpiece of "
                "unforgiving design"
            )
        ) is False

    def test_opinion_column_rejected(self):
        assert editorial_eligibility(
            story(
                "Opinion: Why e-bikes are the future of "
                "urban transport"
            )
        ) is False

    def test_personal_essay_rejected(self):
        assert editorial_eligibility(
            story(
                "I quit my job to cycle across South America"
            )
        ) is False

    def test_how_to_guide_rejected(self):
        assert editorial_eligibility(
            story(
                "How to choose the right smartwatch for "
                "your budget"
            )
        ) is False

    def test_sponsored_content_rejected(self):
        assert editorial_eligibility(
            story(
                "Sponsored: the new flagship phone you "
                "should buy"
            )
        ) is False

    def test_evergreen_listicle_rejected(self):
        assert editorial_eligibility(
            story(
                "10 ways to save money on your energy bill"
            )
        ) is False

    def test_routine_weather_rejected(self):
        assert editorial_eligibility(
            story(
                "Weather forecast: sunny spells and showers "
                "this weekend"
            )
        ) is False

    def test_breaking_news_accepted(self):
        reasons = []
        assert editorial_eligibility(
            story(
                "Strong earthquake hits coastal city, "
                "buildings collapse",
                "A powerful earthquake struck the coast at "
                "dawn, damaging buildings and cutting power.",
            ),
            reasons,
        ) is True
        assert reasons == []

    def test_normal_current_news_accepted(self):
        assert editorial_eligibility(
            story(
                "France appoints new finance minister amid "
                "budget crisis",
                "Eric Lombard takes over the finance "
                "ministry this week.",
            )
        ) is True

    def test_meaningful_update_accepted(self):
        assert editorial_eligibility(
            story(
                "Wildfire update: crews gain control as "
                "winds ease",
                "Firefighters reported progress overnight "
                "as winds dropped.",
            )
        ) is True

    def test_plain_review_word_needs_product(self):
        # "court to review the ruling" is news, not a review.
        assert editorial_eligibility(
            story(
                "Court to review the regulator's ruling on "
                "the merger",
                "Judges will examine the decision next "
                "month.",
            )
        ) is True

    def test_extreme_weather_news_not_rejected(self):
        assert editorial_eligibility(
            story(
                "Weather alert: hurricane makes landfall "
                "with 140 mph winds",
                "The hurricane came ashore overnight, "
                "flooding coastal towns.",
            )
        ) is True

    def test_is_editorial_junk_shortcut(self):
        assert is_editorial_junk(
            story("Review: best laptops of 2026 compared")
        ) is True
        assert is_editorial_junk(
            story("Zelenskyy arrives in Serbia for talks")
        ) is False


# ---------------------------------------------------------------------------
# Headline / body consistency
# ---------------------------------------------------------------------------


class TestHeadlineConsistency:
    def test_headline_score_missing_from_source_rejected(self):
        ok, problems = check_headline_consistency(
            "City FC defeat United 3-1 in the final",
            "City FC beat United on penalties after a "
            "1-1 draw.",
        )
        assert not ok
        assert any("score" in p for p in problems)

    def test_headline_score_present_in_source_accepted(self):
        ok, problems = check_headline_consistency(
            "City FC defeat United 3-1 in the final",
            "City FC defeated United 3-1 in the final at "
            "the national stadium.",
        )
        assert ok
        assert problems == []

    def test_headline_win_source_loss_rejected(self):
        ok, problems = check_headline_consistency(
            "United win the derby in extra time",
            "United lost the derby in extra time.",
        )
        assert not ok
        assert any("win" in p for p in problems)

    def test_headline_killed_source_injured_rejected(self):
        ok, problems = check_headline_consistency(
            "Factory blast kills three workers",
            "Three workers were injured in the factory "
            "blast and are in hospital.",
        )
        assert not ok
        assert any("deaths" in p for p in problems)

    def test_headline_confirmed_source_reported_rejected(self):
        ok, problems = check_headline_consistency(
            "Government confirms new tax will take effect",
            "The government reportedly plans a new tax "
            "next year.",
        )
        assert not ok
        assert any("confirmed" in p for p in problems)

    def test_headline_announced_source_proposed_rejected(self):
        ok, problems = check_headline_consistency(
            "Company announces factory closure",
            "The company is considering closing the "
            "factory next quarter.",
        )
        assert not ok
        assert any("announced" in p for p in problems)

    def test_matching_headline_body_accepted(self):
        ok, problems = check_headline_consistency(
            "Three killed in refinery explosion",
            "An explosion at a refinery killed three "
            "workers on Thursday.",
        )
        assert ok
        assert problems == []


# ---------------------------------------------------------------------------
# Article-content junk gate
# ---------------------------------------------------------------------------


class TestArticleJunkRatio:
    def test_clean_article_low_ratio(self):
        text = (
            "Officials confirmed the crash on Saturday.\n"
            "Rescue teams reached the village by helicopter.\n"
            "The fire spread over more than 36 square miles.\n"
            "More than 20,000 residents were evacuated."
        )
        assert article_junk_ratio(text) < 0.5

    def test_chrome_heavy_page_high_ratio(self):
        text = (
            "Sign up for our newsletter\n"
            "Download our app for alerts\n"
            "Read more: related stories\n"
            "Subscribe now to continue reading\n"
            "The president left Belgrade on Sunday evening."
        )
        assert article_junk_ratio(text) > 0.5

    def test_empty_text_zero(self):
        assert article_junk_ratio("") == 0.0
        assert article_junk_ratio("   \n  ") == 0.0

    def test_caption_and_author_bio_counted_as_junk(self):
        text = (
            "Image caption: the damaged bridge after the "
            "storm\n"
            "Author: John Smith, senior correspondent\n"
            "The bridge collapsed during the storm on "
            "Tuesday."
        )
        assert article_junk_ratio(text) > 0.5


# ---------------------------------------------------------------------------
# Editorial false positives (real-audit cases that were wrongly rejected)
# ---------------------------------------------------------------------------


class TestEditorialFalsePositives:
    def test_buy_now_pay_later_accepted(self):
        # "Buy Now Pay Later" is a financial/industry term, not a
        # shopping call-to-action.
        assert editorial_eligibility(
            story(
                "Is Australia's Buy Now Pay Later boom at an end?",
                "Consumer spending on buy now pay later services "
                "has surged this year, alarming regulators.",
            )
        ) is True

    def test_buy_now_pay_later_variants_accepted(self):
        assert editorial_eligibility(
            story(
                "Regulators scrutinise Buy Now, Pay Later lenders",
                "BNPL providers face new oversight rules.",
            )
        ) is True

    def test_real_shopping_call_to_action_still_rejected(self):
        # The generic "buy now" pattern still catches real
        # shopping content.
        assert editorial_eligibility(
            story(
                "Buy now and save 50% on the new flagship phone",
                "Limited-time offer ends this week.",
            )
        ) is False

    def test_dating_app_company_news_accepted(self):
        # Merely mentioning a dating app is company/technology
        # news, not lifestyle content.
        assert editorial_eligibility(
            story(
                "Bumble divides users by ditching 'women-first' "
                "chat rule",
                "The dating app changed its messaging policy this "
                "week, angering some users.",
            )
        ) is True

    def test_dating_advice_still_rejected(self):
        # Explicit dating advice remains lifestyle.
        assert editorial_eligibility(
            story(
                "Dating tips: how to write a better profile",
                "Advice for dating app users everywhere.",
            )
        ) is False

    def test_eclipse_tips_accepted(self):
        # A human-interest story tied to a major current
        # astronomical event is newsworthy, not an evergreen
        # how-to.
        assert editorial_eligibility(
            story(
                "Astronomy superfan, 11, shares tips for watching "
                "solar eclipse",
                "The 11-year-old has followed eclipses for years "
                "and will watch this week's total eclipse.",
            )
        ) is True

    def test_eclipse_best_places_accepted(self):
        assert editorial_eligibility(
            story(
                "Best places to see partial solar eclipse across "
                "the country",
                "The partial eclipse will be visible this weekend "
                "from most regions.",
            )
        ) is True

    def test_eclipse_weather_forecast_accepted(self):
        assert editorial_eligibility(
            story(
                "Solar eclipse weather forecast: where skies will "
                "clear",
                "Forecasters expect clearer skies in the north for "
                "eclipse day.",
            )
        ) is True

    def test_how_to_guide_still_rejected(self):
        # The astronomy override is narrowly scoped: ordinary
        # how-to content is still rejected.
        assert editorial_eligibility(
            story(
                "How to choose the right smartwatch for your "
                "budget",
                "A buying guide with tips and comparisons.",
            )
        ) is False

    def test_routine_weather_still_rejected(self):
        assert editorial_eligibility(
            story(
                "Weather forecast: sunny spells and showers this "
                "weekend",
                "A routine outlook for the coming days.",
            )
        ) is False


# ---------------------------------------------------------------------------
# Low-value feature / analysis formats (dry-run audit)
# ---------------------------------------------------------------------------


class TestFeatureFormats:
    def test_transfer_gossip_rejected(self):
        # Audit case: a player "closes in on a transfer" is
        # speculation, not a completed transfer.
        assert editorial_eligibility(
            story(
                "Spain's World Cup final hero Ferran Torres "
                "closes in on PSG transfer"
            )
        ) is False

    def test_transfer_roundup_rejected(self):
        assert editorial_eligibility(
            story("Transfer gossip: the biggest rumours this week")
        ) is False

    def test_completed_transfer_accepted(self):
        # A done deal is major sports news, not gossip.
        assert editorial_eligibility(
            story(
                "Ferran Torres signs five-year deal with PSG",
                "The deal was confirmed on Thursday evening.",
            )
        ) is True

    def test_why_x_matters_analysis_rejected(self):
        assert editorial_eligibility(
            story(
                "Why Osun's election matters for Nigeria's 2027 "
                "vote"
            )
        ) is False

    def test_profile_piece_rejected(self):
        assert editorial_eligibility(
            story("Meet the main contenders in Zambia's vote")
        ) is False

    def test_commemoration_rejected(self):
        assert editorial_eligibility(
            story(
                "Cuba marks 100th anniversary of Fidel Castro's "
                "birth amid energy crisis"
            )
        ) is False

    def test_how_x_are_tackling_feature_rejected(self):
        assert editorial_eligibility(
            story(
                "Tougher crops and beaver dams: how UK farmers "
                "are tackling drought"
            )
        ) is False

    def test_soft_poll_data_rejected(self):
        assert editorial_eligibility(
            story(
                "Poll finds most Britons prefer working from home",
                "A survey of 2,000 workers found strong support "
                "for hybrid arrangements.",
            )
        ) is False

    def test_question_headline_explainer_rejected(self):
        # Audit case: an AJLabs explainer whose headline is only a
        # question, and the user's solar-mission example.  A
        # question headline asserts no event, so it cannot carry
        # current news.
        assert editorial_eligibility(
            story(
                "What is a 'ceasefire' supposed to achieve?",
                "Gaza, Lebanon, Iran, Ukraine: four ceasefires, "
                "none that held.",
            )
        ) is False
        assert editorial_eligibility(
            story(
                "Why is the Sun's corona millions of degrees "
                "hotter than its surface?",
                "New mission data may answer the question.",
            )
        ) is False

    def test_emotional_profile_piece_rejected(self):
        # Audit case: "The Gambian women turning grief into song"
        # is a culture profile, not current news.
        assert editorial_eligibility(
            story(
                "The Gambian women turning grief into song",
                "Known as the Kanyeleng, they have transformed "
                "their experience of child loss through a "
                "tradition of music.",
            )
        ) is False

    def test_consumer_how_to_cancel_rejected(self):
        # Audit case: a first-person subscription-refund how-to.
        assert editorial_eligibility(
            story(
                "I got an £89 refund – how to cancel and avoid "
                "unwanted subscriptions",
                "Readers share how they got into and out of "
                "unwanted plans.",
            )
        ) is False


class TestFeatureFormatsKeepNews:
    """The feature rules must never reject legitimate current
    news: science, finance, environment, elections and disasters."""

    def test_science_discovery_accepted(self):
        assert editorial_eligibility(
            story(
                "Hidden moth population found after 70 years",
                "The discovery has given conservationists new "
                "hope for the species.",
            )
        ) is True
        assert editorial_eligibility(
            story(
                "Indian solar mission's new findings throw light "
                "on enduring Sun mysteries"
            )
        ) is True

    def test_financial_news_accepted(self):
        assert editorial_eligibility(
            story(
                "US long-term borrowing costs rise to 25-year "
                "high, as inflation fears hit bond sale",
                "The US sold 30-year bonds at the highest "
                "borrowing costs since 2001.",
            )
        ) is True

    def test_environment_news_accepted(self):
        assert editorial_eligibility(
            story(
                "Record rainfall leaves four dead, thousands "
                "stranded in Japan",
                "Record rainfall in Chiba claimed at least four "
                "lives and stranded thousands at the airport.",
            )
        ) is True

    def test_disaster_news_accepted(self):
        assert editorial_eligibility(
            story(
                "Strong earthquake hits coastal city, buildings "
                "collapse",
                "A powerful earthquake struck the coast at dawn "
                "on Thursday.",
            )
        ) is True

    def test_heatwave_market_story_accepted(self):
        # A data-led consumer-market story about heatwaves is
        # current news, not a lifestyle feature: the new "how to
        # cancel" and question rules must not over-reach.
        assert editorial_eligibility(
            story(
                "Searches for UK homes with air conditioning "
                "more than double",
                "Rightmove says demand has more than doubled this "
                "summer as heatwaves push staying cool up the "
                "priority list for homebuyers.",
            )
        ) is True


# ---------------------------------------------------------------------------
# Opinion false negatives (formats the filter must recognize)
# ---------------------------------------------------------------------------


class TestOpinionFormats:
    def test_pipe_letter_suffix_rejected(self):
        assert editorial_eligibility(
            story(
                "A timely warning on the rise of the 'tech "
                "bros' | Letter",
                "A reader responds to the tech industry debate.",
            )
        ) is False

    def test_letter_prefix_rejected(self):
        assert editorial_eligibility(
            story(
                "Letter: our schools need more funding",
                "A reader argues for more school funding.",
            )
        ) is False

    def test_readers_letters_rejected(self):
        assert editorial_eligibility(
            story(
                "Readers' letters: the future of the high street",
                "Our readers share their views on high streets.",
            )
        ) is False

    def test_letters_page_rejected(self):
        assert editorial_eligibility(
            story(
                "Letters page: what you told us about the "
                "election",
                "A round-up of reader correspondence.",
            )
        ) is False

    def test_letters_to_the_editor_rejected(self):
        assert editorial_eligibility(
            story(
                "Letters to the editor: NHS reform",
                "Readers write in about NHS reform.",
            )
        ) is False

    def test_plain_letter_word_in_news_accepted(self):
        # A news story about a letter (e.g. an open letter from
        # officials) is not an opinion piece.
        assert editorial_eligibility(
            story(
                "US officials send letter demanding action on "
                "migrant crisis",
                "The letter was signed by 20 lawmakers.",
            )
        ) is True

    def test_op_ed_rejected(self):
        assert editorial_eligibility(
            story(
                "Op-Ed: why Europe must rearm now",
                "An opinion piece on European defence.",
            )
        ) is False

    def test_guest_essay_rejected(self):
        assert editorial_eligibility(
            story(
                "Guest essay: the loneliness of remote work",
                "A contributor reflects on working from home.",
            )
        ) is False


# ---------------------------------------------------------------------------
# Headline/body repetition (near-verbatim restatements must be dropped)
# ---------------------------------------------------------------------------


class TestHeadlineRepetition:
    def test_air_india_restatement_detected(self):
        # Confirmed audit case: the body restates the headline
        # with only a grammatical change and filler.
        assert is_headline_paraphrase(
            "Air India is to test all of its pilots for alcohol",
            "Air India to test all pilots for alcohol",
        ) is True

    def test_air_india_filler_restatement_detected(self):
        assert is_headline_paraphrase(
            "Air India is now set to test every single one of its "
            "pilots",
            "Air India to test all pilots",
        ) is True

    def test_synonym_restatement_detected(self):
        assert is_headline_paraphrase(
            "The earthquake killed 100 people",
            "Quake kills 100",
        ) is True

    def test_inflection_restatement_detected(self):
        assert is_headline_paraphrase(
            "Police have arrested 78 people in a smuggling bust",
            "Police arrest 78 people in smuggling bust",
        ) is True

    def test_body_adding_region_not_repetition(self):
        # The body naturally contains the same key entity but
        # adds genuinely new information - never a paraphrase.
        assert is_headline_paraphrase(
            "The quake struck the country's western region after "
            "heavy rainfall",
            "Earthquake kills 100 in Colombia",
        ) is False

    def test_body_adding_reason_not_repetition(self):
        assert is_headline_paraphrase(
            "French cyclist and defending champion Pauline "
            "Ferrand-Prevot pulled out of the Women's Tour de "
            "France ahead of Saturday's stage due to feeling "
            "unwell",
            "Women's Tour de France: Defending champion Pauline "
            "Ferrand-Prevot pulls out of race",
        ) is False
