"""Turning the operator's message templates into what actually goes on the wire.

Two steps, both applied to configured message text just before it is sent:

* **Placeholders** — ``{space_link}`` and friends, filled in per user (:func:`substitute`).
* **Markdown** — rendered to the HTML a Matrix client displays (:func:`render_markdown`).

Matrix has no "this message is markdown" flag. The spec defines exactly one value for a message's
``format``, ``org.matrix.custom.html``, and tells clients to fall back to the plain ``body`` for
anything they do not recognise — so ``"format": "text/markdown"`` would reach Element as literal
asterisks. What Element's composer does is convert markdown to HTML locally *before* sending, and
that is what happens here: ``body`` keeps the markdown source (readable in a client that renders no
HTML at all), ``formatted_body`` carries the rendered HTML.

Rendering is CommonMark with raw HTML disabled, so a message cannot smuggle arbitrary tags past the
homeserver's clients, and every tag CommonMark itself emits is on Matrix's allowed list. markdown-it
additionally refuses ``javascript:``, ``vbscript:`` and ``data:`` hrefs.

https://spec.matrix.org/latest/client-server-api/#mroommessage-msgtypes
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml

# ``linkify`` turns bare URLs into links, which is what makes the shipped default welcome messages
# (plain URLs in running text) clickable without anybody rewriting them as [text](url).
_MD = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("linkify")

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render_markdown(source: str) -> str | None:
    """Render ``source`` to Matrix HTML, or ``None`` if markdown added nothing worth sending.

    Returning ``None`` for unformatted text keeps a plain message a plain message: no ``format`` and
    no ``formatted_body``, exactly the event the bot sent before markdown support existed.
    """
    html: str = _MD.render(source).strip()
    if html == f"<p>{escapeHtml(source.strip())}</p>":
        return None
    return html


def substitute(template: str, values: Mapping[str, str]) -> str:
    """Replace the ``{name}`` placeholders listed in ``values``, leaving every other brace alone.

    Deliberately not :meth:`str.format`: these templates are free text an operator wrote, and a
    stray ``{`` or a typo'd placeholder name must not raise in the middle of onboarding somebody.
    """
    return _PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), template)


def placeholders_in(template: str) -> set[str]:
    """The placeholder names ``template`` uses, so a caller can check it can fill them."""
    return {match.group(1) for match in _PLACEHOLDER.finditer(template)}
