"""The session speed slider covers every speed a character pack may set.

A range input clamps its value, so a narrower slider silently rewrites a
pack's default speed the first time the session settings are saved.
"""

from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

from galgame2voice.schemas.character_manifest import VoiceParamsConfig

INDEX = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


class _SliderFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attrs = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("id") == "sTtsSpeed":
            self.attrs = values


def _bound(metadata, name):
    for item in metadata:
        if hasattr(item, name):
            return float(getattr(item, name))
    raise AssertionError(f"speed has no {name} bound")


def test_speed_slider_spans_the_pack_speed_range():
    finder = _SliderFinder()
    finder.feed(INDEX.read_text(encoding="utf-8"))
    assert finder.attrs is not None
    metadata = VoiceParamsConfig.model_fields["speed"].metadata
    slider_min = Decimal(finder.attrs["min"])
    slider_max = Decimal(finder.attrs["max"])
    slider_step = Decimal(finder.attrs["step"])
    assert slider_min == Decimal(str(_bound(metadata, "ge")))
    assert slider_max == Decimal(str(_bound(metadata, "le")))
    assert slider_step > 0
    assert (Decimal("0.88") - slider_min) % slider_step == 0
    assert (slider_max - slider_min) % slider_step == 0
