"""Shared color palette for the report formats.

Extracted from the original XLSX writer so HTML and XLSX use the same hex
values -- single source of truth for the traffic-light palette.
"""

from __future__ import annotations


# Hex values without the leading '#'. openpyxl wants `fgColor` without '#';
# CSS / HTML wants the prefixed form. Helpers below produce each form.
HEADER_HEX = "305496"   # dark blue (table headers)
RED_HEX    = "FCE4E4"   # error background  (light red)
ORANGE_HEX = "FFE0B2"   # warning background (light orange)
GREEN_HEX  = "E2EFDA"   # pass / clean background


def css(name: str) -> str:
    """`#`-prefixed hex for CSS / HTML."""
    return "#" + {
        "header":  HEADER_HEX,
        "red":     RED_HEX,
        "orange":  ORANGE_HEX,
        "green":   GREEN_HEX,
    }[name]


def hex_(name: str) -> str:
    """Raw hex without `#` (for openpyxl PatternFill)."""
    return {
        "header":  HEADER_HEX,
        "red":     RED_HEX,
        "orange":  ORANGE_HEX,
        "green":   GREEN_HEX,
    }[name]
