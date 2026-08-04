"""Unit tests for message templating: markdown → Matrix HTML, and `{placeholder}` substitution."""

from __future__ import annotations

import pytest

from onbot.formatting import placeholders_in, render_markdown, substitute


@pytest.mark.parametrize(
    "source",
    [
        "Welcome to the company chat.",
        "Ask @admin:matrix.test if anything is unclear.",
        "🛑 🔐 Save your Security Key Backup.",
    ],
)
def test_plain_text_needs_no_formatted_body(source: str) -> None:
    """Unformatted text stays exactly the event the bot sent before markdown support existed."""
    assert render_markdown(source) is None


def test_links_bold_and_code_render() -> None:
    html = render_markdown("Read the [guide](https://element.io/user-guide), it is **important** `now`")
    assert html == (
        '<p>Read the <a href="https://element.io/user-guide">guide</a>, '
        "it is <strong>important</strong> <code>now</code></p>"
    )


def test_bare_urls_are_linkified() -> None:
    """The shipped default messages carry plain URLs in running text; those should be clickable."""
    html = render_markdown("Have a look at https://element.io/user-guide")
    assert html is not None
    assert '<a href="https://element.io/user-guide">https://element.io/user-guide</a>' in html


def test_lists_render() -> None:
    html = render_markdown("- set up your backup\n- join a room")
    assert html == "<ul>\n<li>set up your backup</li>\n<li>join a room</li>\n</ul>"


def test_html_in_a_message_is_not_passed_through() -> None:
    """Raw HTML is off, so a message cannot smuggle tags into the client.

    ``None`` here is the strongest possible answer: the tags escaped to exactly the plain text, so
    there is no ``formatted_body`` at all and the client shows the angle brackets verbatim.
    """
    assert render_markdown("beware <img src=x onerror=alert(1)> and <b>bold</b>") is None


def test_dangerous_link_schemes_are_dropped() -> None:
    """markdown-it refuses the href, leaving the source as literal text rather than a live link."""
    assert render_markdown("[click me](javascript:alert(1))") is None
    # Alongside real formatting there *is* a formatted_body, but the scheme is inert text in it.
    html = render_markdown("see [x](javascript:alert(1)) and **this**")
    assert html == "<p>see [x](javascript:alert(1)) and <strong>this</strong></p>"
    assert "<a " not in html


def test_substitute_fills_known_placeholders() -> None:
    filled = substitute(
        "Hi {user_id}, see [{space_name}]({space_link})",
        {"user_id": "@alice:matrix.test", "space_name": "Space", "space_link": "https://matrix.to/#/x"},
    )
    assert filled == "Hi @alice:matrix.test, see [Space](https://matrix.to/#/x)"


@pytest.mark.parametrize(
    "template",
    [
        "budget is {0} and rising",  # a stray brace in free text
        "use {unknown_thing} here",  # a typo'd or future placeholder
        'a JSON snippet: {"a": 1}',
    ],
)
def test_substitute_leaves_anything_it_cannot_fill_alone(template: str) -> None:
    """Free text an operator wrote must never blow up mid-onboarding over a brace."""
    assert substitute(template, {"user_id": "@alice:matrix.test"}) == template


def test_placeholders_in_reports_names() -> None:
    assert placeholders_in("[{space_name}]({space_link}) for {user_id}") == {
        "space_name",
        "space_link",
        "user_id",
    }
