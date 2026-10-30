"""An overlay element's drop shadow -- stored, round-tripped and drawn.

``OverlayElement.shadow`` draws the same text in black, offset down-right,
beneath the element's own text, so it stays readable over a busy video.  No
DC field holds it, so a layout without it must serialize exactly as before.
"""
from __future__ import annotations

from typing import Any

from trcc.core.models import OverlayElement
from trcc.services.overlay import (
    SHADOW_COLOR,
    SHADOW_MIN_OFFSET,
    OverlayService,
)

from .test_overlay_clock import _CLOCK, _DrawRecorder


def _render(element: dict[str, Any]) -> list[tuple]:
    """What the renderer was asked to draw for one *element*."""
    rec = _DrawRecorder()
    OverlayService(rec).render(
        rec.create_surface(320, 320),
        {"overlay_enabled": True, "elements": [element]},
        sensors={"cpu:temp": 42.0},
        clock=_CLOCK,
    )
    return rec.drawn


def test_to_dict_names_the_shadow_only_when_set() -> None:
    plain = OverlayElement(id="a", type="text", text="hi")
    shaded = OverlayElement(id="b", type="text", text="hi", shadow=True)

    assert "shadow" not in plain.to_dict()
    assert shaded.to_dict()["shadow"] is True


def test_from_dict_round_trips_the_shadow_and_defaults_it_off() -> None:
    shaded = OverlayElement(id="b", type="text", text="hi", shadow=True)

    assert OverlayElement.from_dict(shaded.to_dict()).shadow is True
    assert OverlayElement.from_dict({"id": "c", "type": "text"}).shadow is False


def test_a_shadowed_element_draws_the_shadow_first_in_black_at_the_offset() -> None:
    """MUTATION CHECK: draw the shadow after the text, or drop it, and this
    fails -- the shadow must be beneath, so it is the first draw."""
    drawn = _render({"type": "text", "text": "CPU", "x": 100, "y": 50,
                     "color": "#ff8fb1", "size": 32, "shadow": True})

    assert drawn == [
        (100 + SHADOW_MIN_OFFSET, 50 + SHADOW_MIN_OFFSET, "CPU",
         SHADOW_COLOR, 32, False, False),
        (100, 50, "CPU", "#ff8fb1", 32, False, False),
    ]


def test_the_offset_grows_with_the_text_size() -> None:
    """A twentieth of the size: 110 px text drops its shadow 5 px."""
    drawn = _render({"type": "clock", "source": "time", "x": 400, "y": 185,
                     "color": "#ffffff", "size": 110, "bold": True,
                     "shadow": True})

    assert [(x, y, color) for x, y, _t, color, *_rest in drawn] == [
        (405, 190, SHADOW_COLOR), (400, 185, "#ffffff")]


def test_every_element_kind_carries_the_shadow() -> None:
    """One draw site for text, metric and clock -- none may miss it."""
    for element in (
        {"type": "text", "text": "RAM"},
        {"type": "metric", "metric": "cpu:temp", "format": "{value:.0f}"},
        {"type": "clock", "source": "time"},
    ):
        drawn = _render({**element, "x": 10, "y": 10, "size": 20,
                         "shadow": True})
        assert [d[3] for d in drawn] == [SHADOW_COLOR, "#ffffff"], element


def test_a_gui_cell_keeps_the_element_s_shadow() -> None:
    """The gui edits through cells; a cell that dropped the shadow would send
    ``shadow=False`` back on the next whole-element add."""
    from trcc.core.results import OverlayElementEntry
    from trcc.ui.presentation.overlay_serialization import (
        config_fields,
        entries_to_configs,
    )

    (cell,) = entries_to_configs([OverlayElementEntry(
        id="t", type="text", text="NET", shadow=True)])
    fields = config_fields(cell)

    assert cell.shadow is True
    assert fields is not None and fields["shadow"] is True


def test_an_element_without_a_shadow_draws_once() -> None:
    drawn = _render({"type": "text", "text": "GPU", "x": 1, "y": 2,
                     "color": "#ffffff", "size": 32})

    assert drawn == [(1, 2, "GPU", "#ffffff", 32, False, False)]
