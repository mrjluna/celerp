"""Connector cards and the connector page header draw an icon with icon(), never an emoji."""
from __future__ import annotations

import re

import pytest
from fasthtml.common import to_xml

from test_no_emoji_icons import _emoji
from ui.routes import settings_connectors as routes

KNOWN = ("shopify", "woocommerce", "quickbooks", "xero", "celerp")


def _card(cid: str) -> str:
    return to_xml(routes._connector_card(
        {"id": cid, "name": cid.title(), "auth_type": "api_key", "category": "accounting", "entities": []},
        None, "", "", lang="en"))


@pytest.mark.parametrize("cid", (*KNOWN, "some-new-platform"))
def test_every_connector_card_draws_an_svg_icon_and_no_emoji(cid):
    html = _card(cid)
    assert re.search(r'class="connector-icon"[^>]*>\s*<svg', html), html[:400]
    assert _emoji(html) == []


@pytest.mark.parametrize("cid", (*KNOWN, "some-new-platform"))
def test_connector_page_header_draws_an_svg_icon_and_no_emoji(cid):
    html = to_xml(routes._connector_title(cid, cid.title()))
    assert "<svg" in html and cid.title() in html
    assert _emoji(html) == []


def test_known_connectors_get_distinct_icons():
    drawn = {cid: to_xml(routes._connector_icon(cid)) for cid in KNOWN}
    assert len(set(drawn.values())) == len(KNOWN)
    assert to_xml(routes._connector_icon("some-new-platform")) not in drawn.values()


def test_module_metadata_icons_are_icon_names():
    """A nav or bulk-action "icon" in a module's metadata names an icon() icon, never an emoji."""
    import ast
    from pathlib import Path

    from ui.components.icons import icon_names

    root = Path(__file__).resolve().parents[1] / "default_modules"
    found = []
    for mod in ("celerp-manufacturing", "celerp-fixed-assets", "celerp-labels"):
        tree = ast.parse((root / mod / "__init__.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "icon" and isinstance(v, ast.Constant):
                        found.append(v.value)
    assert len(found) == 7
    assert set(found) <= set(icon_names()), found
