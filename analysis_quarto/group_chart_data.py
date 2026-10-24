"""JSON payload for the release rankings by E-PRTR sector and IED activity.

:mod:`facility_chart_data` ranks individual facilities. The same three views -
releases excluding CO2, hazardous mass, carcinogenic mass - also read well one
step up, where the question is which *kind* of installation carries the mass.
This module builds that second figure: the same component
(``assets/facility-releases.js``), the same palette (the series set of
:mod:`palette`, assigned per view by :func:`facility_chart_data.view_colors`) and
the same three views, with two groupings behind a segmented control:

* ``sector``   - the E-PRTR sector the facility registered under (the nine
  sectors of Annex I to the E-PRTR Regulation), plus one row for the facilities
  whose record carries no sector;
* ``activity`` - the IED Annex I activity (``IEDAnnexIMainActivity_ied``), i.e.
  the permitting category of the installation. Each view ranks its own top
  :data:`ACTIVITY_TOP_N` codes - 82 codes appear in the release view and 19 of
  them carry 90% of the mass - and pools the rest into one row, so every bar
  still adds up to the attributable total.

Both attributes are multi-valued in the extract (a plant can hold several
sector and activity codes), so the grouping uses the *first* listed value of the
latest reporting year. That is the same choice the map's facility card makes, so
the two figures describe a facility the same way; the code is what the country
published, not a classification made here.

Not every facility can be attributed to an Annex I activity: in 2022-2024 about
16% of the release mass (13% of the hazardous mass, 1.5% of the carcinogenic
mass) belongs to facilities without an activity code. Those facilities have no
row of their own and are left out of the activity ranking, whose subheading
states the coverage of each view. The sector ranking covers every facility,
because "sector not reported" is kept as a row.

Masses stay in kilograms, as in the facility payload; each view carries the axis
unit and the number formatting the browser needs.

Run the file directly to dump the payload to ``/tmp/group_payload.json``::

    python group_chart_data.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from facility_chart_data import (
    VIEWS,
    _group_block,
    _view_payload,
)
from facility_emission_ranking import (
    CO2_POLLUTANT,
    END_YEAR,
    MIN_STACK_SHARE,
    START_YEAR,
    TOP_POLLUTANTS,
    _build_view,
    describe_substances,
    find_data_file,
    load_releases,
    prepare_releases,
)
from facility_map_data import _list_values, _shorten

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SECTOR_COLUMN = "sector"
ACTIVITY_COLUMN = "activity_code"
UNREPORTED_SECTOR = "Sector not reported"

ACTIVITY_TOP_N = 20
OTHER_ACTIVITY_KEY = "__other_activities__"
OTHER_ACTIVITY_LABEL = "All other IED activities"

#: Bar labels for the sector rows: the published names are too long for the
#: label column, and these are the words the ranking is read with.
SECTOR_LABELS = {
    "Energy sector": "Energy",
    "Production and processing of metals": "Metals",
    "Mineral industry": "Minerals",
    "Chemical industry": "Chemicals",
    "Waste and wastewater management": "Waste and wastewater",
    "Paper and wood production and processing": "Paper and wood",
    "Intensive livestock production and aquaculture": "Livestock and aquaculture",
    "Animal and vegetable products from the food and beverage sector": "Food and beverages",
    "Other activities": "Other activities",
    UNREPORTED_SECTOR: "Sector not reported",
}

#: Bar labels for the Annex I codes that reach a top 20 in some view; every
#: other code falls back to a trimmed activity name. The code stays in front,
#: because it is how the Annex is cited.
ACTIVITY_LABELS = {
    "1.1": "Combustion of fuels, 50 MW+",
    "1.2": "Refining of mineral oil and gas",
    "1.3": "Coke production",
    "1.4(b)": "Gasification or liquefaction of fuels",
    "2.1": "Metal ore roasting or sintering",
    "2.2": "Pig iron or steel production",
    "2.3(a)": "Hot-rolling mills",
    "2.4": "Ferrous metal foundries",
    "2.5(a)": "Non-ferrous crude metals",
    "2.5(b)": "Non-ferrous metal melting",
    "2.6": "Surface treatment of metals",
    "3.1(a)": "Cement clinker",
    "3.1(b)": "Lime",
    "3.3": "Glass and glass fibre",
    "4.1(a)": "Organic chemicals: simple hydrocarbons",
    "4.1(b)": "Organic chemicals: oxygenated",
    "4.1(d)": "Organic chemicals: nitrogenous",
    "4.1(f)": "Organic chemicals: halogenated",
    "4.1(h)": "Organic chemicals: polymers",
    "4.2(a)": "Inorganic chemicals: gases",
    "4.2(b)": "Inorganic chemicals: acids",
    "4.2(c)": "Inorganic chemicals: bases",
    "4.2(d)": "Inorganic chemicals: salts",
    "4.2(e)": "Inorganic chemicals: non-metals",
    "4.3": "Fertilisers",
    "4.5": "Pharmaceutical products",
    "5.2(a)": "Waste incineration (non-hazardous)",
    "5.2(b)": "Waste incineration (hazardous)",
    "5.3(a)": "Disposal of non-hazardous waste",
    "5.3(b)": "Recovery of non-hazardous waste",
    "5.4": "Landfills",
    "5.5": "Temporary storage of hazardous waste",
    "6.1(a)": "Pulp production",
    "6.1(b)": "Paper or cardboard production",
    "6.7": "Surface treatment with solvents",
    "6.11": "Independent waste water treatment",
}

#: Characters of an Annex I name kept in the hover box and in the name of the
#: largest facility behind a row; both are single lines of body text.
ACTIVITY_NAME_WIDTH = 110
FACILITY_NAME_WIDTH = 48
ACTIVITY_LABEL_WIDTH = 34


# --------------------------------------------------------------------------
# Facility attributes
# --------------------------------------------------------------------------


def _first_value(value: Any) -> str:
    """First item of a stringified multi-valued cell, as the map reads it."""
    items = _list_values(value)
    return items[0] if items else ""


def load_attributes(path: str | Path | None = None) -> pd.DataFrame:
    """Sector, IED activity and country of every facility, latest year wins.

    One row per ``FacilityInspireId``, indexed by it, so it can be joined onto
    the prepared release frame. A blank sector becomes
    :data:`UNREPORTED_SECTOR` rather than a missing row that pandas would drop
    from the ranking; a blank activity stays blank, because an installation
    without an Annex I code cannot be attributed to one.
    """
    source = find_data_file(path)
    columns = [
        "reportingYear",
        "FacilityInspireId",
        "countryName",
        "EPRTR_SectorName",
        "IEDAnnexIMainActivity_ied",
        "IEDMainActivityName_ied",
    ]
    attributes = pd.read_csv(source, usecols=columns, low_memory=False)
    latest = (
        attributes.sort_values("reportingYear")
        .drop_duplicates("FacilityInspireId", keep="last")
        .set_index("FacilityInspireId")
    )

    sector = latest["EPRTR_SectorName"].astype("string").fillna("").str.strip()
    sector = sector.mask(
        sector.str.lower().isin(["", "nan", "none"]), UNREPORTED_SECTOR
    )
    country = latest["countryName"].astype("string").fillna("").str.strip()

    return pd.DataFrame(
        {
            SECTOR_COLUMN: sector,
            ACTIVITY_COLUMN: latest["IEDAnnexIMainActivity_ied"].map(_first_value),
            "activity_name": latest["IEDMainActivityName_ied"].map(_first_value),
            "country": country,
        }
    )


# --------------------------------------------------------------------------
# Row copy
# --------------------------------------------------------------------------


def _everything(frame: pd.DataFrame) -> pd.Series:
    """An all-True mask, for a frame that is already the subset to rank."""
    return pd.Series(True, index=frame.index, dtype=bool)


def _place_text(rows: pd.DataFrame) -> str:
    """Facility count, country count and the largest facility behind a row."""
    facilities = int(rows["FacilityInspireId"].nunique())
    countries = int(rows["country"].replace("", pd.NA).nunique())
    largest = (
        rows.groupby("FacilityInspireId", observed=True)
        .agg(mass=("Releases", "sum"), name=("facility_full", "first"))
        .sort_values("mass", ascending=False)
    )
    parts = [
        f"{facilities:,} {'facility' if facilities == 1 else 'facilities'}",
        f"{countries} {'country' if countries == 1 else 'countries'}",
    ]
    if len(largest):
        name = _shorten(str(largest["name"].iloc[0]), FACILITY_NAME_WIDTH)
        parts.append(f"largest: {name}")
    return " \u00b7 ".join(parts)


def _sector_label(sector: str) -> str:
    return SECTOR_LABELS.get(sector, sector)


def _activity_label(code: str, name: str) -> str:
    """Bar label of an Annex I code: the code, then a short name."""
    short = ACTIVITY_LABELS.get(code) or _trim_activity(name)
    return f"{code} {short}".strip()


def _activity_name(code: str, name: str) -> str:
    """Hover-box name of an Annex I code: the code and the published name."""
    text = _shorten(name, ACTIVITY_NAME_WIDTH) if name and name != code else ""
    return f"{code} \u00b7 {text}" if text else code


def _trim_activity(name: str) -> str:
    """A label from a published Annex I name, for codes with no curated label."""
    text = name.strip()
    for marker in (" with ", ", as defined", " including", ", such as", " not covered"):
        cut = text.find(marker)
        if cut > 12:
            text = text[:cut]
    return _shorten(text, ACTIVITY_LABEL_WIDTH)


# --------------------------------------------------------------------------
# Groupings
# --------------------------------------------------------------------------


def _sector_grouping(
    frame: pd.DataFrame,
    masks: dict[str, pd.Series],
    co2_by_sector: pd.Series,
    substances: dict[str, dict[str, Any]],
    start_year: int,
    end_year: int,
    *,
    min_share: float,
    top_pollutants: int,
) -> dict[str, Any]:
    """Rank every E-PRTR sector, plus the facilities that report none."""
    views: list[dict[str, Any]] = []
    heading = "by E-PRTR sector"

    for spec in VIEWS:
        subset = frame[masks[spec.key]]
        totals = (
            subset.groupby(SECTOR_COLUMN, observed=True)["Releases"]
            .sum()
            .sort_values(ascending=False)
        )
        info = {
            str(sector): {
                "label": _sector_label(sector),
                # The published sector name is the full name; only the
                # unreported row carries our wording.
                "name": str(sector),
                "place": _place_text(subset[subset[SECTOR_COLUMN].eq(sector)]),
            }
            for sector in totals.index
        }

        view = _build_view(
            frame,
            masks[spec.key],
            key=spec.key,
            title=f"{spec.heading}, {heading}",
            axis_title=spec.axis_title,
            top_n=len(totals),
            top_pollutants=top_pollutants,
            min_share=min_share,
            group_column=SECTOR_COLUMN,
            row_info=info,
        )
        views.append(
            _view_payload(
                spec,
                view,
                subset,
                co2_by_sector,
                substances,
                start_year,
                end_year,
                heading=f"{spec.heading}, {heading}",
                subheading=(
                    f"All {len(totals)} sectors that report mass in this view "
                    f"\u00b7 air and water combined \u00b7 {start_year}\u2013{end_year}"
                ),
            )
        )

    reporting = frame.drop_duplicates("FacilityInspireId")
    unreported = int(reporting[SECTOR_COLUMN].eq(UNREPORTED_SECTOR).sum())
    footnote = (
        f"\u201c{UNREPORTED_SECTOR}\u201d holds the {unreported:,} facilities "
        f"({unreported / len(reporting):.1%}) whose latest record carries no "
        "sector code, so every facility is accounted for."
    )

    return _group_block(
        "sector",
        "E-PRTR sector",
        "sector",
        "sectors",
        views,
        switch_label="Grouped by",
        footnote=footnote,
    )


def _activity_grouping(
    frame: pd.DataFrame,
    masks: dict[str, pd.Series],
    substances: dict[str, dict[str, Any]],
    start_year: int,
    end_year: int,
    *,
    top_n: int,
    min_share: float,
    top_pollutants: int,
) -> dict[str, Any]:
    """Rank the top Annex I codes of every view, with one pooled tail row."""
    views: list[dict[str, Any]] = []
    heading = "by IED Annex I activity"
    is_co2 = frame["Pollutant"].eq(CO2_POLLUTANT)

    for spec in VIEWS:
        mask = masks[spec.key]
        # Facilities without an activity code cannot be attributed to one; they
        # stay out of this ranking and the subheading states the coverage.
        subset = frame[mask & frame[ACTIVITY_COLUMN].ne("")]
        totals = (
            subset.groupby(ACTIVITY_COLUMN, observed=True)["Releases"]
            .sum()
            .sort_values(ascending=False)
        )
        kept = [str(code) for code in totals.head(top_n).index]
        rows = subset.assign(
            _group=subset[ACTIVITY_COLUMN].where(
                subset[ACTIVITY_COLUMN].isin(kept), OTHER_ACTIVITY_KEY
            )
        )
        tail = rows[rows["_group"].eq(OTHER_ACTIVITY_KEY)]

        info: dict[str, dict[str, str]] = {}
        for code in kept:
            group_rows = rows[rows["_group"].eq(code)]
            name = str(group_rows["activity_name"].iloc[0])
            info[code] = {
                "label": _activity_label(code, name),
                "name": _activity_name(code, name),
                "place": _place_text(group_rows),
            }
        if len(tail):
            codes = int(tail[ACTIVITY_COLUMN].nunique())
            info[OTHER_ACTIVITY_KEY] = {
                # The count in the label keeps a long pooled bar from reading as
                # the largest single activity; the hover box repeats it because
                # the label column truncates on narrow screens.
                "label": f"{OTHER_ACTIVITY_LABEL} ({codes} codes)",
                "name": OTHER_ACTIVITY_LABEL,
                "place": f"{codes} codes \u00b7 " + _place_text(tail),
            }

        # The pooled row is kept last: it is a remainder, not a member of the
        # top of the ranking, even where its mass exceeds the 20th code.
        order = [*kept, OTHER_ACTIVITY_KEY] if len(tail) else kept

        # CO2 is left out of two of the three views, so it is summed over the
        # facilities behind each row rather than over the rows in the view.
        assignment = rows.drop_duplicates("FacilityInspireId").set_index(
            "FacilityInspireId"
        )["_group"]
        co2_by_group = (
            frame.loc[is_co2]
            .assign(_group=lambda part: part["FacilityInspireId"].map(assignment))
            .dropna(subset=["_group"])
            .groupby("_group", observed=True)["Releases"]
            .sum()
        )

        view = _build_view(
            rows,
            _everything(rows),
            key=spec.key,
            title=f"{spec.heading}, {heading}",
            axis_title=spec.axis_title,
            top_n=len(order),
            top_pollutants=top_pollutants,
            min_share=min_share,
            group_column="_group",
            row_info=info,
            row_order=order,
        )

        coverage = float(subset["Releases"].sum()) / float(
            frame.loc[mask, "Releases"].sum()
        )
        views.append(
            _view_payload(
                spec,
                view,
                rows,
                co2_by_group,
                substances,
                start_year,
                end_year,
                heading=f"{spec.heading}, {heading}",
                subheading=(
                    f"{coverage:.0%} of this view's mass carries an Annex I "
                    f"activity \u00b7 top {top_n} codes, the rest pooled \u00b7 "
                    f"{start_year}\u2013{end_year}"
                ),
            )
        )

    return _group_block(
        "activity",
        "IED activity",
        "activity",
        "activities",
        views,
        switch_label="Grouped by",
        footnote=(
            f"Each view ranks its own top {top_n} codes and pools the rest, so a "
            "code can enter one ranking and not another; the activity is the "
            "first code listed on the facility's latest record."
        ),
    )


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


def build_payload(
    data: pd.DataFrame | str | Path | None = None,
    *,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
    activity_top_n: int = ACTIVITY_TOP_N,
    min_share: float = MIN_STACK_SHARE,
    top_pollutants: int = TOP_POLLUTANTS,
    attributes: pd.DataFrame | None = None,
    attributes_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the JSON-serialisable payload for the grouped ranking chart.

    ``data`` is the classified release frame (or a path to the extract), and
    ``attributes`` the :func:`load_attributes` table; it is read from the same
    extract when omitted. The payload carries two groupings, E-PRTR sector and
    IED activity, and the three views of :mod:`facility_chart_data`.
    """
    if data is None or isinstance(data, (str, Path)):
        frame = load_releases(data, start_year=start_year, end_year=end_year)
    else:
        frame = prepare_releases(data, start_year=start_year, end_year=end_year)

    if frame.empty:
        raise ValueError("No releases left after filtering; check the input data.")

    keys = attributes if attributes is not None else load_attributes(attributes_path)
    frame = frame.join(keys, on="FacilityInspireId")
    missing = int(frame[SECTOR_COLUMN].isna().sum())
    if missing:
        raise ValueError(
            f"{missing:,} release rows belong to facilities with no sector or "
            "activity record; pass an attributes table for the whole extract."
        )

    identifier = "FacilityInspireId"
    is_co2 = frame["Pollutant"].eq(CO2_POLLUTANT)
    co2_by_facility = frame[is_co2].groupby(identifier, observed=True)["Releases"].sum()
    other_by_facility = frame[~is_co2].groupby(identifier, observed=True)[
        "Releases"
    ].sum()
    co2_by_sector = frame[is_co2].groupby(SECTOR_COLUMN, observed=True)["Releases"].sum()

    masks = {
        "releases": ~is_co2,
        "hazardous": frame["is_hazardous"],
        "carcinogenic": frame["is_carcinogenic"],
    }

    substances = describe_substances(frame)

    groups = [
        _sector_grouping(
            frame,
            masks,
            co2_by_sector,
            substances,
            start_year,
            end_year,
            min_share=min_share,
            top_pollutants=top_pollutants,
        ),
        _activity_grouping(
            frame,
            masks,
            substances,
            start_year,
            end_year,
            top_n=activity_top_n,
            min_share=min_share,
            top_pollutants=top_pollutants,
        ),
    ]

    co2_total = float(co2_by_facility.sum())
    other_total = float(other_by_facility.sum())

    return {
        "meta": {
            "from": start_year,
            "to": end_year,
            "co2": {
                "total": co2_total,
                "shareOfAllMass": round(co2_total / (co2_total + other_total), 4),
                "ratio": round(co2_total / other_total, 1) if other_total else 0.0,
            },
            "carcinogenic": {"iarc": ["1", "2A"], "clp": "Carc."},
        },
        "groups": groups,
        "substances": substances,
    }


if __name__ == "__main__":
    payload = build_payload()
    target = Path("/tmp/group_payload.json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"wrote {target} ({target.stat().st_size / 1024:.0f} kB)")

    for group in payload["groups"]:
        print(f"\n== grouping: {group['button']} ({group['noun']})")
        print("   ", group["footnote"])
        for view in group["views"]:
            print(
                f"\n[{view['id']}] {view['heading']} - {len(view['rows'])} rows, "
                f"{view['shown'] / view['scale']:,.2f} {view['unit']}"
            )
            print("   ", view["subheading"])
            for row in view["rows"]:
                mix = ", ".join(
                    f"{segment['label']}={segment['share']:.0%}"
                    for segment in row["segments"][:4]
                )
                print(
                    f"  {row['total'] / view['scale']:>9.3f}  "
                    f"{row['label'][:36]:<36} {row['place'][:52]:<52} {mix}"
                )
