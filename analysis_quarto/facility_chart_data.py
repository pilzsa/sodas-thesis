"""JSON payload for the release-ranking charts (React + D3 front end).

The page draws the ranking with a small React component
(``assets/facility-releases.js``): one stacked bar chart with a segmented
control that switches the ranking. This module keeps every data decision in
Python: it reuses the ranking and pooling logic of
:mod:`facility_emission_ranking` and serialises what the component needs into a
single JSON object.

Three views, all on the same mass basis (kilograms 2022-2024 summed per row):

* ``releases``     - every pollutant except CO2, stacked by pollutant;
* ``hazardous``    - only pollutants flagged as hazardous, greenhouse gases out;
* ``carcinogenic`` - only substances classified as carcinogenic (IARC Group 1 or
  2A, or a CLP ``Carc.`` class), greenhouse gases out.

The views are wrapped in a *grouping* - one way of choosing the rows. This
builder emits the facility grouping (rows are facilities, ranked top 25);
:mod:`group_chart_data` reuses the same helpers to emit an E-PRTR sector
grouping and an IED activity grouping, which the component puts behind a second
segmented control. A grouping carries the noun its hover box and footer need
("this facility" / "this sector") and, optionally, a footnote about its row set.

CO2 is not a view of its own. At 3.4 Gt it carries roughly sixty times the mass
of every other pollutant combined (98% of all reported mass), so a CO2 bar
would flatten the ranking into one sliver beside a wall of it. The component
reports the CO2 left out in each hover box instead, using the totals and the
per-row CO2 carried here.

Colour follows the *stack slot*, not the substance: the largest slice of the
ranking on screen takes the first hue and the rest take the next one
(:func:`view_colors`). The three views draw from different substance pools, so a
single name-to-colour table repeats tints inside one legend; slot order is what
the eye reads in a stacked bar anyway.

Every substance also carries the classification that was assigned to it
(``describe_substances``), so the hover box can say *why* a pollutant counts as
hazardous or carcinogenic: IARC group, CLP carcinogenicity, mutagenicity and
reprotoxicity classes, HAP, SVHC or WFD priority status.

Masses travel in kilograms because the views span different orders of
magnitude: the release view tops out around 25 Mt, the carcinogenic view around
1 kt. The browser formats and scales them per view.

Run the file directly to dump the payload to ``/tmp/facility_payload.json``::

    python facility_chart_data.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from facility_emission_ranking import (
    CO2_POLLUTANT,
    MIN_STACK_SHARE,
    OTHER_COLOR,
    OTHER_FULL_LABEL,
    OTHER_LABEL,
    SERIES_COLORS,
    TOP_N,
    TOP_POLLUTANTS,
    FacilityView,
    _build_view,
    _unit_for,
    describe_substances,
    load_releases,
    prepare_releases,
)


@dataclass(frozen=True)
class ViewSpec:
    """One switchable ranking."""

    key: str
    button: str
    heading: str
    subheading: str
    axis_title: str


VIEWS = (
    ViewSpec(
        key="releases",
        button="Releases excl. CO\u2082",
        heading="Releases excluding CO\u2082",
        subheading=(
            "Every reported pollutant except CO\u2082 \u00b7 air and water "
            "releases combined \u00b7 2022\u20132024"
        ),
        axis_title="Cumulative releases",
    ),
    ViewSpec(
        key="hazardous",
        button="Hazardous",
        heading="Hazardous releases",
        subheading=(
            "Substances classified as hazardous \u00b7 greenhouse gases "
            "excluded \u00b7 2022\u20132024"
        ),
        axis_title="Cumulative hazardous mass",
    ),
    ViewSpec(
        key="carcinogenic",
        button="Carcinogenic",
        heading="Carcinogenic releases",
        subheading=(
            "Substances classified as carcinogenic (IARC Group 1 or 2A, or a "
            "CLP Carc. class) \u00b7 greenhouse gases excluded \u00b7 "
            "2022\u20132024"
        ),
        axis_title="Cumulative carcinogenic mass",
    ),
)


def _decimals_for(max_in_unit: float) -> int:
    """Bar-end decimals that do not turn a short axis into a wall of zeros."""
    if max_in_unit >= 100:
        return 0
    if max_in_unit >= 10:
        return 1
    if max_in_unit >= 1:
        return 2
    return 3


def view_colors(view: FacilityView) -> dict[str, str]:
    """One colour per slice of one view, in stack order.

    The first hue goes to the first slice of the stack and each following slice
    takes the next one, so colour means *rank in this ranking*. Assigning by
    substance instead would need one tint per substance across all three views,
    and those pools do not overlap enough for a short set: the carcinogenic view
    draws from ranks 20-45 of the release view, so a ten-colour table repeated
    tints inside a single legend. The pooled bucket always takes the neutral.
    """
    colors = {OTHER_FULL_LABEL: OTHER_COLOR}
    series = [
        view.full_labels[label]
        for label in view.matrix.columns
        if view.full_labels[label] != OTHER_FULL_LABEL
    ]
    if len(series) > len(SERIES_COLORS):
        raise ValueError(
            f"{len(series)} substances in view {view.key!r}, but the series set "
            f"has {len(SERIES_COLORS)} colours"
        )
    for slot, name in enumerate(series):
        colors[name] = SERIES_COLORS[slot]
    return colors


def _row_payload(
    view: FacilityView,
    co2_by_key: pd.Series,
) -> list[dict[str, Any]]:
    """Serialise the stack matrix of one view into chart rows.

    Masses stay in kilograms; ``total`` is the row's mass in this view and
    ``co2`` the CO2 reported by the facilities behind that row, which the hover
    box reports as left out. ``row_id`` is a facility id for the facility
    ranking and a sector or activity key for the grouped rankings.
    """
    rows: list[dict[str, Any]] = []
    colors = view_colors(view)

    for position, row_id in enumerate(view.matrix.index):
        series = view.matrix.loc[row_id]
        total = float(view.totals.loc[row_id])
        segments: list[dict[str, Any]] = []

        for label, value in series.items():
            value = float(value)
            if value <= 0:
                continue
            full_name = view.full_labels[label]
            segments.append(
                {
                    "label": label,
                    "full": full_name,
                    "color": colors.get(full_name, OTHER_COLOR),
                    "value": value,
                    "share": round(value / total, 4) if total else 0.0,
                }
            )

        rows.append(
            {
                "id": str(row_id),
                "label": view.labels[position],
                "name": view.full_names[position],
                "place": view.places[position],
                "total": total,
                "co2": float(co2_by_key.get(row_id, 0.0)),
                "segments": segments,
            }
        )

    return rows


def _legend_payload(
    view: FacilityView,
    substances: dict[str, dict[str, Any]],
    pool: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Legend chips for the substances that survive in one view."""
    chips: list[dict[str, Any]] = []
    colors = view_colors(view)

    for pollutant in view.matrix.columns:
        full_name = view.full_labels[pollutant]
        if full_name == OTHER_FULL_LABEL and pool is not None:
            note = pool["note"]
        else:
            note = substances.get(full_name, {}).get("note", "")
        chips.append(
            {
                "label": pollutant,
                "full": full_name,
                "color": colors.get(full_name, OTHER_COLOR),
                "mass": float(view.matrix[pollutant].sum()),
                "classification": note,
            }
        )

    return chips


def _pool_payload(
    subset: pd.DataFrame,
    view: FacilityView,
    substances: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Describe the ``Other pollutants`` bucket of one view, if it has one.

    The bucket hides individual substances, so the hover box says how many it
    pools, how many of them are hazardous, and names the largest few.
    """
    full_names = set(view.full_labels.values())
    if OTHER_FULL_LABEL not in full_names:
        return None

    kept = full_names - {OTHER_FULL_LABEL}
    totals = (
        subset.groupby("Pollutant", observed=True)["Releases"]
        .sum()
        .sort_values(ascending=False)
    )
    members = totals[~totals.index.isin(kept)]
    classified = sum(
        1 for name in members.index if substances.get(name, {}).get("hazardous")
    )

    note = f"Pools the {len(members)} smaller substances of this view into one segment"
    note += (
        f"; {classified} of them carry a hazard classification."
        if classified
        else "."
    )

    return {
        "label": OTHER_LABEL,
        "full": OTHER_FULL_LABEL,
        "color": OTHER_COLOR,
        "tags": [
            {
                "kind": "pool",
                "text": f"{len(members)} substances",
                "title": "Each substance here stays below the stacking threshold",
            }
        ],
        "note": note,
        "examples": [str(name) for name in members.index[:4]],
    }



def _view_payload(
    spec: ViewSpec,
    view: FacilityView,
    subset: pd.DataFrame,
    co2_by_key: pd.Series,
    substances: dict[str, dict[str, Any]],
    start_year: int,
    end_year: int,
    *,
    heading: str | None = None,
    subheading: str | None = None,
) -> dict[str, Any]:
    """One switchable ranking, with the axis unit its longest bar needs.

    ``subset`` is the frame the view was built from (the rows of the view), so
    the pooled bucket can be described; ``heading`` and ``subheading`` are
    overridden by the grouped rankings, which name their own rows.
    """
    max_total = float(view.totals.max()) if len(view.totals) else 0.0
    factor, symbol, unit_name = _unit_for(max_total)
    pool = _pool_payload(subset, view, substances)

    return {
        "id": spec.key,
        "button": spec.button,
        "heading": heading or spec.heading,
        "subheading": subheading or spec.subheading,
        "axisTitle": f"{spec.axis_title}, {start_year}\u2013{end_year} ({unit_name})",
        "unit": symbol,
        "scale": factor,
        "decimals": _decimals_for(max_total / factor),
        "shown": float(view.totals.sum()),
        "rows": _row_payload(view, co2_by_key),
        "legend": _legend_payload(view, substances, pool),
        "pool": pool,
    }


def _group_block(
    group_id: str,
    button: str,
    noun: str,
    plural: str,
    views: list[dict[str, Any]],
    *,
    switch_label: str = "Ranked by",
    footnote: str = "",
) -> dict[str, Any]:
    """One way of grouping the rows, as the component reads it.

    ``noun`` and ``plural`` let the hover box and the footer say "this sector
    reported no CO2" instead of "this facility"; ``footnote`` explains the row
    set of the grouping under the chart.
    """
    return {
        "id": group_id,
        "button": button,
        "noun": noun,
        "plural": plural,
        "switchLabel": switch_label,
        "footnote": footnote,
        "views": views,
    }


def build_payload(
    data: pd.DataFrame | str | Path | None = None,
    *,
    start_year: int = 2022,
    end_year: int = 2024,
    top_n: int = TOP_N,
    min_share: float = MIN_STACK_SHARE,
    top_pollutants: int = TOP_POLLUTANTS,
) -> dict[str, Any]:
    """Build the JSON-serialisable payload for the chart component.

    The payload groups its views: this builder emits one grouping ("Facilities")
    so the component renders a single ranking, while
    :mod:`group_chart_data` emits a sector grouping and an activity grouping
    that share one figure.
    """
    if data is None or isinstance(data, (str, Path)):
        frame = load_releases(data, start_year=start_year, end_year=end_year)
    else:
        frame = prepare_releases(data, start_year=start_year, end_year=end_year)

    if frame.empty:
        raise ValueError("No releases left after filtering; check the input data.")

    identifier = "FacilityInspireId"
    is_co2 = frame["Pollutant"].eq(CO2_POLLUTANT)
    co2_by_facility = frame[is_co2].groupby(identifier, observed=True)["Releases"].sum()
    other_by_facility = frame[~is_co2].groupby(identifier, observed=True)[
        "Releases"
    ].sum()

    masks = {
        "releases": ~is_co2,
        "hazardous": frame["is_hazardous"],
        "carcinogenic": frame["is_carcinogenic"],
    }

    substances = describe_substances(frame)

    views: list[dict[str, Any]] = []
    for spec in VIEWS:
        view = _build_view(
            frame,
            masks[spec.key],
            key=spec.key,
            title=spec.heading,
            axis_title=spec.axis_title,
            top_n=top_n,
            top_pollutants=top_pollutants,
            min_share=min_share,
        )
        views.append(
            _view_payload(
                spec,
                view,
                frame[masks[spec.key]],
                co2_by_facility,
                substances,
                start_year,
                end_year,
            )
        )

    co2_total = float(co2_by_facility.sum())
    other_total = float(other_by_facility.sum())
    ratio = co2_total / other_total if other_total else 0.0

    return {
        "meta": {
            "from": start_year,
            "to": end_year,
            "topN": top_n,
            "co2": {
                "total": co2_total,
                "shareOfAllMass": round(co2_total / (co2_total + other_total), 4),
                "ratio": round(ratio, 1),
            },
            "carcinogenic": {"iarc": ["1", "2A"], "clp": "Carc."},
        },
        "groups": [
            _group_block(
                "facility",
                "Facilities",
                "facility",
                "facilities",
                views,
                footnote=(
                    "Each view ranks its own top "
                    f"{top_n} facilities; the hover box reports the share of the "
                    "facility total that each substance carries."
                ),
            )
        ],
        "substances": substances,
    }



if __name__ == "__main__":
    payload = build_payload()
    target = Path("/tmp/facility_payload.json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"wrote {target} ({target.stat().st_size / 1024:.0f} kB)")
    meta = payload["meta"]
    co2 = meta["co2"]
    print(
        f"2022-2024 · CO2 {co2['total'] / 1e9:,.1f} Mt "
        f"({co2['shareOfAllMass']:.1%} of all mass, {co2['ratio']:,.0f}x the rest)"
    )
    for group in payload["groups"]:
        print(f"\n== grouping: {group['button']} ({group['noun']})")
        if group["footnote"]:
            print("   ", group["footnote"])
        for view in group["views"]:
            print(
                f"\n[{view['id']}] {view['heading']} - {len(view['rows'])} rows, "
                f"{view['shown'] / view['scale']:,.1f} {view['unit']}"
            )
            print("   ", view["subheading"])
            for row in view["rows"][:3]:
                mix = ", ".join(
                    f"{segment['label']}={segment['value'] / view['scale']:.3f}"
                    for segment in row["segments"]
                )
                print(
                    f"  {row['total'] / view['scale']:>9.3f}  "
                    f"{row['label'][:44]:<44} {mix}"
                )
            print("    legend:", [chip["label"] for chip in view["legend"]])

