"""Data and geometry payload for the EU facility map (React + D3 front end).

The page draws the map with ``assets/facility-map.js``: Europe as a base map,
every E-PRTR facility as a proportional circle coloured by the strongest hazard
classification it reports, the municipality outlines as a locator layer, and a
hover box that reports the total of the selected view, the substances behind
that total with their classifications, and the IED record of the facility.

Two kinds of artefact, both served as static files from ``assets/map/`` so the
page itself stays small:

* ``facilities.json`` - written by :func:`write_payload` on every build: the
  facility table, the LAU aggregates, the classification descriptions and the
  legend/scale settings;
* ``countries.json`` and ``lau.json`` - written once by :func:`build_geo` from
  the GISCO LAU file: simplified outlines, already projected to EPSG:3035 and
  quantised, so the browser needs no projection and no topojson decoder.

Commands::

    python facility_map_data.py             # rebuild assets/map/facilities.json
    python facility_map_data.py --geo       # rebuild the base geometry (GDAL)
    python facility_map_data.py --all       # both; --force ignores the cache

``--geo`` needs GDAL's ``ogr2ogr`` (the system ``python3`` has it, the Quarto
venv does not), so it is a one-off step: the geometry only changes when the
GISCO release changes. The payload step runs on every render through
``project.pre-render`` in ``_quarto.yml``, but it writes only what changed: the
payload is rebuilt from ``eprtr_enhanced.csv`` when the extract, the LAU cache or
one of the modules below it is newer than ``facilities.json``, and a render that
changed nothing leaves the file - and its mtime - alone (``--force`` rebuilds it
anyway).

Views
-----
The three views carry the same keys as the facility ranking chart, so switching
the map and the chart tells the same story:

* ``releases``     - every pollutant except CO2, summed 2022-2024;
* ``hazardous``    - only pollutants flagged as hazardous, greenhouse gases out;
* ``carcinogenic`` - every substance the hazard ladder calls carcinogenic at
  all (Possibly, Probably or Known), greenhouse gases out.

The carcinogenic view is deliberately wider than the carcinogenic *ranking*,
which keeps only IARC Group 1/2A: the map colours every facility by the ladder
below, so a facility whose strongest class is "Possibly carcinogenic" has to be
on the map too, or the two palest carcinogenic classes of the legend would be
empty. The map therefore draws several thousand facilities in that view where
the ranking charts a few hundred.

Media
-----
Every release row carries the medium it was reported to (``TargetRelease``:
``AIR`` or ``WATER``). The map exposes it twice: each view is measured
separately for air, water and both, so the toggle rescales the dots and the
totals, and the hover box names the medium of every substance and the air/water
split of the facility total.

Facility level
--------------
Each facility takes the strongest classification any of its substances carries,
which is what colours the circle:

===== ========================== ==========================================
level name                       assigned when one substance ...
===== ========================== ==========================================
0     No classified substance    carries no classification at all
1     Hazard-listed              is HAP, SVHC or WFD priority, no Carc. class
2     Possibly carcinogenic      is IARC 2B or CLP ``Carc. 2``
3     Probably carcinogenic      is IARC 2A or CLP ``Carc. 1B``
4     Carcinogenic to humans     is IARC 1 or CLP ``Carc. 1A``
===== ========================== ==========================================

LAU layer
---------
LAU means *local administrative unit* (municipality, LAU 2). Each facility is
matched to its LAU through ``LAU/lau_lookup.csv``, the point-in-polygon result
of the GISCO LAU 2024 file (see :func:`build_lau_lookup`). The layer is drawn as
outlines only - the map does not shade it, so the circles keep the whole colour
budget - and the LAU table behind it supplies the municipality name of every
facility, the hover box and the search index.

Payload contract
----------------
``facilities[i]`` is a positional array (short keys keep the file small); the
slots are named in :data:`FACILITY_SLOTS` and mirrored in the component:

``[name, x, y, level, totalAirKg, totalWaterKg, hazardousAirKg,
hazardousWaterKg, carcinogenicAirKg, carcinogenicWaterKg, co2Kg, nSubstances,
lauIndex, sectorIndex, iedActivityIndex, iedStatusIndex, permitYear,
permitUrlIndex]``

The air and water masses are stored separately and never summed in the payload:
"air + water" is the sum of the two slots, so one number per measure comes from
the same places as the numbers the hover box quotes.

The two identifier slots point into string tables rather than repeating text in
every row: ``sectorIndex`` into ``sectors`` (the E-PRTR sector name as the
extract spells it, e.g. "Waste and wastewater management") and
``permitUrlIndex`` into ``ied["permitUrls"]``, the permit link the country
published for the installation (``0`` when the record carries none). Both are
interning tables, so a country that points all its facilities at one register
stores that URL once.

``-1`` marks a missing value (no LAU match, no IED record). ``lau[i]`` is
``[id, name, countryCode, areaKm2, totalAirKg, totalWaterKg, hazardousAirKg,
hazardousWaterKg, carcinogenicAirKg, carcinogenicWaterKg, nFacilities]`` and
``lauIndex`` points into it. Masses are kilograms because the views span six
orders of magnitude; the browser formats and scales them per measure.

``views[i].measures[medium]`` carries everything the map needs to draw one
view for one medium: ``unit``/``factor`` for the labels, ``maxKg``/``totalKg``,
the number of ``facilities`` with a mass, the ``size`` domain of the dots and
the ``slots`` to sum for a facility row. ``media`` lists the three media in the
order of the toggle.

``details[viewId][i]`` is the substance list behind ``facilities[i]``: a flat
array ``[countAll, countAir, countWater, pollutantIndex, kg, medium, ...]``
with the largest substances first, at most :data:`SUBSTANCE_ROWS` of them plus
:data:`SUBSTANCE_MEDIUM_ROWS` per medium, and the counts are the number of
substances the total (or the medium's total) is made of. ``pollutantIndex``
points into ``pollutants``, whose classification is in ``substances``;
``medium`` is :data:`MEDIUM_AIR`, :data:`MEDIUM_WATER` or
:data:`MEDIUM_BOTH`, and ``kg`` is that substance's combined mass, so a row
whose medium is air or water has the same number in either reading. The list
is flat because thousands of nested objects cost more JSON than the values
they carry.


Data sources
------------
* E-PRTR/IED extract: ``eprtr_enhanced.csv`` (EEA IED/E-PRTR v16, 2007-2024,
  classified by ``eprtr_usetox``);
* LAU boundaries and point-in-polygon: GISCO ``LAU_RG_01M_2024_3035.gpkg``.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

import facility_chart_data
import facility_emission_ranking
import palette
from facility_chart_data import VIEWS as CHART_VIEWS
from facility_emission_ranking import (
    CLASSIFICATION_COLUMNS,
    CO2_POLLUTANT,
    DATA_FILE,
    END_YEAR,
    GREENHOUSE_GASES,
    START_YEAR,
    _is_present,
    _unit_for,
    describe_substances,
    find_data_file,
    prepare_releases,
)
from palette import (
    AMBER,
    CRIMSON,
    ORCHID,
    ORANGE,
    SLATE_GREY,
    outline,
)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

MODULE_DIR = Path(__file__).resolve().parent
ASSET_DIR = MODULE_DIR / "assets" / "map"
PAYLOAD_FILE = ASSET_DIR / "facilities.json"
COUNTRY_FILE = ASSET_DIR / "countries.json"
LAU_GEO_FILE = ASSET_DIR / "lau.json"

#: Where the LAU point-in-polygon cache and the GISCO file may live. The cache
#: is kept outside ``assets/`` because it is build input, not a page asset; the
#: first entry is the copy that ships with this repository.
LAU_LOOKUP_CANDIDATES = (
    MODULE_DIR.parent / "LAU" / "lau_lookup.csv",
    Path(
        "/home/sarah/Documents/GitHub/eprtr_usetox/analysis/outputs/lau_lookup.csv"
    ),
)

LAU_GPKG_CANDIDATES = (
    MODULE_DIR.parent / "LAU" / "LAU_RG_01M_2024_3035.gpkg",
)

#: EPSG:3035 window kept in the base map, metres. It covers Portugal to Cyprus
#: and the Canaries to North Cape; clipping at the source keeps the overseas
#: territories of FR, ES, PT and NL out of the picture (they would otherwise
#: stretch the fitted extent across the Atlantic).
MAP_WINDOW = (1_050_000.0, 900_000.0, 6_550_000.0, 5_475_000.0)

#: Metres per stored coordinate unit. 250 m keeps every municipality (the
#: smallest one here, Lalden CH, is 1.3 km² and would collapse into a line at
#: 500 m) while keeping the deltas of a ring small integers.
RING_SCALE = 250

#: Douglas-Peucker tolerance of the two layers, metres. The countries are only
#: context, the municipalities carry the fill, so they keep more detail.
COUNTRY_SIMPLIFY_M = 1500
LAU_SIMPLIFY_M = 1000

#: Polygons below this area (m²) are not simplified at all: a 1 km tolerance
#: turns a municipality of a few square kilometres into a degenerate ring, and
#: the smallest one here (Lalden, CH) is 1.3 km².
MIN_SIMPLIFY_M2 = 5_000_000

#: Country fill and stroke of the base map. The fill is close to white and the
#: stroke a dark slate: land has to read as a bright plate on the pale sea
#: background of the plot (see ``styles.css``) while every border stays legible
#: under dots that are filled at 75% - the coastline is the line the eye uses to
#: find a country, so it is the darkest line on the map (4.7:1 against the land,
#: where the municipality hairline is deliberately quieter).
COUNTRY_FILL = "#fbfcfd"
COUNTRY_STROKE = "#5f7488"

#: How many substances the hover box lists per facility and view, largest
#: first. Four fits the card without scrolling; the rest are counted.
SUBSTANCE_ROWS = 4

#: Additional rows kept for each medium, so a substance that dominates one
#: medium is listed even when its combined mass is small. The card shows the
#: rows of the selected medium, so a water-only substance never hides behind
#: four air-only ones.
SUBSTANCE_MEDIUM_ROWS = 2

#: Circles of the dot-size legend: three, the smallest and largest left out. A
#: circle at the very bottom of the scale is a speck and the one at the top is
#: shared by every capped facility, so neither is a size worth printing; the
#: legend shows the decades in between.
SIZE_TICKS = 3

#: Upper end of the dot-size scale, kilograms (1 Mt). The reported masses run to
#: 4.35 Mt, but only a handful of facilities pass 1 Mt: with the scale ending
#: there, the 100 t - 1 Mt band that holds most of the mass gets the whole
#: radius range, and the few giants share the largest dot (the legend says so).
SIZE_MAX_KG = 1_000_000_000.0

#: Medium codes of ``details`` rows.
MEDIUM_AIR = 1
MEDIUM_WATER = 2
MEDIUM_BOTH = 3

#: The three measures of every view: id, button label, sentence fragment. Air
#: and water are summed from the per-medium slots, "combined" from both. The
#: fragments stay short: the subheading must not run past two lines, or the
#: head would grow and push the toolbar (and the map) down.
MEDIA = (
    ("air", "Air", "air releases only"),
    ("water", "Water", "water releases only"),
    ("combined", "Air + water", "air and water releases"),
)

#: What each view keeps, as the first clause of the hover-free subheading. The
#: ranking chart words its own (narrower) carcinogenic scope; the map says what
#: it draws.
VIEW_SCOPE = {
    "releases": "Every pollutant except CO\u2082",
    "hazardous": "Hazard-classified substances, greenhouse gases out",
    "carcinogenic": (
        "Any IARC 1, 2A or 2B or CLP Carc. substance, greenhouse gases out"
    ),
}


@dataclass(frozen=True)
class HazardLevel:
    """One step of the facility hazard ladder."""

    level: int
    name: str
    short: str
    description: str
    color: str
    outline: str


#: The ladder above, painted with the map colours of the page palette
#: (:mod:`palette`): five separate hues, one per class, in ladder order. A
#: neutral grey for a facility without a classified substance, then golden amber,
#: orange, crimson and orchid for a known carcinogen. The hue, not the depth, is
#: what carries the class; neighbours are dE 79, 33, 48 and 55 apart, and each
#: fill carries its own deeper outline because the dots are drawn at 75% opacity
#: and the amber step is only 1.96:1 against the land unaided (its edge brings
#: that to 5.7:1).
HAZARD_LEVELS = (
    HazardLevel(
        0,
        "No classified substance",
        "Unclassified",
        "Every substance reported here carries no hazard classification.",
        SLATE_GREY,
        outline(SLATE_GREY),
    ),
    HazardLevel(
        1,
        "Hazard-listed",
        "Hazard-listed",
        "A HAP, SVHC or WFD priority substance, but no carcinogenicity class.",
        AMBER,
        outline(AMBER),
    ),
    HazardLevel(
        2,
        "Possibly carcinogenic",
        "Possible",
        "An IARC Group 2B substance or a CLP Carc. 2 substance.",
        ORANGE,
        outline(ORANGE),
    ),
    HazardLevel(
        3,
        "Probably carcinogenic",
        "Probable",
        "An IARC Group 2A substance or a CLP Carc. 1B substance.",
        CRIMSON,
        outline(CRIMSON),
    ),
    HazardLevel(
        4,
        "Carcinogenic to humans",
        "Known",
        "An IARC Group 1 substance or a CLP Carc. 1A substance.",
        ORCHID,
        outline(ORCHID),
    ),
)

#: ``Carc. 2`` / ``Carc. 1B`` / ``Carc. 1A`` inside a CLP string, with the
#: negative look-ahead that keeps ``Carc. 2`` from matching inside ``Carc. 2x``.
CLP_CARC_2 = r"Carc\.\s*2(?!\d)"
CLP_CARC_1B = r"Carc\.\s*1B"
CLP_CARC_1A = r"Carc\.\s*1A"

#: Columns read from the extract. The IED ones carry the installation record
#: (status, Annex I activity code, permit date, permit link) as stringified
#: Python lists.
IED_COLUMNS = (
    "IEDAnnexIMainActivity_ied",
    "IEDMainActivityName_ied",
    "installationStatus_ied",
    "dateOfGranting_ied",
    "permitGranted_ied",
    "permitURL_ied",
    "n_ied_rows",
)

#: Names of the positional slots of ``facilities[i]``, for readers of the JSON
#: and for the assertions in :func:`build_payload`.
FACILITY_SLOTS = (
    "name",
    "x",
    "y",
    "level",
    "totalAirKg",
    "totalWaterKg",
    "hazardousAirKg",
    "hazardousWaterKg",
    "carcinogenicAirKg",
    "carcinogenicWaterKg",
    "co2Kg",
    "nSubstances",
    "lauIndex",
    "sectorIndex",
    "iedActivityIndex",
    "iedStatusIndex",
    "permitYear",
    "permitUrlIndex",
)

#: LAU slot names, see :data:`FACILITY_SLOTS`.
LAU_SLOTS = (
    "id",
    "name",
    "countryCode",
    "areaKm2",
    "totalAirKg",
    "totalWaterKg",
    "hazardousAirKg",
    "hazardousWaterKg",
    "carcinogenicAirKg",
    "carcinogenicWaterKg",
    "nFacilities",
)

#: Facility slots summed by each view, so a measure can read its mass from the
#: payload alone. "air + water" sums both.
VIEW_MASS_SLOTS = {
    "releases": ("totalAirKg", "totalWaterKg"),
    "hazardous": ("hazardousAirKg", "hazardousWaterKg"),
    "carcinogenic": ("carcinogenicAirKg", "carcinogenicWaterKg"),
}

#: Long IED activity names are truncated for the hover box; the code
#: (``6.6(a)``) always stays visible next to them.
IED_NAME_WIDTH = 120

#: ISO-2 codes of the countries whose facilities are plotted, so the hover box
#: can name the country without a lookup table.
COUNTRY_NAMES = {
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "EE": "Estonia",
    "EL": "Greece",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "HR": "Croatia",
    "HU": "Hungary",
    "IE": "Ireland",
    "IS": "Iceland",
    "IT": "Italy",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "MT": "Malta",
    "NL": "Netherlands",
    "NO": "Norway",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "RS": "Serbia",
    "SE": "Sweden",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "CH": "Switzerland",
    "LI": "Liechtenstein",
    "AL": "Albania",
    "MK": "North Macedonia",
    "TR": "Türkiye",
}


# --------------------------------------------------------------------------
# Extract
# --------------------------------------------------------------------------


def load_map_frame(
    path: str | Path | None = None,
    *,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
) -> pd.DataFrame:
    """Read the E-PRTR extract with the columns the map needs.

    Adds the same hazard flags as the facility ranking chart
    (``is_hazardous``, ``is_carcinogenic``, ``facility_label``) plus the
    coordinates, the EPRTR sector and the IED installation record (status,
    Annex I activity, permit date and permit link). Rows without a reported
    mass or without a facility identifier are dropped by
    :func:`prepare_releases`.
    """
    source = find_data_file(path)
    usecols = [
        "reportingYear",
        "FacilityInspireId",
        "facilityName",
        "city",
        "countryName",
        "Longitude",
        "Latitude",
        "EPRTR_SectorName",
        "Pollutant",
        "Releases",
        # Air or water: the map measures every view per medium, and the hover
        # box names the medium of each substance.
        "TargetRelease",
        *CLASSIFICATION_COLUMNS,
        *IED_COLUMNS,
    ]
    frame = pd.read_csv(source, usecols=usecols, low_memory=False)
    return prepare_releases(frame, start_year=start_year, end_year=end_year)


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------

#: Defining parameters of EPSG:3035 (GRS80, natural origin 52 N 10 E). The same
#: numbers as ``geo3035.js`` and ``census_profile.py`` in ``eprtr_usetox``.
_A = 6378137.0
_F = 1.0 / 298.257222101
_E2 = _F * (2.0 - _F)
_E = math.sqrt(_E2)
_LAT0 = math.radians(52.0)
_LON0 = math.radians(10.0)
_FE = 4321000.0
_FN = 3210000.0


def _authalic_q(phi: np.ndarray) -> np.ndarray:
    """Authalic quantity q(phi) of EPSG method 9820."""
    sine = np.sin(phi)
    return (1 - _E2) * (
        sine / (1 - _E2 * sine * sine)
        - (1.0 / (2.0 * _E)) * np.log((1.0 - _E * sine) / (1.0 + _E * sine))
    )


_Q_POLE = float(_authalic_q(np.array(math.pi / 2.0)))
_Q0 = float(_authalic_q(np.array(_LAT0)))
_BETA0 = math.asin(_Q0 / _Q_POLE)
_RQ = _A * math.sqrt(_Q_POLE / 2.0)
_M0 = math.cos(_LAT0) / math.sqrt(1.0 - _E2 * math.sin(_LAT0) ** 2)
_D = _A * _M0 / (_RQ * math.cos(_BETA0))
_SIN_B0 = math.sin(_BETA0)
_COS_B0 = math.cos(_BETA0)


def project_laea3035(
    lon: Sequence[float] | np.ndarray, lat: Sequence[float] | np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """WGS84/ETRS89 degrees to EPSG:3035 metres, in closed form.

    The LAU geometry ships already projected (the GISCO file is in 3035), so the
    facility coordinates are projected here and the browser only has to scale
    metres to pixels.
    """
    longitude = np.asarray(lon, dtype=float)
    latitude = np.asarray(lat, dtype=float)
    delta = np.radians(longitude) - _LON0
    beta = np.arcsin(np.clip(_authalic_q(np.radians(latitude)) / _Q_POLE, -1.0, 1.0))
    radius = _RQ * np.sqrt(
        2.0 / (1.0 + _SIN_B0 * np.sin(beta) + _COS_B0 * np.cos(beta) * np.cos(delta))
    )
    east = _FE + radius * _D * np.cos(beta) * np.sin(delta)
    north = _FN + (radius / _D) * (
        _COS_B0 * np.sin(beta) - _SIN_B0 * np.cos(beta) * np.cos(delta)
    )
    return east, north


# --------------------------------------------------------------------------
# Classification helpers
# --------------------------------------------------------------------------


def _row_levels(frame: pd.DataFrame) -> pd.Series:
    """Hazard tier of every release row, as defined in :data:`HAZARD_LEVELS`."""
    iarc = frame["iarc_group"].astype("string").str.strip().str.upper().fillna("")
    clp = frame["clp_class"].astype("string").fillna("")

    listed = (
        frame["hap"].fillna(False).astype(bool)
        | frame["cand_svhc"].fillna(False).astype(bool)
        | frame["wfd_prio_sub"].fillna(False).astype(bool)
        | _is_present(frame["iarc_group"])
        | _is_present(frame["clp_class"])
    ) & ~frame["Pollutant"].isin(GREENHOUSE_GASES)

    possible = iarc.eq("2B") | clp.str.contains(CLP_CARC_2, regex=True, na=False)
    probable = iarc.eq("2A") | clp.str.contains(CLP_CARC_1B, regex=True, na=False)
    known = iarc.eq("1") | clp.str.contains(CLP_CARC_1A, regex=True, na=False)

    level = pd.Series(0, index=frame.index, dtype="int8")
    level = level.mask(listed, 1)
    level = level.mask(listed & possible, 2)
    level = level.mask(listed & probable, 3)
    level = level.mask(listed & known, 4)
    return level.astype("int8")


def _nice_ceiling(value: float) -> float:
    """Smallest 1-2-5 step at or above ``value`` (5 Mt for 4.35 Mt)."""
    if not (value > 0):
        return 1.0
    exponent = math.floor(math.log10(value))
    for factor in (1.0, 2.0, 5.0):
        candidate = factor * 10.0**exponent
        if candidate >= value:
            return candidate
    return 10.0 ** (exponent + 1)


def _size_scale(masses: list[float], largest: float) -> dict[str, Any]:
    """Domain and legend ticks of the dot diameter, on a log scale.

    The reported masses span eight decades, so an area-proportional scale would
    draw every facility but the largest as the same dot. The diameter therefore
    follows the log of the mass, with two round anchors: the decade just below
    the fifth percentile (the kilogram-scale tail does not stretch the scale)
    and the next 1-2-5 step above the largest facility - but never above
    :data:`SIZE_MAX_KG`. Only a handful of facilities pass 1 Mt, so a scale that
    ended at the 4.35 Mt maximum would spend most of the radius range on
    facilities that are not there: capping it hands that range to the 100 t -
    1 Mt band where the bulk of the mass sits, and the few giants share the
    largest dot. The legend draws three circles from the decades inside that
    span, never at its ends (see :func:`_decade_ticks`): on the combined
    releases view that reads ``1 t``, ``1 kt``, ``100 kt``.
    """
    positive = [float(value) for value in masses if value and value > 0]
    if positive:
        low = 10.0 ** math.floor(math.log10(float(np.percentile(positive, 5))))
    else:  # pragma: no cover - a view always has facilities
        low = 1.0
    high = max(min(_nice_ceiling(float(largest)), SIZE_MAX_KG), low * 10.0)
    return {
        "minKg": _signal(low, 2),
        "maxKg": _signal(high, 2),
        "ticks": _decade_ticks(low, high),
        "capKg": _signal(SIZE_MAX_KG, 2),
        "cappedFacilities": sum(1 for mass in positive if mass > SIZE_MAX_KG),
    }


def _decade_ticks(low: float, high: float) -> list[float]:
    """``SIZE_TICKS`` legend circles strictly inside the span of the scale.

    The two ends are left out on purpose: the bottom circle is a speck of 1.4 px
    and the top one is shared by every facility past the 1 Mt cap, so neither
    reads as a size to compare against. What remains is the decades between
    them, three of them spread evenly - on the combined releases scale (100 kg
    to 1 Mt) that is 1 t, 1 kt and 100 kt.

    A high end that is not a power of ten (5 Mt, say) keeps the decade below it,
    because 10^6 still sits inside 5 x 10^6; a span of only two or three decades
    returns the one or two interior decades it has, and a single-decade span has
    no interior at all, so it prints its two ends.
    """
    low_exponent = int(round(math.log10(low))) if low > 0 else 0
    if high <= 0:
        return _log_ticks(low, high)
    high_exponent = int(math.floor(math.log10(high)))
    is_power = math.isclose(high, 10.0**high_exponent)
    top = high_exponent - 1 if is_power else high_exponent
    inner = list(range(low_exponent + 1, top + 1))
    if not inner:
        # A span of a single decade has no interior to print, so the two ends
        # are the only sizes there are.
        return [_signal(low, 2), _signal(high, 2)]
    if len(inner) <= SIZE_TICKS:
        return [10.0**exponent for exponent in inner]
    last = len(inner) - 1
    # Three indices spread over the interior, the middle one rounded down.
    picks = [
        inner[(index * last + (SIZE_TICKS - 1) // 2) // (SIZE_TICKS - 1)]
        for index in range(SIZE_TICKS)
    ]
    return [10.0**exponent for exponent in dict.fromkeys(picks)]


def _log_ticks(low: float, high: float) -> list[float]:
    """Fallback for a span without usable decades: three even steps in log space.

    Only reachable when the domain is degenerate (a high end at or below zero),
    because :func:`_decade_ticks` floors a non-decade high end to its decade.
    """
    if low <= 0 or high <= low:
        return [_signal(max(low, 0.0), 2), _signal(max(high, 0.0), 2)]
    low_log, high_log = math.log10(low), math.log10(high)
    return [
        _signal(10.0 ** (low_log + (high_log - low_log) * step / (SIZE_TICKS + 1)), 2)
        for step in range(1, SIZE_TICKS + 1)
    ]


def _signal(value: float | None, digits: int = 3) -> float | int:
    """Round a mass to ``digits`` significant digits, keeping the JSON small."""
    if value is None:
        return 0
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return 0
    if number < 1000:
        return round(number, 3)
    step = 10 ** (int(math.floor(math.log10(number))) - digits + 1)
    return int(round(number / step) * step)


def _intern(values: Iterable[Any]) -> tuple[list[str], dict[str, int]]:
    """String table and index, in first-seen order."""
    table: list[str] = []
    index: dict[str, int] = {}
    for value in values:
        text = "" if value is None else str(value)
        if text not in index:
            index[text] = len(table)
            table.append(text)
    return table, index


def _list_values(value: Any) -> list[str]:
    """Items of a stringified Python list: ``"['functional', 'disused']"``."""
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text or text.lower() in {"nan", "none"}:
        return []
    if not text.startswith("["):
        return [text]
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return [text]
    if isinstance(parsed, (list, tuple)):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [str(parsed)]


def _joined(value: Any, limit: int = 3) -> str:
    """First few items of a stringified list, joined for display."""
    items = list(dict.fromkeys(_list_values(value)))
    if not items:
        return ""
    if len(items) > limit:
        items = items[:limit] + ["\u2026"]
    return " / ".join(items)


def _permit_year(value: Any) -> int:
    """Year of the first IED permit date; -1 when there is no usable date."""
    for text in _list_values(value):
        match = re.match(r"(\d{4})", text)
        if match:
            year = int(match.group(1))
            if 1950 <= year <= 2100:
                return year
    return -1


#: Shape of a usable permit link: a dotted host or a raw IPv4 address, an
#: optional port and an optional path, query or fragment. The column also
#: carries bare hosts (``www.brabant.nl``), intranet names (``http://simca/``)
#: and the odd free-text note, which must not become a link the browser cannot
#: follow; the scheme is added before the match, see :func:`_permit_url`.
URL_TEXT = re.compile(
    r"^https?://(?:(?:[\w-]+\.)+[a-z]{2,}|(?:\d{1,3}\.){3}\d{1,3})"
    r"(?::\d+)?(?:[/?#]\S*)?$",
    re.IGNORECASE,
)


def _permit_url(value: Any) -> str:
    """First followable permit link of the IED record, or ``""``.

    The extract holds the links as a stringified Python list of what each
    country published: absolute URLs, bare hosts without a scheme, and
    occasionally nothing usable. ``https://`` is added where it is missing,
    because a bare host is a relative link in the browser.
    """
    for text in _list_values(value):
        link = text.strip().strip("\"'")
        if not link:
            continue
        # Some rows carry a broken prefix (``docmhttps://...``) or a browser
        # extension in front of the real link: the link starts at its scheme.
        scheme = re.search(r"https?://", link, re.IGNORECASE)
        if scheme:
            link = link[scheme.start() :]
        elif link.startswith("//"):
            link = "https:" + link
        else:
            link = "https://" + link
        # Two links pasted into one value: the first one is the permit.
        head, _, tail = link.partition(" ")
        if tail and re.match(r"https?://", tail.strip(), re.IGNORECASE):
            link = head
        # A space left inside a path is what the browser would escape anyway.
        link = link.replace(" ", "%20")
        if URL_TEXT.match(link):
            return link
    return ""


def _shorten(text: str, width: int = IED_NAME_WIDTH) -> str:
    """Cut a long IED activity name at a word boundary."""
    if len(text) <= width:
        return text
    return text[:width].rsplit(" ", 1)[0].rstrip(",;:") + "\u2026"


# --------------------------------------------------------------------------
# LAU reference table
# --------------------------------------------------------------------------


def find_lau_lookup(path: str | Path | None = None) -> Path:
    """Return the LAU point-in-polygon cache, or explain how to build one."""
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path).expanduser())
    candidates.extend(LAU_LOOKUP_CANDIDATES)

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    searched = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise FileNotFoundError(
        "Could not locate lau_lookup.csv. Run 'python facility_map_data.py "
        f"--lookup' (needs GDAL and the GISCO LAU file). Searched:\n{searched}"
    )


def load_lau_lookup(path: str | Path | None = None) -> pd.DataFrame:
    """Read the facility-to-LAU table (one row per distinct coordinate)."""
    source = find_lau_lookup(path)
    table = pd.read_csv(
        source,
        usecols=["lon", "lat", "lau_id", "lau_name", "lau_cc", "lau_area"],
        low_memory=False,
    )
    table["lon"] = pd.to_numeric(table["lon"], errors="coerce").round(4)
    table["lat"] = pd.to_numeric(table["lat"], errors="coerce").round(4)
    table = table.dropna(subset=["lon", "lat", "lau_id"])
    return table.drop_duplicates(subset=["lon", "lat"], keep="first")


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


def _view_masks(
    frame: pd.DataFrame, levels: pd.Series
) -> dict[str, pd.Series]:
    """The three views as row masks.

    ``releases`` and ``hazardous`` are the masks of the ranking chart. The
    carcinogenic view is wider on purpose (see the module docstring): a row
    counts as soon as the hazard ladder calls its substance carcinogenic at all,
    so IARC Group 2B and ``Carc. 2`` rows sit next to 1/2A and are not lost just
    because another substance of the facility ranks higher. Rows of greenhouse
    gases never reach level 2, so no CO2 row slips in.
    """
    is_co2 = frame["Pollutant"].eq(CO2_POLLUTANT)
    return {
        "releases": ~is_co2,
        "hazardous": frame["is_hazardous"],
        "carcinogenic": levels >= 2,
    }


def _substance_breakdown(
    frame: pd.DataFrame,
    mask: pd.Series,
    identifier: str,
    pollutant_index: dict[str, int],
    limit: int = SUBSTANCE_ROWS,
    medium_limit: int = SUBSTANCE_MEDIUM_ROWS,
) -> dict[str, list[float | int]]:
    """Substances behind the total of one view, per facility.

    Returns ``{facility: [countAll, countAir, countWater, pollutantIndex, kg,
    medium, ...]}``, largest substance first. The list keeps the ``limit``
    largest substances by combined mass plus the ``medium_limit`` largest of
    each medium, so a substance that only matters in water is still named when
    the reader switches the map to water. The three counts are the number of
    substances the combined, air and water totals are made of, so the hover box
    can say how many it left out in whichever medium is on. The mass is summed
    over the years of the extract, exactly like the facility total it explains.
    """
    columns = [identifier, "Pollutant", "Releases", "TargetRelease"]
    subset = frame.loc[mask, columns]
    if subset.empty:
        return {}

    subset = subset.assign(Pollutant=subset["Pollutant"].astype(str))
    grouped = subset.groupby([identifier, "Pollutant"], observed=True)
    combined = grouped["Releases"].sum().rename("kg")
    by_medium = {
        medium: subset[subset["TargetRelease"].eq(medium)]
        .groupby([identifier, "Pollutant"], observed=True)["Releases"]
        .sum()
        .rename(medium.lower())
        for medium in ("AIR", "WATER")
    }
    table = (
        pd.concat([combined, *by_medium.values()], axis=1)
        .fillna(0.0)
        .reset_index()
    )
    table["medium"] = MEDIUM_AIR
    table.loc[table["water"] > 0, "medium"] = MEDIUM_WATER
    table.loc[(table["air"] > 0) & (table["water"] > 0), "medium"] = MEDIUM_BOTH

    breakdown: dict[str, list[float | int]] = {}
    for facility, group in table.groupby(identifier, observed=True):
        ordered = group.sort_values("kg", ascending=False)
        picked = list(ordered.index[:limit])
        for column in ("air", "water"):
            largest = ordered.sort_values(column, ascending=False).index[:medium_limit]
            for position in largest:
                if position not in picked:
                    picked.append(position)

        row: list[float | int] = [
            len(group),
            int((group["air"] > 0).sum()),
            int((group["water"] > 0).sum()),
        ]
        for position in picked:
            entry = ordered.loc[position]
            place = pollutant_index.get(entry["Pollutant"], -1)
            if place < 0:  # pragma: no cover - every pollutant is in the table
                continue
            row.extend(
                [
                    place,
                    _signal(float(entry["kg"])),
                    int(entry["medium"]),
                ]
            )
        breakdown[str(facility)] = row
    return breakdown


def _attach_lau(latest: pd.DataFrame, lau: pd.DataFrame) -> pd.DataFrame:
    """Attach the LAU record to the facility rows through their coordinates.

    ``latest`` is indexed by facility; a merge drops the index, so it is
    restored on the way out - the caller keeps working with facility keys.
    """
    identifier = latest.index.name
    located = latest.reset_index()
    located["lon4"] = pd.to_numeric(located["Longitude"], errors="coerce").round(4)
    located["lat4"] = pd.to_numeric(located["Latitude"], errors="coerce").round(4)
    reference = lau.rename(columns={"lon": "lon4", "lat": "lat4"})
    joined = located.merge(
        reference[["lon4", "lat4", "lau_id", "lau_name", "lau_cc", "lau_area"]],
        on=["lon4", "lat4"],
        how="left",
    )
    return joined.drop(columns=["lon4", "lat4"]).set_index(identifier)


def _activity_table(
    located: pd.DataFrame,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """IED Annex I activities: index by code, with a readable name."""
    names: dict[str, str] = {}
    for code_value, name_value in zip(
        located["IEDAnnexIMainActivity_ied"],
        located["IEDMainActivityName_ied"],
    ):
        code = _joined(code_value, limit=1)
        if code and code not in names:
            names[code] = _shorten(_joined(name_value, limit=1))
    ordered = sorted(names.items())
    table = [{"code": code, "name": name} for code, name in ordered]
    return table, {code: position for position, (code, _) in enumerate(ordered)}


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """``1 facility`` / ``12 facilities``."""
    if count == 1:
        return f"1 {singular}"
    return f"{count:,} {plural or singular + 's'}"


def build_payload(
    data: pd.DataFrame | str | Path | None = None,
    *,
    lau: pd.DataFrame | None = None,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
) -> dict[str, Any]:
    """Build the JSON payload of the map.

    ``data`` may be the raw extract, an already prepared release table (for
    example ``eprtr_classified`` from the notebook) or a path to the CSV.
    ``lau`` is the facility-to-LAU table; it is read from ``lau_lookup.csv``
    when omitted.
    """
    frame = (
        load_map_frame(data, start_year=start_year, end_year=end_year)
        if data is None or isinstance(data, (str, Path))
        else prepare_releases(data, start_year=start_year, end_year=end_year)
    )
    if frame.empty:
        raise ValueError("No releases left after filtering; check the input data.")
    if "TargetRelease" not in frame.columns:
        raise KeyError(
            "The map payload needs the TargetRelease column (AIR / WATER); "
            "read the extract with facility_map_data.load_map_frame()."
        )

    identifier = "FacilityInspireId"
    frame = frame.assign(_level=_row_levels(frame))
    levels = frame.groupby(identifier, observed=True)["_level"].max()
    masks = _view_masks(frame, frame["_level"])

    is_air = frame["TargetRelease"].eq("AIR")
    is_water = frame["TargetRelease"].eq("WATER")

    def summed(mask: pd.Series, medium: str | None = None) -> pd.Series:
        """Cumulative mass 2022-2024 per facility, inside one mask and medium."""
        if medium == "air":
            rows = frame.loc[mask & is_air]
        elif medium == "water":
            rows = frame.loc[mask & is_water]
        else:
            rows = frame.loc[mask]
        return rows.groupby(identifier, observed=True)["Releases"].sum()

    # Air and water stay apart for every view, so the medium toggle only picks
    # slots to read - no second aggregation per setting.
    masses = {
        key: (summed(mask, "air"), summed(mask, "water"))
        for key, mask in masks.items()
    }
    mass_series = [series for key in masses for series in masses[key]]
    co2 = summed(frame["Pollutant"].eq(CO2_POLLUTANT))
    substances = frame.loc[masks["releases"]].groupby(identifier, observed=True)[
        "Pollutant"
    ].nunique()

    # Attributes come from the most recent reporting year of each facility.
    latest = (
        frame.sort_values("reportingYear")
        .drop_duplicates(identifier, keep="last")
        .set_index(identifier)
    )
    latest = _attach_lau(latest, load_lau_lookup() if lau is None else lau)

    latitude = pd.to_numeric(latest["Latitude"], errors="coerce")
    longitude = pd.to_numeric(latest["Longitude"], errors="coerce")
    inside = (longitude.between(-32.0, 45.0) & latitude.between(27.0, 72.0)).fillna(
        False
    )
    missing_coordinates = int((~inside).sum())
    located = latest.loc[inside].copy()
    located["x_m"], located["y_m"] = project_laea3035(
        located["Longitude"], located["Latitude"]
    )

    # A handful of coordinates parse but land outside the frame (misplaced
    # facilities in the extract); they would only stretch the axes.
    x0, y0, x1, y1 = MAP_WINDOW
    in_window = located["x_m"].between(x0, x1) & located["y_m"].between(y0, y1)
    off_map = int((~in_window).sum())
    located = located.loc[in_window]

    # Draw the largest facilities first, so small circles stay on top of them.
    released = masses["releases"][0].add(masses["releases"][1], fill_value=0.0)
    order = released.sort_values(ascending=False).index
    located = located.loc[[key for key in order if key in located.index]].reset_index()

    pollutant_names = (
        frame.loc[masks["releases"]]
        .groupby("Pollutant", observed=True)["Releases"]
        .sum()
        .sort_values(ascending=False)
        .index.astype(str)
        .tolist()
    )
    pollutant_index = {name: position for position, name in enumerate(pollutant_names)}

    # The substance list of every facility, one flat array per view, in the
    # order of ``payload["details"][viewId]``.
    breakdowns = {
        key: _substance_breakdown(frame, mask, identifier, pollutant_index)
        for key, mask in masks.items()
    }

    sectors, sector_index = _intern(
        [""] + located["EPRTR_SectorName"].fillna("").astype(str).unique().tolist()
    )
    activities, activity_index = _activity_table(located)
    statuses, status_index = _intern(
        [""] + [_joined(value) for value in located["installationStatus_ied"].unique()]
    )
    # Permit links are interned like the other IED strings: the countries that
    # publish one register tend to reuse it across their facilities.
    permit_urls, permit_url_index = _intern(
        [""] + [_permit_url(value) for value in located["permitURL_ied"]]
    )

    records = located[
        [
            "FacilityInspireId",
            "facilityName",
            "x_m",
            "y_m",
            "lau_id",
            "lau_name",
            "lau_cc",
            "lau_area",
            "EPRTR_SectorName",
            "IEDAnnexIMainActivity_ied",
            "installationStatus_ied",
            "dateOfGranting_ied",
        ]
    ].itertuples(index=False)

    # LAU table in first-seen order (largest facility first), with the air and
    # water mass of every view summed over the facilities inside each
    # municipality.
    lau_table: list[list[Any]] = []
    lau_at: dict[str, int] = {}
    for record in records:
        if not isinstance(record.lau_id, str) or not record.lau_id:
            continue
        position = lau_at.get(record.lau_id)
        if position is None:
            area = record.lau_area
            position = len(lau_table)
            lau_at[record.lau_id] = position
            lau_table.append(
                [
                    record.lau_id,
                    record.lau_name if isinstance(record.lau_name, str) else "",
                    record.lau_cc if isinstance(record.lau_cc, str) else "",
                    round(float(area), 1)
                    if isinstance(area, (int, float)) and math.isfinite(float(area))
                    else 0.0,
                    *[0.0 for _ in mass_series],
                    0,
                ]
            )
        row = lau_table[position]
        facility = record.FacilityInspireId
        for offset, series in enumerate(mass_series):
            row[4 + offset] += float(series.get(facility, 0.0))
        row[-1] += 1

    facilities: list[list[Any]] = []
    details: dict[str, list[list[float | int]]] = {key: [] for key in breakdowns}
    for record in located[
        [
            "FacilityInspireId",
            "facilityName",
            "x_m",
            "y_m",
            "lau_id",
            "EPRTR_SectorName",
            "IEDAnnexIMainActivity_ied",
            "installationStatus_ied",
            "dateOfGranting_ied",
            "permitURL_ied",
        ]
    ].itertuples(index=False):
        facility = record.FacilityInspireId
        activity = _joined(record.IEDAnnexIMainActivity_ied, limit=1)
        status = _joined(record.installationStatus_ied)
        sector = record.EPRTR_SectorName if isinstance(record.EPRTR_SectorName, str) else ""
        permit_url = _permit_url(record.permitURL_ied)
        for key, table in breakdowns.items():
            details[key].append(table.get(facility, []))

        facilities.append(
            [
                record.facilityName if isinstance(record.facilityName, str) else "",
                int(round(record.x_m)),
                int(round(record.y_m)),
                int(levels.get(facility, 0)),
                *[_signal(series.get(facility, 0.0)) for series in mass_series],
                _signal(co2.get(facility, 0.0)),
                int(substances.get(facility, 0)),
                lau_at.get(record.lau_id, -1)
                if isinstance(record.lau_id, str)
                else -1,
                sector_index.get(sector, 0),
                activity_index.get(activity, -1) if activity else -1,
                status_index.get(status, -1) if status else -1,
                _permit_year(record.dateOfGranting_ied),
                permit_url_index.get(permit_url, 0),
            ]
        )

    for row in facilities:
        if len(row) != len(FACILITY_SLOTS):  # pragma: no cover - guards the contract
            raise AssertionError(
                f"facility row has {len(row)} slots, expected {len(FACILITY_SLOTS)}"
            )

    # One entry per view and medium. "air + water" is the sum of the two mass
    # slots, so every number the map draws comes from the same two slots the
    # hover box quotes.
    slot = {name: position for position, name in enumerate(FACILITY_SLOTS)}
    permit_links = sum(1 for row in facilities if row[slot["permitUrlIndex"]] > 0)
    views: list[dict[str, Any]] = []
    for spec in CHART_VIEWS:
        air_slot, water_slot = (slot[name] for name in VIEW_MASS_SLOTS[spec.key])
        measures: dict[str, Any] = {}
        for medium, button, phrase in MEDIA:
            if medium == "air":
                values = [row[air_slot] for row in facilities]
                used = [VIEW_MASS_SLOTS[spec.key][0]]
            elif medium == "water":
                values = [row[water_slot] for row in facilities]
                used = [VIEW_MASS_SLOTS[spec.key][1]]
            else:
                values = [row[air_slot] + row[water_slot] for row in facilities]
                used = list(VIEW_MASS_SLOTS[spec.key])
            largest = max(values) if values else 0.0
            factor, symbol, unit_name = _unit_for(largest)
            measures[medium] = {
                "id": medium,
                "button": button,
                "unit": symbol,
                "unitName": unit_name,
                "factor": factor,
                "maxKg": _signal(largest),
                "totalKg": _signal(sum(values)),
                "facilities": sum(1 for value in values if value > 0),
                "slots": used,
                "size": _size_scale(values, largest),
                "subheading": (
                    f"{VIEW_SCOPE[spec.key]} \u00b7 {phrase} \u00b7 "
                    f"{start_year}\u2013{end_year}"
                ),
            }
        views.append(
            {
                "id": spec.key,
                "button": spec.button,
                "heading": spec.heading,
                "measures": measures,
            }
        )

    level_counts = levels.value_counts().to_dict()
    payload: dict[str, Any] = {
        "meta": {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "years": [start_year, end_year],
            "source": (
                f"E-PRTR / IED extract ({DATA_FILE}), {start_year}\u2013{end_year}"
            ),
            "projection": "EPSG:3035",
            "window": [int(value) for value in MAP_WINDOW],
            "facilityCount": len(facilities),
            "missingCoordinates": missing_coordinates,
            "offMap": off_map,
            "lauCount": len(lau_table),
            "facilitiesWithLau": sum(
                1 for row in facilities if row[slot["lauIndex"]] >= 0
            ),
            "ringScale": RING_SCALE,
            "sizeCapKg": _signal(SIZE_MAX_KG, 2),
            "permitLinks": permit_links,
        },
        "slots": {"facilities": list(FACILITY_SLOTS), "lau": list(LAU_SLOTS)},
        "views": views,
        "media": [
            {"id": medium, "button": button, "note": phrase}
            for medium, button, phrase in MEDIA
        ],
        "pollutants": pollutant_names,
        "substances": describe_substances(frame),
        "sectors": sectors,
        "ied": {
            "activities": activities,
            "statuses": statuses,
            "permitUrls": permit_urls,
        },
        "levels": [
            {
                "level": item.level,
                "name": item.name,
                "short": item.short,
                "description": item.description,
                "color": item.color,
                "outline": item.outline,
                "facilities": int(level_counts.get(item.level, 0)),
            }
            for item in HAZARD_LEVELS
        ],
        "countries": {
            "fill": COUNTRY_FILL,
            "stroke": COUNTRY_STROKE,
            "names": COUNTRY_NAMES,
        },
        "facilities": facilities,
        "details": details,
        "lau": lau_table,
    }
    return payload


def write_payload(
    payload: dict[str, Any] | None = None, path: str | Path = PAYLOAD_FILE
) -> Path:
    """Write the payload as single-line JSON: it is an asset, not a document.

    An unchanged payload is not rewritten - not even its mtime is touched - so a
    render that changed nothing leaves the asset as it was, and the next render
    can see that it is up to date.
    """
    if payload is None:
        payload = build_payload()
    target = Path(path)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    if target.is_file():
        try:
            if target.read_text(encoding="utf-8") == text:
                return target
        except OSError:
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


#: The code the payload is computed from. These are inputs as much as the CSV:
#: a palette token reaches the legend, a view definition reaches the tables, and
#: a rounding rule reaches every number in the file.
PAYLOAD_MODULES = (
    Path(__file__),
    Path(facility_emission_ranking.__file__),
    Path(facility_chart_data.__file__),
    Path(palette.__file__),
)


def payload_sources(
    data: str | Path | None = None, *, lookup: str | Path | None = None
) -> list[Path]:
    """Every file the payload is derived from: the extract, the cache, the code."""
    return [find_data_file(data), find_lau_lookup(lookup), *PAYLOAD_MODULES]


def payload_is_fresh(
    path: str | Path = PAYLOAD_FILE,
    *,
    sources: Iterable[Path] | None = None,
    data: str | Path | None = None,
    lookup: str | Path | None = None,
    force: bool = False,
) -> bool:
    """Whether the payload is newer than everything it is built from.

    The step runs on every render (``project.pre-render``) and reads the whole
    extract to write a 3.5 MB asset; a rebuild that would produce the same file
    is what made a re-render look like it never settled. A payload written after
    its newest input is the answer this build would write anyway, so it is kept.
    """
    target = Path(path)
    if force or not target.is_file():
        return False
    files = (
        list(sources)
        if sources is not None
        else payload_sources(data, lookup=lookup)
    )
    newest = max(source.stat().st_mtime for source in files)
    return target.stat().st_mtime > newest


def _payload_lau_ids(payload: dict[str, Any] | None) -> list[str]:
    """The municipalities the payload draws, from memory or from the file."""
    if payload is None:
        payload = json.loads(Path(PAYLOAD_FILE).read_text(encoding="utf-8"))
    slot = LAU_SLOTS.index("id")
    return [row[slot] for row in payload["lau"]]


# --------------------------------------------------------------------------
# Base geometry (GDAL)
# --------------------------------------------------------------------------

#: Layer name used by the GISCO GeoPackage when it cannot be read from the file.
#: The packaged layer keeps the name of the file it was built from.
LAU_LAYER_FALLBACK = "LAU_RG_01M_2024_3035.gpkg"

#: Rings thinner than this (km²) are dropped - at a European scale they are
#: smaller than a pixel - except the largest ring of every shape, which is kept
#: so that a municipality on a small island does not lose its outline.
MIN_RING_KM2 = 2.0


def find_lau_gpkg(path: str | Path | None = None) -> Path:
    """Return the GISCO LAU GeoPackage, or explain where to put it."""
    candidates = [Path(path).expanduser()] if path else []
    candidates.extend(LAU_GPKG_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise FileNotFoundError(
        "Could not locate the GISCO LAU GeoPackage "
        "(LAU_RG_01M_2024_3035.gpkg). Download it from "
        "https://gisco-services.ec.europa.eu/distribution/v2/lau/ and put it "
        f"next to LAU/lau_lookup.csv. Searched:\n{searched}"
    )


def _layer_name(gpkg: Path) -> str:
    """Name of the single layer inside the GISCO GeoPackage."""
    ogrinfo = shutil.which("ogrinfo")
    if ogrinfo:
        completed = subprocess.run(
            [ogrinfo, "-ro", "-so", str(gpkg)], capture_output=True, text=True
        )
        for line in completed.stdout.splitlines():
            match = re.match(r"\s*1:\s*(.+?)\s*\(", line)
            if match:
                return match.group(1)
    return LAU_LAYER_FALLBACK


def _window_wkt() -> str:
    """The map window as WKT in EPSG:3035."""
    x0, y0, x1, y1 = MAP_WINDOW
    return (
        f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"
    )


def _ogr2ogr(
    sql: str, source: str | Path, target: Path, *, fmt: str = "GeoJSON"
) -> None:
    """Run one ``ogr2ogr`` query, passing the statement through a file.

    The statement travels in a file because the LAU filter lists thousands of
    municipality ids and such an argument overflows the process argument list.
    """
    binary = shutil.which("ogr2ogr")
    if binary is None:
        raise RuntimeError(
            "ogr2ogr was not found. The geometry step needs GDAL (apt install "
            "gdal-bin); it is a one-off step, so it can also be run on another "
            "machine and the two JSON files copied over."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        statement = Path(scratch) / "query.sql"
        statement.write_text(sql, encoding="utf-8")
        completed = subprocess.run(
            [
                binary,
                "-f",
                fmt,
                str(target),
                str(source),
                "-dialect",
                "SQLITE",
                "-sql",
                f"@{statement}",
            ],
            capture_output=True,
            text=True,
        )
    if completed.returncode:
        raise RuntimeError(
            f"ogr2ogr failed with {fmt}: {completed.stderr.strip()[-600:]}"
        )


def _query_geojson(sql: str, source: Path) -> dict[str, Any]:
    """Run a query and return the parsed GeoJSON it produced."""
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / "layer.geojson"
        _ogr2ogr(sql, source, target)
        return json.loads(target.read_text(encoding="utf-8"))


def _quantise(ring: Sequence[Sequence[float]], scale: int = RING_SCALE) -> list[tuple[int, int]]:
    """Ring in whole ``scale``-metre steps, without duplicates or closure."""
    points: list[tuple[int, int]] = []
    for coordinate in ring:
        point = (
            int(round(float(coordinate[0]) / scale)),
            int(round(float(coordinate[1]) / scale)),
        )
        if not points or points[-1] != point:
            points.append(point)
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    return points if len(points) >= 3 else []


def _ring_area_km2(points: Sequence[tuple[int, int]], scale: int = RING_SCALE) -> float:
    """Shoelace area of a quantised ring, converted to square kilometres."""
    double_area = 0
    for (x0, y0), (x1, y1) in zip(points, list(points[1:]) + [points[0]]):
        double_area += x0 * y1 - x1 * y0
    return abs(double_area) / 2.0 * (scale / 1000.0) ** 2


def _delta_encode(points: Sequence[tuple[int, int]]) -> list[int]:
    """``[x0, y0, dx1, dy1, ...]`` - the first point absolute, the rest small."""
    flat = [points[0][0], points[0][1]]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        flat.extend((x1 - x0, y1 - y0))
    return flat


def _encode_geometry(
    geometry: dict[str, Any] | None, min_ring_km2: float = MIN_RING_KM2
) -> list[list[int]]:
    """Compact rings of one (Multi)Polygon; empty when nothing survives."""
    if not geometry:
        return []
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "MultiPolygon":
        polygons = coordinates
    else:
        polygons = [coordinates]

    rings: list[tuple[float, list[tuple[int, int]]]] = []
    for polygon in polygons:
        for ring in polygon:
            points = _quantise(ring)
            if points:
                rings.append((_ring_area_km2(points), points))
    if not rings:
        return []

    # Always keep the largest ring, so a municipality that is mostly a small
    # island keeps its outline; drop the specks that only add bytes.
    largest = max(range(len(rings)), key=lambda position: rings[position][0])
    return [
        _delta_encode(points)
        for position, (area, points) in enumerate(rings)
        if area >= min_ring_km2 or position == largest
    ]


def build_countries(
    gpkg: str | Path | None = None, target: str | Path = COUNTRY_FILE
) -> Path:
    """Country outlines of the base map, dissolved from the LAU file.

    The outlines are clipped to :data:`MAP_WINDOW` before they are simplified,
    so the overseas territories of FR, ES, PT and NL stay out of the picture.
    """
    source = find_lau_gpkg(gpkg)
    layer = _layer_name(source)
    sql = (
        'SELECT CNTR_CODE, '
        f"ST_SimplifyPreserveTopology(ST_Union(geom), {COUNTRY_SIMPLIFY_M}) AS geom "
        f'FROM "{layer}" '
        f"WHERE ST_Intersects(geom, ST_GeomFromText('{_window_wkt()}', 3035)) "
        "GROUP BY CNTR_CODE"
    )
    collection = _query_geojson(sql, source)

    codes: list[str] = []
    shapes: list[list[list[int]]] = []
    dropped = 0
    for feature in collection.get("features", []):
        code = str((feature.get("properties") or {}).get("CNTR_CODE") or "")
        rings = _encode_geometry(feature.get("geometry"))
        if not code or not rings:
            dropped += 1
            continue
        codes.append(code)
        shapes.append(rings)

    document = {
        "scale": RING_SCALE,
        "window": [int(value) for value in MAP_WINDOW],
        "codes": codes,
        "shapes": shapes,
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    if dropped:
        print(f"  {dropped} countries had no outline inside the window")
    return path


def build_lau_geometry(
    gpkg: str | Path | None = None,
    target: str | Path = LAU_GEO_FILE,
    lookup: str | Path | None = None,
    ids: Iterable[str] | None = None,
) -> Path:
    """Outlines of the municipalities that host at least one mapped facility.

    Municipalities without a facility carry no intensity to shade, so shipping
    them would multiply the size of the file for context only; the country
    outlines already provide it. ``ids`` are the municipality ids of the
    payload; without them every id of the lookup is drawn.
    """
    source = find_lau_gpkg(gpkg)
    layer = _layer_name(source)
    if ids is None:
        table = load_lau_lookup(lookup)
        ids = table["lau_id"].dropna()
    wanted = sorted({str(value) for value in ids if value})
    if not wanted:
        raise ValueError("No municipality ids to draw; rebuild the lookup first.")

    quoted = ",".join("'" + value.replace("'", "''") + "'" for value in wanted)
    # Small municipalities keep their exact outline: simplification at 1 km
    # would collapse them (see MIN_SIMPLIFY_M2).
    sql = (
        f"SELECT GISCO_ID, CASE WHEN ST_Area(geom) < {MIN_SIMPLIFY_M2} THEN geom "
        f"ELSE ST_SimplifyPreserveTopology(geom, {LAU_SIMPLIFY_M}) END AS geom "
        f'FROM "{layer}" WHERE GISCO_ID IN ({quoted})'
    )
    collection = _query_geojson(sql, source)

    ids: list[str] = []
    shapes: list[list[list[int]]] = []
    found: set[str] = set()
    empty: list[str] = []
    for feature in collection.get("features", []):
        lau_id = str((feature.get("properties") or {}).get("GISCO_ID") or "")
        rings = _encode_geometry(feature.get("geometry"))
        found.add(lau_id)
        if not lau_id or not rings:
            empty.append(lau_id or "?")
            continue
        ids.append(lau_id)
        shapes.append(rings)

    missing = [value for value in wanted if value not in found]
    document = {
        "scale": RING_SCALE,
        "ids": ids,
        "shapes": shapes,
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    if missing:
        print(
            f"  {_plural(len(missing), 'municipality', 'municipalities')} not in "
            f"the GISCO file: {', '.join(missing[:6])}"
            + (" ..." if len(missing) > 6 else "")
        )
    if empty:
        print(
            f"  {_plural(len(empty), 'municipality', 'municipalities')} left "
            "without a ring after quantisation"
        )
    return path


def build_geo(
    *,
    force: bool = False,
    gpkg: str | Path | None = None,
    lookup: str | Path | None = None,
    ids: Iterable[str] | None = None,
) -> list[Path]:
    """Rebuild ``countries.json`` and ``lau.json`` from the GISCO file.

    Both only change when the GISCO release changes, so an existing file is
    kept unless ``force`` is set: the step needs GDAL and takes a few seconds.
    ``ids`` restricts the municipalities to the ones the payload uses.
    """
    source = find_lau_gpkg(gpkg)
    print(f"  source: {source}")

    steps = (
        (Path(COUNTRY_FILE), lambda: build_countries(gpkg=source)),
        (
            Path(LAU_GEO_FILE),
            lambda: build_lau_geometry(gpkg=source, lookup=lookup, ids=ids),
        ),
    )
    written: list[Path] = []
    for path, step in steps:
        if path.is_file() and not force:
            print(f"  {path.name}: kept, {path.stat().st_size / 1e6:.2f} MB "
                  "(--force to rebuild)")
            written.append(path)
            continue
        started = time.perf_counter()
        step()
        elapsed = time.perf_counter() - started
        print(f"  {path.name}: {path.stat().st_size / 1e6:.2f} MB in {elapsed:.1f} s")
        written.append(path)
    return written


#: Point-in-polygon helper, run by :func:`build_lau_lookup` in the interpreter
#: that carries GDAL's Python bindings.
#:
#: The SQL route is unusably slow here: joining 33,000 coordinates against
#: 98,000 municipalities makes SQLite test every pair (some 3 billion
#: ``ST_Within`` calls, hours of work), while the OGR layer spatial filter -
#: which leans on the GeoPackage rtree - returns a handful of candidates per
#: point.
_JOIN_SCRIPT = r'''
import csv
import sys

from osgeo import ogr, osr

ogr.UseExceptions()

points_path, gpkg_path, target_path, layer_name = sys.argv[1:5]

source = ogr.Open(gpkg_path)
layer = source.GetLayerByName(layer_name)
if layer is None:
    raise SystemExit("layer %r not found in %s" % (layer_name, gpkg_path))
definition = layer.GetLayerDefn()
indices = {
    name: definition.GetFieldIndex(name)
    for name in ("GISCO_ID", "LAU_NAME", "CNTR_CODE", "AREA_KM2")
}

reference = osr.SpatialReference()
reference.ImportFromEPSG(3035)
reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
geo = osr.SpatialReference()
geo.ImportFromEPSG(4326)
geo.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
to_map = osr.CoordinateTransformation(geo, reference)

with open(points_path, newline="") as handle, open(
    target_path, "w", newline=""
) as out:
    reader = csv.DictReader(handle)
    writer = csv.writer(out)
    writer.writerow(["lon", "lat", "lau_id", "lau_name", "lau_cc", "lau_area"])
    for row in reader:
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(float(row["lon"]), float(row["lat"]))
        point.Transform(to_map)

        layer.SetSpatialFilter(point)
        match = None
        for feature in layer:
            if feature.GetGeometryRef().Contains(point):
                match = feature
                break
        if match is None:
            # A coordinate exactly on a boundary: name the municipality rather
            # than leaving the facility out of the layer.
            layer.SetSpatialFilter(point)
            for feature in layer:
                if feature.GetGeometryRef().Intersects(point):
                    match = feature
                    break
        layer.SetSpatialFilter(None)

        if match is None:
            writer.writerow([row["lon"], row["lat"], "", "", "", ""])
        else:
            writer.writerow(
                [row["lon"], row["lat"]]
                + [match.GetField(index) for index in indices.values()]
            )
'''


def _ogr_interpreter() -> str:
    """Interpreter whose GDAL bindings can run :data:`_JOIN_SCRIPT`.

    The Quarto environment usually has no GDAL bindings, while the system
    interpreter that ships the ``gdal-bin`` package does; the join is therefore
    handed to whichever interpreter can import ``osgeo``.
    """
    candidates = [
        sys.executable,
        str(Path(sys.base_prefix) / "bin" / "python3"),
        "python3",
    ]
    tried: list[str] = []
    for candidate in candidates:
        if candidate in tried:
            continue
        tried.append(candidate)
        resolved = shutil.which(candidate)
        if resolved is None:
            continue
        probe = subprocess.run(
            [resolved, "-c", "import osgeo"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return resolved
    raise RuntimeError(
        "The LAU lookup needs GDAL's Python bindings (python3-gdal). Tried: "
        + ", ".join(tried)
    )


def build_lau_lookup(
    target: str | Path | None = None,
    *,
    data: str | Path | None = None,
    gpkg: str | Path | None = None,
) -> Path:
    """Rebuild the facility-to-municipality table (needs GDAL).

    One point-in-polygon join of every distinct coordinate in the extract on the
    GISCO LAU file. The result is cached next to the GeoPackage and read by
    :func:`build_payload` and :func:`build_lau_geometry`; rebuilding it is only
    necessary when the extract or the GISCO release changes.
    """
    destination = Path(target) if target else LAU_LOOKUP_CANDIDATES[0]
    source = find_lau_gpkg(gpkg)
    layer = _layer_name(source)

    frame = pd.read_csv(find_data_file(data), usecols=["Longitude", "Latitude"])
    points = (
        pd.DataFrame(
            {
                "lon": pd.to_numeric(frame["Longitude"], errors="coerce"),
                "lat": pd.to_numeric(frame["Latitude"], errors="coerce"),
            }
        )
        .dropna()
        .drop_duplicates()
        .sort_values(["lon", "lat"])
    )

    interpreter = _ogr_interpreter()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        points_file = Path(scratch) / "points.csv"
        points.to_csv(points_file, index=False)
        completed = subprocess.run(
            [
                interpreter,
                "-c",
                _JOIN_SCRIPT,
                str(points_file),
                str(source),
                str(destination),
                layer,
            ],
            capture_output=True,
            text=True,
        )
    if completed.returncode:
        raise RuntimeError(
            "The LAU point-in-polygon join failed: "
            + (completed.stderr.strip()[-600:] or "unknown error")
        )

    print(f"  {destination}: {len(points):,} distinct coordinates")
    return destination


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def build_cli() -> argparse.ArgumentParser:
    """Argument parser of the module (see the module docstring)."""
    parser = argparse.ArgumentParser(
        prog="facility_map_data.py",
        description=(
            "Build the JSON payload and the base geometry of the EU facility map."
        ),
    )
    parser.add_argument(
        "--geo",
        action="store_true",
        help="rebuild assets/map/{countries,lau}.json (needs GDAL)",
    )
    parser.add_argument(
        "--lookup",
        action="store_true",
        help="rebuild LAU/lau_lookup.csv (needs GDAL)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="payload, geometry and lookup in one pass",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild the payload and the geometry files even when they exist",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help="path to eprtr_enhanced.csv (default: next to the notebook)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = build_cli().parse_args(argv)

    try:
        if args.lookup or args.all:
            build_lau_lookup(data=args.data)

        target = Path(PAYLOAD_FILE)
        if payload_is_fresh(data=args.data, force=args.force):
            # Nothing below it changed, so this build would write the same file:
            # keep it, and its mtime, and let the render move on.
            print(
                f"  {target.name}: {target.stat().st_size / 1e6:.2f} MB, up to "
                "date (--force rebuilds it)"
            )
            payload = None
        else:
            started = time.perf_counter()
            payload = build_payload(args.data)
            elapsed = time.perf_counter() - started
            target = write_payload(payload)
            print(f"  {target.name}: rebuilt in {elapsed:.1f} s")

        if args.geo or args.all:
            # Only the municipalities the payload draws need an outline.
            build_geo(force=args.force, ids=_payload_lau_ids(payload))
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if payload is None:
        return 0

    meta = payload["meta"]
    print(
        f"  {target.name}: {target.stat().st_size / 1e6:.2f} MB \u00b7 "
        f"{_plural(meta['facilityCount'], 'facility', 'facilities')} \u00b7 "
        f"{_plural(meta['lauCount'], 'municipality', 'municipalities')}"
    )
    for view in payload["views"]:
        for medium, measure in view["measures"].items():
            print(
                f"    {view['id']:<13} {medium:<9} "
                f"{measure['facilities']:>6} facilities \u00b7 "
                f"max {measure['maxKg'] / measure['factor']:.1f} "
                f"{measure['unit']} \u00b7 total "
                f"{measure['totalKg'] / measure['factor']:.1f} "
                f"{measure['unit']}"
            )
    if meta["missingCoordinates"]:
        print(
            f"    {meta['missingCoordinates']:,} facilities have no usable "
            "coordinates and are not on the map"
        )
    if meta["offMap"]:
        print(
            f"    {meta['offMap']:,} facilities fall outside the map window and "
            "are not drawn"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

