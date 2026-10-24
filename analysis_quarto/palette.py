"""The page palette: the map's five hazard colours and the charts' five-hue set.

One source of truth for the colour of every data mark on the page. The map
ladder names one class per hue, from the neutral end to the magenta end:

=========== ============ =================================================
token       hex          meaning
=========== ============ =================================================
Slate Grey  ``#8b9199``  nothing classified
Amber       ``#e5ae1e``  hazard-listed, no carcinogenicity class
Orange      ``#ec7c26``  possibly carcinogenic
Crimson     ``#e0495f``  probably carcinogenic
Orchid      ``#b25cb5``  carcinogenic to humans
=========== ============ =================================================

The **map** paints the hazard ladder with :data:`MAP_FILLS`, one fill per level,
in table order: a neutral grey for a facility whose substances carry no
classification, orchid for one releasing a known carcinogen. The five are
separate hues that warm from grey through amber and orange to crimson and then
turn cool again at orchid, and neighbouring classes are dE 79, 33, 48 and 55
apart in CIELAB, so the class of a dot is read off its hue rather than guessed
from how dark it is. Against the land the fills run from 1.96:1 (amber) to
4.02:1 (orchid), but a dot is never drawn opaque: the map fills every dot at
75%, which leaves 1.66:1 to 2.79:1, and the edge is what carries the mark.

Five hues do not keep their order under red-green colour vision deficiency the
way a single ramp would: amber and orange collapse to dE 11.7 under deuteranopia
(both read as yellow) and 20.2 under protanopia, and the neutral grey against
orchid is dE 24.2 under deuteranopia. That is the trade this ladder makes
against the ink ramp before it, which held its order under deuteranopia but
separated its two palest steps by only dE 5.7. What the hue ladder buys is the
thing a map of eight thousand dots needs: the class of a dot is a hue and not a
shade, and the one pair that does collapse under deuteranopia is the two
neighbouring warm steps, whose own dark edges, printed counts and hover cards
still separate them.

Every dot and swatch is outlined with :func:`outline`, the same hue taken 45%
towards black: at 75% fill opacity a translucent mark on a pale land needs an
edge of its own family, and a deeper shade of the fill keeps the hue (the two
warmest steps would otherwise share the same slate).

The **charts** rank substances, so their slots are nominal and take five separate
hues, one per stack slot (:data:`SERIES`): Bubblegum Pink (``#ef476f``) for the
largest slice, then Golden Pollen (``#ffd166``), Emerald (``#06d6a0``), Ocean
Blue (``#118ab2``) and Dark Teal (``#073b4c``), with the pooled bucket in
:data:`OTHER`. Colour therefore means *rank in the ranking on screen*, not
substance identity. That is the only assignment that cannot collide inside a
legend: the three views draw from different substance pools, so one global
name-to-colour table repeats itself - and it did, with dichloromethane and
chloroform, the first and the fourth carcinogen by mass, carrying the same tint.

Separate hues separate the slots in a way the ink ramp before them could not:
neighbouring slots stay at least dE 34.8 apart in normal vision, 33.7 under
deuteranopia and 34.8 under protanopia, where that ramp bottomed out at 15. The
weakest pair of the five is not a neighbouring one - Bubblegum Pink against
Emerald is dE 18.4 under deuteranopia - and two of the five are pale on the white
panel, Golden Pollen at 1.4:1 and Emerald at 1.9:1, so a slice takes its extent
from the slices around it and the legend swatch keeps its inset ring.

The two schemes stay apart by *figure*, not by hue. The map keeps the
grey-to-orchid hazard ladder and the charts take the five series hues, so no
token changes meaning between figures - but the two sets are closer than the
ink ramp ever was: the map's Crimson against Bubblegum Pink (``#ef476f``) is
dE 8.8, and its Amber against Golden Pollen (``#ffd166``) is dE 19.0. They never
share a figure, a legend or a hover card, and they are drawn as different marks:
a dot with a dark edge on the land here, a bar segment with a labelled legend
there. A reader who wants the two sets further apart would move the chart's
largest-slice hue, not the ladder, because the ladder's hue has to stay the
class of thousands of dots.
"""

from __future__ import annotations

#: The map ladder, from the neutral end (nothing classified) to the magenta end
#: (carcinogenic to humans).
SLATE_GREY = "#8b9199"
AMBER = "#e5ae1e"
ORANGE = "#ec7c26"
CRIMSON = "#e0495f"
ORCHID = "#b25cb5"

#: The chart set: one hue per stack slot, in stack order. Bubblegum Pink marks
#: the largest slice, Dark Teal the fifth; the pooled bucket takes :data:`OTHER`.
BUBBLEGUM_PINK = "#ef476f"
GOLDEN_POLLEN = "#ffd166"
EMERALD = "#06d6a0"
OCEAN_BLUE = "#118ab2"
DARK_TEAL = "#073b4c"


def _rgb(value: str) -> tuple[int, int, int]:
    """Split ``#rrggbb`` into its three channels."""
    text = value.lstrip("#")
    return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))


def mix(start: str, end: str, share: float) -> str:
    """Blend two colours in sRGB; ``share`` 0 keeps ``start``, 1 gives ``end``."""
    first, second = _rgb(start), _rgb(end)
    channels = tuple(
        round(a + (b - a) * share) for a, b in zip(first, second)
    )
    return "#%02x%02x%02x" % channels


def outline(fill: str, share: float = 0.45) -> str:
    """Outline for a fill: the same hue, ``share`` of its light taken away.

    Marks on the map are 1.4 to 9 px across and are filled at 75% opacity, so a
    step that sits close to the surface value needs an edge in its own family;
    one shared ink stroke at low opacity would veil every class instead of
    separating it. The pull goes towards black, which scales the three channels
    together and therefore leaves the hue alone: a pull towards a dark neutral
    would drag orange and magenta onto the same slate, and the ladder's whole
    point is that the classes do not share a hue. 45% puts every edge past
    5.7:1 against the land and 4.7:1 against the sea - the amber step is the
    weakest of the five - where its fill is 1.96 and 1.63.
    """
    return mix(fill, "#000000", share)


#: The hazard ladder, indexed by level (0 = nothing classified ... 4 = IARC 1).
MAP_FILLS = (SLATE_GREY, AMBER, ORANGE, CRIMSON, ORCHID)

#: The outline of each level, same indexing as :data:`MAP_FILLS`: a deeper shade
#: of the fill itself, so the edge never changes the hue the fill encodes.
MAP_OUTLINES = tuple(outline(fill) for fill in MAP_FILLS)

#: Chart series, one colour per stack slot, in stack order. Five slots is what
#: the page's rankings stack before the pool; a longer stack would need a sixth
#: hue, and the legend is written to fail loudly instead (see
#: :func:`facility_chart_data.view_colors`).
SERIES = (BUBBLEGUM_PINK, GOLDEN_POLLEN, EMERALD, OCEAN_BLUE, DARK_TEAL)

#: The pooled bucket: the only achromatic mark in the charts, so "everything
#: else" reads as a remainder rather than as one more ranked substance. It is as
#: pale as the panel on purpose; its legend swatch keeps the inset ring. (The
#: map's Slate Grey is the next nearest value, dE 24.7 away, and the two never
#: appear in the same figure.)
OTHER = "#ced4d9"
