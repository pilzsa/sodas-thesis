"""Data preparation and a Plotly ranking of E-PRTR facilities.

The module turns the classified E-PRTR extract into one Plotly figure:
horizontal stacked bars for the 25 facilities with the largest cumulative
mass in 2022-2024, with a button switch between three views

* ``Releases excl. CO2`` - every pollutant except CO2, air and water combined;
* ``Hazardous mass`` - only pollutants flagged as hazardous in the dataset,
  with greenhouse gases excluded;
* ``Carcinogenic mass`` - only substances classified as carcinogenic (IARC
  Group 1 or 2A, or a CLP ``Carc.`` class), with greenhouse gases excluded.

CO2 is not a view of its own: at 3.4 Gt it is roughly sixty times the mass of
everything else and 98% of all reported mass, so inside a stack it flattens
every bar into one colour. The charts report the CO2 left out in their hover
boxes instead.

``CO2 excluding biomass`` is a memo row that reports the fossil share of the
``CO2`` row, so it is dropped before any summing; keeping it would add roughly
a quarter of a billion tonnes of phantom CO2 and reshuffle the ranking.

Each view ranks its own top facilities, and each bar sums to that facility's
cumulative mass in the view. Pollutants that never reach ``MIN_STACK_SHARE`` of
the longest bar are pooled into one ``Other pollutants`` segment, which keeps
the axis and the legend readable without hiding mass: the hover text still
reports the exact figure.

The Quarto site draws the same three views with a React + D3 component
(:mod:`facility_chart_data` supplies its JSON); this module stays as the
standalone, exportable version of the chart.

Run the file directly for a standalone browser view::

    python facility_emission_ranking.py

or import it from a notebook::

    from facility_emission_ranking import build_figure

    build_figure(eprtr_classified).show()
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from palette import OTHER, SERIES

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

START_YEAR = 2022
END_YEAR = 2024
TOP_N = 25
#: Substances a stack carries on its own. Five is what the chart set separates
#: (see :data:`palette.SERIES`); anything thinner or smaller is pooled, so a
#: legend never shows two chips of the same colour.
TOP_POLLUTANTS = 5
LABEL_WIDTH = 44  # characters kept from the facility name on the y-axis

#: A pollutant is stacked on its own only if its largest slice reaches this
#: share of the longest bar; thinner slices are pooled, because they would be
#: a couple of pixels wide and only clutter the legend.
MIN_STACK_SHARE = 0.005

DATA_FILE = "eprtr_enhanced.csv"
KG_PER_MEGATONNE = 1e9  # E-PRTR Releases are reported in kg per year

OTHER_LABEL = "Other pollutants"
OTHER_FULL_LABEL = "Other pollutants (too small to stack on their own)"

#: Memo rows that repeat a figure already reported under another pollutant.
#: ``CO2 excluding biomass`` is the fossil share of the ``CO2`` row (it never
#: exceeds it in the extract), so summing both would double count emissions.
MEMO_POLLUTANTS = frozenset({"Carbon dioxide (CO2) excluding biomass"})

CLASSIFICATION_COLUMNS = (
    "hap",
    "cand_svhc",
    "wfd_prio_sub",
    "iarc_group",
    "clp_class",
)

#: IARC groups that count as carcinogenic in the carcinogenic view: Group 1
#: ("carcinogenic to humans") and Group 2A ("probably carcinogenic"). Group 2B
#: ("possibly") is left to the hazardous view, where most of those substances
#: already sit through their WFD status; adding 2B here would turn the
#: carcinogenic ranking into a copy of the hazardous one.
CARCINOGENIC_IARC_GROUPS = frozenset({"1", "2A"})

#: A CLP "Carc." class (1A, 1B or 2) also marks a substance as carcinogenic.
CLP_CARCINOGEN_PATTERN = re.compile(r"Carc\.", re.IGNORECASE)

#: Kyoto-basket greenhouse gases: excluded from the hazardous view.
GREENHOUSE_GASES = frozenset(
    {
        "Carbon dioxide (CO2)",
        "Carbon dioxide (CO2) excluding biomass",
        "Methane (CH4)",
        "Nitrous oxide (N2O)",
        "Hydro-fluorocarbons (HFCS)",
        "Perfluorocarbons (PFCs)",
        "Sulphur hexafluoride (SF6)",
    }
)


#: Short labels for the legend; unlisted pollutants fall back to a cleaned
#: version of their E-PRTR name.
POLLUTANT_SHORT_LABELS = {
    "Carbon dioxide (CO2)": "CO\u2082",
    "Carbon dioxide (CO2) excluding biomass": "CO\u2082 (excl. biomass)",
    "Carbon monoxide (CO)": "CO",
    "Sulphur oxides (SOX)": "SO\u2093",
    "Nitrogen oxides (NOX)": "NO\u2093",
    "Ammonia (NH3)": "NH\u2083",
    "Total organic carbon(as total C or COD/3) (TOC)": "TOC",
    "Particulate matter (PM10)": "PM\u2081\u2080",
    "Fine particulate matter (PM2.5)": "PM\u2082.\u2085",
    "Chlorides (as total Cl)": "Chlorides",
    "Total nitrogen": "Total N",
    "Total phosphorus": "Total P",
    "Non-methane volatile organic compounds (NMVOC)": "NMVOC",
    "Halogenated organic compounds (as AOX)": "AOX",
    "Zinc and compounds (as Zn)": "Zinc",
    "Copper and compounds (as Cu)": "Copper",
    "Lead and compounds (as Pb)": "Lead",
    "Nickel and compounds (as Ni)": "Nickel",
    "Chromium and compounds (as Cr)": "Chromium",
    "Cadmium and compounds (as Cd)": "Cadmium",
    "Mercury and compounds (as Hg)": "Mercury",
    "Arsenic and compounds (as As)": "Arsenic",
    "Hydrogen cyanide (HCN)": "HCN",
    "Cyanides (as total CN)": "Cyanides",
    "Polycyclic aromatic hydrocarbons (PAHs)": "PAHs",
    "Polychlorinated biphenyls (PCBs)": "PCBs",
    "Benzene": "Benzene",
    "Toluene": "Toluene",
    "Xylenes": "Xylenes",
    "Dichloromethane (DCM)": "DCM",
    "Trichloromethane": "Chloroform",
    "Tetrachloromethane (TCM)": "TCM",
    "Trichloroethylene (TRI)": "Trichloroethylene",
    "Tetrachloroethylene": "Perchloroethylene",
    "Vinyl chloride": "Vinyl chloride",
    "Ethyl benzene": "Ethylbenzene",
    "Di-(2-ethyl hexyl) phthalate (DEHP)": "DEHP",
    "PCDD + PCDF (dioxins + furans) (as Teq)": "Dioxins/furans (TEQ)",
}

#: Categorical series colours, from the page palette (:mod:`palette`): one hue
#: per stack slot, Bubblegum Pink for the largest slice down to Dark Teal.
#: Both figures assign them by *stack slot* within the ranking on screen, not by
#: substance, because the three views draw from different substance pools: a
#: global name-to-colour table repeated itself inside a single legend.
SERIES_COLORS = SERIES

#: The pooled bucket keeps the only achromatic mark, i.e. the quietest slot.
OTHER_COLOR = OTHER


FONT_FAMILY = "Inter, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
TEXT_COLOR = "#17212c"
MUTED_TEXT_COLOR = "#536273"
GRID_COLOR = "#d7dfe8"

CO2_POLLUTANT = "Carbon dioxide (CO2)"

TOTAL_TITLE = (
    "<b>Top 25 facilities by releases excluding CO\u2082, 2022\u20132024</b><br>"
    f"<span style='font-size:12px;color:{MUTED_TEXT_COLOR}'>"
    "Every reported pollutant except CO\u2082 \u00b7 air and water releases "
    "combined \u00b7 cumulative mass in million tonnes (Mt)"
    "</span>"
)
HAZARDOUS_TITLE = (
    "<b>Top 25 facilities by hazardous releases, 2022\u20132024</b><br>"
    f"<span style='font-size:12px;color:{MUTED_TEXT_COLOR}'>"
    "Pollutants flagged as hazardous in the dataset \u00b7 greenhouse gases "
    "excluded \u00b7 cumulative mass in million tonnes (Mt)"
    "</span>"
)
CARCINOGENIC_TITLE = (
    "<b>Top 25 facilities by carcinogenic releases, 2022\u20132024</b><br>"
    f"<span style='font-size:12px;color:{MUTED_TEXT_COLOR}'>"
    "Substances classified as carcinogenic (IARC Group 1 or 2A, or a CLP "
    "Carc. class) \u00b7 greenhouse gases excluded \u00b7 cumulative mass in "
    "tonnes"
    "</span>"
)

TOTAL_AXIS_TITLE = "Cumulative releases"
HAZARDOUS_AXIS_TITLE = "Cumulative hazardous releases"
CARCINOGENIC_AXIS_TITLE = "Cumulative carcinogenic releases"

HOVER_TEMPLATE = (
    "<b>%{customdata[0]}</b><br>"
    "%{customdata[1]}<br><br>"
    "%{customdata[2]}: <b>%{customdata[4]}</b> "
    "(%{customdata[3]} of %{customdata[5]} for this facility)<br>"
    f"<span style='font-size:11px;color:{MUTED_TEXT_COLOR}'>%{{customdata[6]}}</span>"
    "<extra></extra>"
)


# --------------------------------------------------------------------------
# Data preparation
# --------------------------------------------------------------------------


def find_data_file(path: str | Path | None = None) -> Path:
    """Return the path of the classified E-PRTR extract.

    Resolution order: an explicit ``path``, the ``EPRTR_DATA`` environment
    variable, the working directory, its parent (Quarto runs from the
    project folder) and finally the module location.
    """
    if path is not None:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(f"E-PRTR file not found: {candidate}")
        return candidate

    candidates: list[Path] = []
    env_path = os.environ.get("EPRTR_DATA")
    if env_path:
        candidates.append(Path(env_path).expanduser())

    here = globals().get("__file__")
    bases = [Path.cwd(), Path.cwd().parent]
    if here:
        bases += [Path(here).resolve().parent, Path(here).resolve().parent.parent]
    candidates += [base / DATA_FILE for base in bases]

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    searched = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise FileNotFoundError(
        f"Could not locate {DATA_FILE}. Set the EPRTR_DATA environment variable "
        f"or pass an explicit path. Searched:\n{searched}"
    )


def load_releases(
    path: str | Path | None = None,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
) -> pd.DataFrame:
    """Read the E-PRTR extract and return the prepared release table."""
    source = find_data_file(path)
    usecols = [
        "reportingYear",
        "FacilityInspireId",
        "facilityName",
        "city",
        "countryName",
        "Pollutant",
        "Releases",
        *CLASSIFICATION_COLUMNS,
    ]
    frame = pd.read_csv(source, usecols=usecols, low_memory=False)
    return prepare_releases(frame, start_year=start_year, end_year=end_year)


def prepare_releases(
    frame: pd.DataFrame,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
) -> pd.DataFrame:
    """Filter years, clean the mass column and classify hazardous releases.

    Returns a copy with the additional columns ``is_hazardous``,
    ``is_carcinogenic``, ``facility_label``, ``facility_full`` and
    ``facility_place``.
    """
    required = {
        "reportingYear",
        "FacilityInspireId",
        "facilityName",
        "city",
        "countryName",
        "Pollutant",
        "Releases",
        *CLASSIFICATION_COLUMNS,
    }
    missing = required.difference(frame.columns)
    if missing:
        raise KeyError(f"Missing required columns: {sorted(missing)}")

    data = frame.copy()

    data["reportingYear"] = pd.to_numeric(data["reportingYear"], errors="coerce")
    data = data[data["reportingYear"].between(start_year, end_year)]

    data["Releases"] = pd.to_numeric(data["Releases"], errors="coerce")
    data = data[data["Releases"].notna() & (data["Releases"] >= 0)]

    data["FacilityInspireId"] = (
        data["FacilityInspireId"].astype("string").str.strip()
    )
    identifiers = data["FacilityInspireId"]
    valid_id = (identifiers.notna() & identifiers.str.len().gt(0)).fillna(False)
    data = data[valid_id & data["Pollutant"].notna()]
    data = data[~data["Pollutant"].isin(MEMO_POLLUTANTS)]

    data["is_hazardous"] = _hazard_mask(data)
    data["is_carcinogenic"] = _carcinogenic_mask(data)
    data = _attach_facility_labels(data)
    return data.reset_index(drop=True)


def _hazard_mask(data: pd.DataFrame) -> pd.Series:
    """Flag rows the dataset classifies as hazardous (GHGs excluded)."""
    flag = pd.Series(False, index=data.index)

    for column in ("hap", "cand_svhc", "wfd_prio_sub"):
        flag |= data[column].fillna(False).astype(bool)

    for column in ("iarc_group", "clp_class"):
        flag |= _is_present(data[column])

    return flag & ~data["Pollutant"].isin(GREENHOUSE_GASES)


def _carcinogenic_mask(data: pd.DataFrame) -> pd.Series:
    """Flag rows whose substance is classified as carcinogenic.

    Either the IARC group is 1 or 2A, or the CLP string carries a ``Carc.``
    class. Greenhouse gases stay out, as in the hazardous mask.
    """
    groups = data["iarc_group"].astype("string").str.strip().str.upper()
    by_iarc = groups.isin(CARCINOGENIC_IARC_GROUPS)
    by_clp = data["clp_class"].astype("string").str.contains(
        CLP_CARCINOGEN_PATTERN, na=False
    )
    return (by_iarc | by_clp) & ~data["Pollutant"].isin(GREENHOUSE_GASES)


def _is_present(series: pd.Series) -> pd.Series:
    """True where a text column holds an actual classification (not NaN)."""
    text = series.astype("string").str.strip().str.lower()
    return text.notna() & ~text.isin({"", "nan", "none", "n/a", "na", "-"})


def _attach_facility_labels(data: pd.DataFrame) -> pd.DataFrame:
    """Add display labels taken from the most recent reporting year."""
    latest = data.sort_values("reportingYear")
    grouped = latest.groupby("FacilityInspireId", observed=True)

    names = grouped["facilityName"].last().astype("string").str.strip()
    fallback = pd.Series("Unknown facility", index=names.index, dtype="string")
    has_name = (names.notna() & names.str.len().gt(0)).fillna(False)
    names = names.where(has_name, fallback)

    cities = grouped["city"].last().astype("string").str.strip()
    countries = grouped["countryName"].last().astype("string").str.strip()

    labels = {
        fid: _truncate(name, LABEL_WIDTH) for fid, name in names.items()
    }
    labels = _disambiguate_labels(labels, cities)
    places = {}
    for fid in names.index:
        parts = [
            value for value in (cities.get(fid), countries.get(fid)) if _is_text(value)
        ]
        places[fid] = ", ".join(parts) if parts else "Location not reported"

    data["facility_full"] = data["FacilityInspireId"].map(names)
    data["facility_label"] = data["FacilityInspireId"].map(labels)
    data["facility_place"] = data["FacilityInspireId"].map(places)
    return data


def _is_text(value: object) -> bool:
    """True for a non-empty string that is not a disguised missing value."""
    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    return bool(cleaned) and cleaned.lower() not in {"nan", "none", "n/a", "na"}


def _truncate(text: object, width: int) -> str:
    """Collapse whitespace and shorten long names at a word boundary."""
    cleaned = re.sub(r"\s+", " ", str(text)).strip()
    if len(cleaned) <= width:
        return cleaned
    cut = cleaned[: width - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "\u2026"


def _unique_labels(labels: dict[str, str]) -> dict[str, str]:
    """Disambiguate labels that truncate to the same string."""
    seen: dict[str, int] = {}
    unique: dict[str, str] = {}
    for key, label in labels.items():
        count = seen.get(label, 0) + 1
        seen[label] = count
        unique[key] = label if count == 1 else f"{label} [{count}]"
    return unique


def _short_city(city: object, width: int = 18) -> str:
    """City name trimmed to fit the y-axis label."""
    cleaned = re.sub(r"\s+", " ", str(city)).strip()
    if len(cleaned) <= width:
        return cleaned
    first_word = cleaned.split(" ")[0]
    return first_word if len(first_word) <= width else _truncate(first_word, width)


def _disambiguate_labels(
    labels: dict[str, str], cities: pd.Series
) -> dict[str, str]:
    """Add the city to facility names that truncate to the same label."""
    counts = Counter(labels.values())
    disambiguated: dict[str, str] = {}
    for fid, label in labels.items():
        city = cities.get(fid)
        if counts[label] > 1 and _is_text(city):
            suffix = f" ({_short_city(city)})"
            label = _truncate(label, LABEL_WIDTH - len(suffix)) + suffix
        disambiguated[fid] = label
    return _unique_labels(disambiguated)


# --------------------------------------------------------------------------
# View construction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FacilityView:
    """One ranking (total, hazardous or carcinogenic) ready to be plotted.

    The rows are facilities unless :func:`_build_view` was given another
    ``group_column``; then ``labels``, ``full_names`` and ``places`` describe
    E-PRTR sectors or IED activities instead.
    """

    key: str
    title: str
    axis_title: str
    labels: list[str]
    full_names: list[str]
    places: list[str]
    matrix: pd.DataFrame  # index: facility id, columns: short pollutant label
    full_labels: dict[str, str]  # short pollutant label -> E-PRTR name
    totals: pd.Series  # cumulative mass per facility, kg
    unit_scale: float = KG_PER_MEGATONNE  # kilograms in one axis unit
    unit: str = "Mt"

    @property
    def y_order(self) -> list[str]:
        """Top-ranked facility first, matching the vertical axis."""
        return list(reversed(self.labels))

    @property
    def x_max(self) -> float:
        return float(self.totals.max()) / self.unit_scale * 1.16


#: Axis units, largest first: (kilograms in one unit, symbol, spoken name).
AXIS_UNITS = (
    (1e9, "Mt", "million tonnes"),
    (1e6, "kt", "thousand tonnes"),
    (1e3, "t", "tonnes"),
)


def _unit_for(max_kg: float) -> tuple[float, str, str]:
    """Axis unit that keeps the longest bar between 1 and 1000 units."""
    for factor, symbol, name in AXIS_UNITS:
        if max_kg >= factor:
            return factor, symbol, name
    return 1.0, "kg", "kilograms"


def _with_unit(
    view: FacilityView, start_year: int = START_YEAR, end_year: int = END_YEAR
) -> FacilityView:
    """Attach the display unit that fits the view's largest bar."""
    largest = float(view.totals.max()) if len(view.totals) else 0.0
    factor, symbol, name = _unit_for(largest)
    return replace(
        view,
        unit_scale=factor,
        unit=symbol,
        axis_title=f"{view.axis_title}, {start_year}\u2013{end_year} ({name})",
    )


def _build_view(
    data: pd.DataFrame,
    mask: pd.Series,
    key: str,
    title: str,
    axis_title: str,
    top_n: int = TOP_N,
    top_pollutants: int = TOP_POLLUTANTS,
    min_share: float = MIN_STACK_SHARE,
    *,
    group_column: str = "FacilityInspireId",
    row_info: dict[str, dict[str, str]] | None = None,
    row_order: list[str] | None = None,
) -> FacilityView:
    """Rank the rows of ``group_column``, keep the top pollutants and pivot.

    Rows are facilities by default. Passing another column - an E-PRTR sector
    or an IED activity - ranks those groups instead. ``row_info`` then supplies
    the label, full name and subtitle of every group, because the facility
    attributes in the frame no longer describe the rows; ``row_order`` fixes the
    row order, for a ranking whose pooled row belongs at the bottom whatever
    its mass.
    """
    subset = data[mask]
    totals = (
        subset.groupby(group_column, observed=True)["Releases"]
        .sum()
        .sort_values(ascending=False)
    )
    if row_order is not None:
        order = pd.Index([value for value in row_order if value in totals.index])
    else:
        order = totals.head(top_n).index
    subset = subset[subset[group_column].isin(order)]

    pollutant_mass = (
        subset.groupby("Pollutant", observed=True)["Releases"]
        .sum()
        .sort_values(ascending=False)
    )

    # A pollutant survives when its largest row-level mass is visible on the
    # axis, i.e. at least ``min_share`` of the longest bar.
    per_row = (
        subset.groupby([group_column, "Pollutant"], observed=True)["Releases"]
        .sum()
        .unstack(fill_value=0.0)
        .reindex(index=order, fill_value=0.0)
    )
    scale = float(per_row.sum(axis=1).max())
    visible = per_row.max(axis=0).div(scale) if scale else per_row.max(axis=0)

    kept = [
        pollutant
        for pollutant in pollutant_mass.index
        if visible.get(pollutant, 0.0) >= min_share
    ][:top_pollutants]
    if not kept:
        kept = [pollutant_mass.index[0]]

    series = np.where(subset["Pollutant"].isin(kept), subset["Pollutant"], OTHER_LABEL)
    bucket = subset.assign(_series=series)

    matrix = bucket.pivot_table(
        index=group_column,
        columns="_series",
        values="Releases",
        aggfunc="sum",
        fill_value=0.0,
    )
    matrix = matrix.reindex(index=order, columns=[*kept, OTHER_LABEL], fill_value=0.0)

    if OTHER_LABEL in matrix.columns and not matrix[OTHER_LABEL].any():
        matrix = matrix.drop(columns=OTHER_LABEL)

    if row_info is None:
        metadata = subset.drop_duplicates(group_column).set_index(group_column)
        metadata = metadata.reindex(order)
        labels = [str(value) for value in metadata["facility_label"]]
        full_names = [str(value) for value in metadata["facility_full"]]
        places = [str(value) for value in metadata["facility_place"]]
    else:
        rows = [row_info.get(str(value), {}) for value in order]
        labels = [str(row.get("label", value)) for row, value in zip(rows, order)]
        full_names = [str(row.get("name", value)) for row, value in zip(rows, order)]
        places = [str(row.get("place", "")) for row in rows]

    short = {
        column: (
            OTHER_LABEL if column == OTHER_LABEL else _short_pollutant(column)
        )
        for column in matrix.columns
    }
    short = _unique_labels(short)
    full_labels = {
        short[column]: (OTHER_FULL_LABEL if column == OTHER_LABEL else column)
        for column in matrix.columns
    }
    matrix = matrix.rename(columns=short)

    return FacilityView(
        key=key,
        title=title,
        axis_title=axis_title,
        labels=labels,
        full_names=full_names,
        places=places,
        matrix=matrix,
        full_labels=full_labels,
        totals=matrix.sum(axis=1),
    )


def _short_pollutant(name: str) -> str:
    """Legend-sized label for a pollutant that has no explicit mapping."""
    if name in POLLUTANT_SHORT_LABELS:
        return POLLUTANT_SHORT_LABELS[name]
    base = re.sub(r"\s*\([^)]*\)", "", name).strip()
    base = re.sub(r"\s+and compounds\b.*$", "", base, flags=re.IGNORECASE)
    return _truncate(base or name, 26)


def _pollutant_color(short_name: str, index: int) -> str:
    """Colour for one stack slot; the pooled bucket stays neutral.

    ``index`` is the slot in the stack, so the largest slice takes the first
    hue. The set has one colour per slot and no wrapping: a sixth substance
    would silently reuse the first colour, which is exactly the collision this
    palette exists to prevent.
    """
    if short_name == OTHER_LABEL:
        return OTHER_COLOR
    if index >= len(SERIES_COLORS):
        raise ValueError(
            f"{len(SERIES_COLORS)} series colours for {index + 1} stacked "
            "substances: extend palette.SERIES or lower TOP_POLLUTANTS"
        )
    return SERIES_COLORS[index]


def _format_mt(kilograms: float) -> str:
    """Format a mass in million tonnes, switching units for tiny values."""
    value = kilograms / KG_PER_MEGATONNE
    if value >= 100:
        return f"{value:,.0f} Mt"
    if value >= 10:
        return f"{value:,.1f} Mt"
    if value >= 1:
        return f"{value:,.2f} Mt"
    if value >= 0.001:
        return f"{value:,.3f} Mt"
    tonnes = kilograms / 1e3
    if tonnes >= 1:
        return f"{tonnes:,.0f} t"
    return f"{kilograms:,.0f} kg"


def _format_in_unit(kilograms: float, view: FacilityView) -> str:
    """Bar-end label in the view's own axis unit, without the unit itself."""
    value = kilograms / view.unit_scale
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 10:
        return f"{value:,.1f}"
    if value >= 1:
        return f"{value:,.2f}"
    return f"{value:,.3f}"


# --------------------------------------------------------------------------
# Substance classification
# --------------------------------------------------------------------------

#: IARC groups, with the wording of the monograph preamble.
IARC_GROUPS = {
    "1": (
        "carcinogenic to humans",
        "IARC Group 1 \u2014 carcinogenic to humans",
    ),
    "2A": (
        "probably carcinogenic to humans",
        "IARC Group 2A \u2014 probably carcinogenic to humans",
    ),
    "2B": (
        "possibly carcinogenic to humans",
        "IARC Group 2B \u2014 possibly carcinogenic to humans",
    ),
    "3": (
        "not classifiable as to its carcinogenicity",
        "IARC Group 3 \u2014 not classifiable as to its carcinogenicity",
    ),
    "4": (
        "probably not carcinogenic to humans",
        "IARC Group 4 \u2014 probably not carcinogenic to humans",
    ),
}

#: Plain-language reading of the CLP classes that describe chronic health
#: hazards. Only ``Carc.``, ``Muta.`` and ``Repr.`` are quoted, because those
#: are the classifications the rankings are about.
CLP_CLASS_PHRASES = {
    "Carc. 1A": "known human carcinogen",
    "Carc. 1B": "presumed human carcinogen",
    "Carc. 2": "suspected human carcinogen",
    "Muta. 1A": "may cause genetic defects",
    "Muta. 1B": "may cause genetic defects",
    "Muta. 2": "suspected of causing genetic defects",
    "Repr. 1A": "may damage fertility or the unborn child",
    "Repr. 1B": "may damage fertility or the unborn child",
    "Repr. 2": "suspected of damaging fertility or the unborn child",
}

#: ``Carc. 1A``, ``Muta. 1B``, ``Repr. 2`` ... inside a CLP string.
CLP_CLASS_PATTERN = re.compile(r"(?:Carc|Muta|Repr)\.\s*\d[AB]?")

#: Classification flags of the dataset: (column, tag kind, badge, phrase,
#: tooltip). They are quoted after the IARC and CLP classes.
CLASSIFICATION_FLAGS = (
    (
        "hap",
        "hap",
        "HAP",
        "hazardous air pollutant",
        "Hazardous air pollutant (HAP)",
    ),
    (
        "cand_svhc",
        "svhc",
        "SVHC",
        "substance of very high concern",
        "REACH candidate list: substance of very high concern",
    ),
    (
        "wfd_prio_sub",
        "wfd",
        "WFD priority",
        "Water Framework Directive priority substance",
        "Priority substance under the Water Framework Directive",
    ),
)


def _first_text(series: pd.Series) -> str:
    """First non-empty text value in a column, or an empty string."""
    text = series.astype("string").str.strip()
    text = text[text.notna() & text.str.len().gt(0)]
    return str(text.iloc[0]) if len(text) else ""


def _any_true(series: pd.Series) -> bool:
    """True when a flag column holds at least one true value."""
    values = series.astype("string").str.strip().str.lower()
    return bool(values.isin({"true", "1", "yes", "y", "t"}).any())


def _clp_text(clp: str) -> str:
    """One-line version of a multi-line CLP string."""
    return re.sub(r"\s*[\r\n]+\s*", " \u00b7 ", clp).strip()


def _clp_classes(clp: str) -> list[str]:
    """Carc./Muta./Repr. classes in a CLP string, in source order, unique."""
    found = [
        re.sub(r"\s+", " ", match.group(0)) for match in CLP_CLASS_PATTERN.finditer(clp)
    ]
    return list(dict.fromkeys(found))


def describe_substances(data: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Summarise the classification assigned to every pollutant.

    One entry per E-PRTR pollutant name, with

    * ``tags``  - compact badges for the hover box (``kind``, ``text`` and a
      ``title`` that spells the classification out): IARC group, CLP
      carcinogenicity, mutagenicity and reprotoxicity classes, HAP, SVHC and
      WFD priority status;
    * ``note``  - the same information as one sentence, e.g. "IARC Group 1
      (carcinogenic to humans); CLP Carc. 1A (known human carcinogen)";
    * ``hazardous`` / ``carcinogenic`` - whether the dataset flags the
      pollutant, so the component can mark a substance as unclassified.

    The descriptions are read from the classification columns themselves, so
    they stay in step with the data instead of a hand-written list.
    """
    descriptions: dict[str, dict[str, Any]] = {}

    for pollutant, rows in data.groupby("Pollutant", observed=True):
        iarc = _first_text(rows["iarc_group"]).upper()
        clp = _clp_text(_first_text(rows["clp_class"]))
        classes = _clp_classes(clp)

        tags: list[dict[str, str]] = []
        sentences: list[str] = []

        if iarc:
            readable, long = IARC_GROUPS.get(
                iarc, ("carcinogenicity not specified", f"IARC Group {iarc}")
            )
            tags.append({"kind": "iarc", "text": f"IARC {iarc}", "title": long})
            sentences.append(f"IARC Group {iarc} ({readable})")

        for name in classes:
            tags.append(
                {
                    "kind": "clp",
                    "text": f"CLP {name}",
                    "title": f"CLP classification: {clp}",
                }
            )
            sentences.append(
                f"CLP {name} ({CLP_CLASS_PHRASES.get(name, 'health hazard')})"
            )

        for column, kind, badge, phrase, title in CLASSIFICATION_FLAGS:
            if _any_true(rows[column]):
                tags.append({"kind": kind, "text": badge, "title": title})
                sentences.append(phrase)

        note = (
            "; ".join(sentences)
            if sentences
            else "No hazard classification in the dataset."
        )
        descriptions[str(pollutant)] = {
            "iarc": iarc,
            "clp": clp,
            "tags": tags,
            "note": note[0].upper() + note[1:],
            "hazardous": bool(rows["is_hazardous"].any()),
            "carcinogenic": bool(rows["is_carcinogenic"].any()),
        }

    return descriptions


# --------------------------------------------------------------------------
# Figure
# --------------------------------------------------------------------------


def build_figure(
    data: pd.DataFrame | str | Path | None = None,
    *,
    top_n: int = TOP_N,
    top_pollutants: int = TOP_POLLUTANTS,
    min_share: float = MIN_STACK_SHARE,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
) -> go.Figure:
    """Build the three-view stacked bar chart.

    ``data`` may be a prepared or raw DataFrame (e.g. the ``eprtr_classified``
    table already loaded in the notebook) or a path to the CSV.
    """
    if data is None or isinstance(data, (str, Path)):
        frame = load_releases(data, start_year=start_year, end_year=end_year)
    else:
        frame = prepare_releases(data, start_year=start_year, end_year=end_year)

    if frame.empty:
        raise ValueError("No releases left after filtering; check the input data.")

    total_view = _build_view(
        frame,
        ~frame["Pollutant"].eq(CO2_POLLUTANT),
        key="total",
        title=TOTAL_TITLE,
        axis_title=TOTAL_AXIS_TITLE,
        top_n=top_n,
        top_pollutants=top_pollutants,
        min_share=min_share,
    )
    hazardous_view = _build_view(
        frame,
        frame["is_hazardous"],
        key="hazardous",
        title=HAZARDOUS_TITLE,
        axis_title=HAZARDOUS_AXIS_TITLE,
        top_n=top_n,
        top_pollutants=top_pollutants,
        min_share=min_share,
    )
    carcinogenic_view = _build_view(
        frame,
        frame["is_carcinogenic"],
        key="carcinogenic",
        title=CARCINOGENIC_TITLE,
        axis_title=CARCINOGENIC_AXIS_TITLE,
        top_n=top_n,
        top_pollutants=top_pollutants,
        min_share=min_share,
    )

    figure = go.Figure()
    total_view = _with_unit(total_view, start_year, end_year)
    hazardous_view = _with_unit(hazardous_view, start_year, end_year)
    carcinogenic_view = _with_unit(carcinogenic_view, start_year, end_year)
    classifications = describe_substances(frame)
    total_count = _add_view_traces(figure, total_view, classifications, visible=True)
    hazardous_count = _add_view_traces(
        figure, hazardous_view, classifications, visible=False
    )
    carcinogenic_count = _add_view_traces(
        figure, carcinogenic_view, classifications, visible=False
    )

    visible_total = [True] * total_count + [False] * (
        hazardous_count + carcinogenic_count
    )
    visible_hazardous = (
        [False] * total_count + [True] * hazardous_count + [False] * carcinogenic_count
    )
    visible_carcinogenic = [False] * (total_count + hazardous_count) + [
        True
    ] * carcinogenic_count

    buttons = [
        _view_button("Releases excl. CO\u2082", total_view, visible_total),
        _view_button("Hazardous mass", hazardous_view, visible_hazardous),
        _view_button("Carcinogenic mass", carcinogenic_view, visible_carcinogenic),
    ]

    figure.update_layout(
        barmode="stack",
        bargap=0.28,
        height=940,
        margin=dict(l=10, r=200, t=150, b=95),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT_FAMILY, size=12, color=TEXT_COLOR),
        title=dict(
            text=total_view.title,
            x=0,
            xanchor="left",
            font=dict(size=18, color=TEXT_COLOR),
        ),
        xaxis=dict(
            title=dict(text=total_view.axis_title, font=dict(size=12.5)),
            range=[0, total_view.x_max],
            gridcolor=GRID_COLOR,
            zeroline=False,
            showline=False,
            ticks="",
            tickfont=dict(size=11),
            automargin=True,
        ),
        yaxis=dict(
            categoryorder="array",
            categoryarray=total_view.y_order,
            autorange=True,
            showgrid=False,
            zeroline=False,
            ticks="",
            tickfont=dict(size=10.5),
            automargin=True,
        ),
        legend=dict(
            title=dict(text="<b>Pollutant</b>", side="top", font=dict(size=11.5)),
            orientation="v",
            xanchor="left",
            x=1.02,
            yanchor="top",
            y=1.0,
            traceorder="normal",
            font=dict(size=11),
            bgcolor="rgba(255,255,255,0)",
            itemsizing="constant",
        ),
        updatemenus=[
            dict(
                type="buttons",
                direction="right",
                x=1,
                y=1.005,
                xanchor="right",
                yanchor="bottom",
                pad=dict(t=0, r=0, b=6, l=0),
                showactive=True,
                active=0,
                bgcolor="#eef0f3",
                bordercolor="#c9d1d9",
                borderwidth=1,
                font=dict(size=12, color=TEXT_COLOR),
                buttons=buttons,
            )
        ],
        hovermode="closest",
        hoverlabel=dict(
            bgcolor="#ffffff",
            bordercolor=TEXT_COLOR,
            font=dict(family=FONT_FAMILY, size=12, color=TEXT_COLOR),
            align="left",
        ),
        modebar=dict(bgcolor="rgba(255,255,255,0.7)"),
    )
    return figure


def _add_view_traces(
    figure: go.Figure,
    view: FacilityView,
    classifications: dict[str, dict[str, Any]],
    visible: bool,
) -> int:
    """Append the stacked bars and the total-value labels for one view."""
    shares = view.matrix.div(view.totals, axis=0).fillna(0.0)
    count = 0

    for index, pollutant in enumerate(view.matrix.columns):
        masses = view.matrix[pollutant]
        if not masses.any():
            continue

        full_name = view.full_labels[pollutant]
        if full_name == OTHER_FULL_LABEL:
            note = "Pooled substances: each stays below the stacking threshold."
        else:
            note = classifications.get(full_name, {}).get(
                "note", "No hazard classification in the dataset."
            )

        customdata = [
            [
                view.full_names[row],
                view.places[row],
                full_name,
                f"{shares.loc[fid, pollutant]:.1%}",
                _format_mt(masses.loc[fid]),
                _format_mt(view.totals.loc[fid]),
                note,
            ]
            for row, fid in enumerate(view.matrix.index)
        ]

        figure.add_trace(
            go.Bar(
                x=masses.to_numpy() / view.unit_scale,
                y=view.labels,
                name=pollutant,
                orientation="h",
                marker=dict(
                    color=_pollutant_color(pollutant, index),
                    line=dict(width=0),
                ),
                customdata=customdata,
                hovertemplate=HOVER_TEMPLATE,
                visible=visible,
                legendgroup=view.key,
            )
        )
        count += 1

    figure.add_trace(
        go.Scatter(
            # A hair to the right of the bar, so the value never sits on it.
            x=(view.totals / view.unit_scale + view.x_max * 0.008).to_numpy(),
            y=view.labels,
            mode="text",
            text=[_format_in_unit(value, view) for value in view.totals],
            textposition="middle right",
            textfont=dict(size=10.5, color=TEXT_COLOR),
            cliponaxis=False,
            hoverinfo="skip",
            showlegend=False,
            visible=visible,
            legendgroup=view.key,
        )
    )
    return count + 1


def _view_button(label: str, view: FacilityView, visible: list[bool]) -> dict:
    """Button that swaps visibility and rescales the axes for one view."""
    return dict(
        label=label,
        method="update",
        args=[
            {"visible": visible},
            {
                "title.text": view.title,
                "xaxis.title.text": view.axis_title,
                "xaxis.range": [0, view.x_max],
                "yaxis.categoryarray": view.y_order,
                "yaxis.autorange": True,
            },
        ],
    )


if __name__ == "__main__":
    chart = build_figure()
    chart.show(config={"displaylogo": False, "responsive": True})

