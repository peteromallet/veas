from app.services.open_asks import OpenAsk, _get_bot_asks, render_open_asks


def test_both_edd_and_partner_share_open_render_with_examples() -> None:
    from app.bots.prompts.tante_rosi import ASKS

    rendered = render_open_asks(
        ASKS,
        {
            "pregnancy_edd": None,
            "partner_share": None,
            "has_partner": True,
            "partner_name": "Hannah",
        },
    )

    assert "## Open asks" in rendered
    assert "`pregnancy_edd` is not set." in rendered
    assert "Glückwunsch. Damit ich dich gut begleiten kann" in rendered
    assert "Resolves with: `set_pregnancy_edd`" in rendered
    assert "`partner_share` is not set." in rendered
    assert "Willst du, dass ich Hannah ab und zu sage" in rendered
    assert "Resolves with: `set_partner_sharing`" in rendered


def test_edd_set_partner_share_null_renders_only_partner_share() -> None:
    from app.bots.prompts.tante_rosi import ASKS

    rendered = render_open_asks(
        ASKS,
        {
            "pregnancy_edd": "2026-12-01",
            "partner_share": None,
            "has_partner": True,
            "partner_name": "Hannah",
        },
    )

    assert "`pregnancy_edd` is not set." not in rendered
    assert "`partner_share` is not set." in rendered


def test_both_set_returns_empty_string() -> None:
    from app.bots.prompts.tante_rosi import ASKS

    rendered = render_open_asks(
        ASKS,
        {
            "pregnancy_edd": "2026-12-01",
            "partner_share": "opt_in",
            "has_partner": True,
        },
    )

    assert rendered == ""


def test_has_partner_false_suppresses_partner_share() -> None:
    from app.bots.prompts.tante_rosi import ASKS

    rendered = render_open_asks(
        ASKS,
        {
            "pregnancy_edd": "2026-12-01",
            "partner_share": None,
            "has_partner": False,
        },
    )

    assert rendered == ""


def test_empty_asks_list_returns_empty_string() -> None:
    assert render_open_asks([], {}) == ""


def test_partner_name_substitution() -> None:
    ask = OpenAsk(
        key="partner_share",
        open_if=lambda state: True,
        example="Can I share context with {partner_name}?",
        resolves_with="set_partner_sharing",
    )

    rendered = render_open_asks([ask], {"partner_name": "Hannah"})

    assert "Hannah" in rendered
    assert "{partner_name}" not in rendered


def test_registry_resolves_tante_rosi_mediator_and_unknown() -> None:
    from app.bots.prompts.tante_rosi import ASKS as ROSI_ASKS
    from app.services.prompts import VEAS_ASKS
    from app.services.prompts_solo import ASKS as SOLO_ASKS

    assert _get_bot_asks("tante_rosi") is ROSI_ASKS
    assert _get_bot_asks("mediator") is VEAS_ASKS
    assert _get_bot_asks("unknown") is SOLO_ASKS


# ── T6: SuperPOM calibration asks ──────────────────────────────────────

def test_superpom_receives_superpom_asks_not_solo_asks():
    from app.services.open_asks import SUPERPOM_ASKS, _get_bot_asks
    from app.services.prompts_solo import ASKS as SOLO_ASKS

    result = _get_bot_asks("superpom")
    assert result is SUPERPOM_ASKS
    assert result is not SOLO_ASKS


def test_superpom_asks_has_exactly_seven_items():
    from app.services.open_asks import SUPERPOM_ASKS

    assert len(SUPERPOM_ASKS) == 7


def test_superpom_asks_keys_match_calibration_slots():
    from app.services.open_asks import SUPERPOM_ASKS

    expected_keys = {
        "principle",
        "goal",
        "priority",
        "anti_pattern",
        "strength",
        "tension",
        "question",
    }
    actual_keys = {ask.key for ask in SUPERPOM_ASKS}
    assert actual_keys == expected_keys


def test_superpom_asks_all_have_example_and_resolves_with():
    from app.services.open_asks import SUPERPOM_ASKS

    for ask in SUPERPOM_ASKS:
        assert ask.example, f"Ask {ask.key} has no example"
        assert len(ask.example) > 20, (
            f"Ask {ask.key} example too short: {len(ask.example)} chars"
        )
        assert ask.resolves_with, f"Ask {ask.key} has no resolves_with"
        assert "create_orientation_item" in ask.resolves_with, (
            f"Ask {ask.key} resolves_with missing create_orientation_item"
        )


def test_superpom_asks_mention_source_user_stated():
    from app.services.open_asks import SUPERPOM_ASKS

    for ask in SUPERPOM_ASKS:
        assert "source='user_stated'" in ask.example or "source=user_stated" in ask.example, (
            f"Ask {ask.key} missing source='user_stated' guidance"
        )


def test_superpom_asks_mention_source_bot_proposed():
    from app.services.open_asks import SUPERPOM_ASKS

    for ask in SUPERPOM_ASKS:
        assert "source='bot_proposed'" in ask.example or "source=bot_proposed" in ask.example, (
            f"Ask {ask.key} missing source='bot_proposed' guidance"
        )


def test_superpom_asks_mention_label_prefix():
    from app.services.open_asks import SUPERPOM_ASKS

    for ask in SUPERPOM_ASKS:
        assert "SuperPOM -" in ask.example, (
            f"Ask {ask.key} missing SuperPOM label prefix in example"
        )


def test_superpom_asks_all_open_when_nothing_filled():
    from app.services.open_asks import SUPERPOM_ASKS, render_open_asks

    # Empty state — no calibration slots filled.
    state = {}
    rendered = render_open_asks(SUPERPOM_ASKS, state)
    # All seven should be open.
    assert rendered.count("is not set.") == 7


def test_superpom_asks_principle_filled_suppresses_principle_ask():
    from app.services.open_asks import SUPERPOM_ASKS, render_open_asks

    state = {"principle_filled": True}
    rendered = render_open_asks(SUPERPOM_ASKS, state)
    assert "`principle` is not set." not in rendered
    # Other six should still be open.
    assert rendered.count("is not set.") == 6


def test_superpom_asks_all_filled_returns_empty():
    from app.services.open_asks import SUPERPOM_ASKS, render_open_asks

    state = {
        "principle_filled": True,
        "goal_filled": True,
        "priority_filled": True,
        "anti_pattern_filled": True,
        "strength_filled": True,
        "tension_filled": True,
        "question_filled": True,
    }
    rendered = render_open_asks(SUPERPOM_ASKS, state)
    assert rendered == ""


def test_unknown_bot_still_falls_back_to_solo():
    from app.services.open_asks import _get_bot_asks
    from app.services.prompts_solo import ASKS as SOLO_ASKS

    # Unknown bots should still receive SOLO_ASKS.
    assert _get_bot_asks("unknown_bot") is SOLO_ASKS
    assert _get_bot_asks("coach") is SOLO_ASKS


def test_superpom_asks_each_has_review_guidance():
    from app.services.open_asks import SUPERPOM_ASKS

    for ask in SUPERPOM_ASKS:
        example = ask.example.lower()
        assert "review" in example or "ask for review" in example, (
            f"Ask {ask.key} missing review guidance"
        )
