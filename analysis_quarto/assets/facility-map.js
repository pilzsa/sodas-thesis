/* European facility map - SVG for the geometry, React for the controls.
 *
 * Every E-PRTR facility is a dot coloured by the strongest hazard class among
 * the substances it reported and sized by the mass the selected view keeps.
 * Three views mirror the ranking beside it: releases excluding CO2, hazardous
 * mass, carcinogenic mass. The map can be searched (country, municipality or
 * facility), zoomed and panned; the municipality outlines stay as a locator
 * layer and are not shaded, so the dots keep the whole colour budget.
 *
 * Data contract (built by facility_map_data.py):
 *   assets/map/facilities.json  payload: facilities, substances, size scales
 *   assets/map/countries.json   country outlines of the base map
 *   assets/map/lau.json         outlines of the municipalities in the payload
 *
 * Geometry arrives in EPSG:3035 metres as delta-encoded rings at `scale`
 * metres per unit - one flat [x0, y0, dx1, dy1, ...] array per ring - so the
 * page needs neither a projection library nor a topology decoder.
 */

(function () {
  "use strict";

  var ROOT_ID = "facility-map-root";

  var html = htm.bind(React.createElement);
  var useState = React.useState;
  var useEffect = React.useEffect;
  var useRef = React.useRef;
  var useMemo = React.useMemo;
  var useLayoutEffect = React.useLayoutEffect;

  /* Same tokens as the ranking figure, so the two read as one dashboard. */
  var INK = "#17212c";
  var MUTED = "#536273";
  /* Municipality outlines: a locator, not a layer with a value of its own. Dark
     enough to read over the land (2.1:1) and still a step quieter than the
     coastline, which is the line that carries the country shapes (4.7:1). */
  var LAU_STROKE = "#9cb0c4";

  /*
   * Dots are filled at 75%, not opaque: the land, the coastline and a
   * neighbouring dot stay visible under a mark that is 1.4 to 9 px wide, and a
   * cluster of grey dots no longer reads as one flat blob. What carries the
   * class is then the hairline edge in the level's own darker shade - see
   * ``outline`` in palette.py - which the fill cannot soften because it is not
   * translucent.
   */
  var DOT_FILL_OPACITY = 0.75;
  /* Edge of a dot, in screen pixels. The stroke ignores the zoom transform
     (vectorEffect), so this is also its width at 2048x. */
  var DOT_STROKE = 0.7;

  /*
   * Pointer slack, in pixels, added around a dot's own radius. Kept small so a
   * municipality stays reachable between neighbouring dots; the dots themselves
   * are still easy to hit because the slack is added to their drawn radius.
   */
  var HIT_RADIUS = 5;
  var CELL = 24;

  /* Dot sizes, in screen pixels, at zoom 1: the log scale spans this range. */
  var RADIUS_MIN = 1.4;
  var RADIUS_MAX = 9;

  /*
   * Dot radius fades with the zoom: the map group is scaled by k, so a dot
   * drawn in map space grows with the square of the gesture. Dividing the
   * radius by k^DOT_FADE leaves a dot that only grows as k^0.2 - a large plant
   * still reads as large at 1x, but it stops covering a city at 500x, and the
   * facilities around it separate instead of merging into one blob.
   */
  var DOT_FADE = 0.8;

  /*
   * The dot layer is rebuilt when the fade steps, not on every wheel event.
   * One step is a 10% change in k, which moves a dot's screen radius by
   * k^0.2 x 10% - about 2%, well under what an eye can see - yet a gesture
   * across the whole range then redraws thirteen thousand paths a few dozen
   * times instead of once per notch.
   */
  var FADE_STEP = 0.1;

  /* Zoom is a plain SVG transform on the map group; these bound the gesture.
     2048x turns the 5 500 km window into a 2.7 km view: one plant and the
     installations around it, where neighbouring dots come apart instead of
     merging into one blob. */
  var ZOOM_MAX = 2048;
  var ZOOM_STEP = 1.6;
  /* Wheel: about a third more per notch, so the 2048x ceiling is ~32 notches
     away - fine enough to stop where you want, short enough to get there. */
  var WHEEL_RATE = 0.0024;
  var DRAG_SLACK = 3;

  /* Medium codes of a substance row, as written by facility_map_data.py. */
  var MEDIUM_AIR = 1;
  var MEDIUM_WATER = 2;
  var MEDIUM_BOTH = 3;

  /* Where the three substance counts sit at the head of a details row. */
  var MEDIUM_COUNT_SLOT = { combined: 0, air: 1, water: 2 };

  var SEARCH_LIMIT = 8;
  var SEARCH_COUNTRY = 0;
  var SEARCH_MUNICIPALITY = 1;
  var SEARCH_FACILITY = 2;

  var TIP_WIDTH = 320;
  /* Fallback height for the first frame; the card measures itself after that. */
  var TIP_HEIGHT = 380;

  /* Round distances for the scale bar, in km. The short end covers the deepest
     zoom (2.7 km across), where a 1 km bar would span a third of the plot. */
  var SCALE_DISTANCES = [0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 200, 250, 500,
    1000, 2000];

  /* ---------------------------------------------------------------- numbers */

  /** Decimals that keep a value readable at its own magnitude. */
  function decimalsFor(value) {
    var abs = Math.abs(value);
    if (abs >= 100) return 0;
    if (abs >= 10) return 1;
    if (abs >= 1) return 2;
    return 3;
  }

  /** Mass in kg, rendered in the largest unit that leaves at least one unit. */
  function mass(kilograms) {
    var value = Number(kilograms) || 0;
    if (value === 0) return "0 kg";
    var units = [
      [1e9, "Mt"],
      [1e6, "kt"],
      [1e3, "t"],
    ];
    for (var i = 0; i < units.length; i += 1) {
      if (Math.abs(value) >= units[i][0]) {
        var scaled = value / units[i][0];
        return d3.format("." + decimalsFor(scaled) + "f")(scaled) + " " + units[i][1];
      }
    }
    return d3.format("." + decimalsFor(value) + "f")(value) + " kg";
  }

  function area(km2) {
    if (!(km2 > 0)) return "unknown area";
    return d3.format(km2 >= 100 ? ",.0f" : ",.1f")(km2) + " km\u00b2";
  }

  /** Mass for a legend tick: "10 t" rather than "10.0 t" (a scale of decades). */
  function tickMass(kilograms) {
    return mass(kilograms).replace(/\.0+(?= )/, "");
  }

  /** "1 facility" / "12,000 facilities". */
  function plural(count, singular, many) {
    var noun = count === 1 ? singular : many || singular + "s";
    return d3.format(",d")(count) + " " + noun;
  }

  /* ---------------------------------------------------------------- measures */

  /*
   * A measure is one view read through one medium ("air", "water", "combined").
   * The payload stores the air and water mass of every view in separate slots
   * and names the slots of each measure, so "air + water" is a sum over two
   * slots and nothing is stored twice.
   */

  /** Mass of a facility or LAU row, summed over the slots of the measure. */
  function measureMass(measure, slots, row) {
    var names = measure.slots || [];
    var total = 0;
    for (var i = 0; i < names.length; i += 1) {
      total += Number(row[slots[names[i]]]) || 0;
    }
    return total;
  }

  /** "air" / "water" / "air + water" for a substance row's medium code. */
  function mediumName(flag) {
    if (flag === MEDIUM_BOTH) return "air + water";
    if (flag === MEDIUM_WATER) return "water";
    return "air";
  }

  /** Short form for the chips: "air", "water", "both". */
  function mediumShort(flag) {
    if (flag === MEDIUM_BOTH) return "both";
    if (flag === MEDIUM_WATER) return "water";
    return "air";
  }

  /** Does a substance released to `flag` count in the selected medium? */
  function inMedium(flag, medium) {
    if (medium === "air") return flag !== MEDIUM_WATER;
    if (medium === "water") return flag !== MEDIUM_AIR;
    return true;
  }

  /** Compact name of a measure for the legend and the aria labels. */
  function measureShort(measure) {
    if (measure.id === "air") return "air only";
    if (measure.id === "water") return "water only";
    return "air + water";
  }

  /** The air and water slots of a view, for the split line of a card. */
  function mediumSlots(view, slots) {
    var combined = (view.measures && view.measures.combined) || null;
    var names = (combined && combined.slots) || [];
    return {
      air: names.length > 0 ? slots[names[0]] : -1,
      water: names.length > 1 ? slots[names[1]] : -1,
    };
  }


  /* --------------------------------------------------------------- geometry */

  /** One ring as an SVG path in the pixel space of `place`. */
  function ringPath(ring, scale, place) {
    var x = ring[0];
    var y = ring[1];
    var parts = ["M" + place.x(x * scale) + "," + place.y(y * scale)];
    for (var i = 2; i < ring.length; i += 2) {
      x += ring[i];
      y += ring[i + 1];
      parts.push("L" + place.x(x * scale) + "," + place.y(y * scale));
    }
    parts.push("Z");
    return parts.join("");
  }

  /** A shape (all of its rings) as one path. */
  function shapePath(shape, scale, place) {
    var parts = [];
    for (var i = 0; i < shape.length; i += 1) {
      parts.push(ringPath(shape[i], scale, place));
    }
    return parts.join("");
  }

  /**
   * ``#rrggbb`` at the dot fill's opacity, as an ``rgba()`` string. The legend
   * and the hover card show a swatch of what the map draws, so they take the
   * same alpha the dots do; the swatch's inset ring is the level's outline.
   */
  function tint(color) {
    var text = String(color || "").replace("#", "");
    if (text.length !== 6) return color;
    var value = parseInt(text, 16);
    if (isNaN(value)) return color;
    return (
      "rgba(" +
      ((value >> 16) & 255) + ", " +
      ((value >> 8) & 255) + ", " +
      (value & 255) + ", " +
      DOT_FILL_OPACITY +
      ")"
    );
  }

  /**
   * A circle as a path, so a thousand dots cost one node instead of a thousand.
   * Coordinates are map-space pixels (the group scales them by the zoom), so
   * they keep three decimals: at the deepest zoom one map pixel is 2048 screen
   * pixels, and a tenth of it would visibly shift a dot off its facility.
   */
  function dotPath(x, y, radius) {
    var r = Math.max(0.01, Math.round(radius * 1000) / 1000);
    var cx = Math.round(x * 1000) / 1000;
    var cy = Math.round(y * 1000) / 1000;
    var span = Math.round(2 * r * 1000) / 1000;
    var left = Math.round((cx - r) * 1000) / 1000;
    return (
      "M" + left + "," + cy +
      "a" + r + "," + r + " 0 1,0 " + span + ",0" +
      "a" + r + "," + r + " 0 1,0 -" + span + ",0"
    );
  }

  /**
   * Bounding box of a shape, in metres: [minX, minY, maxX, maxY]. Used to frame
   * a country or a municipality selected in the search box.
   */
  function shapeBox(shape, scale) {
    var minX = Infinity;
    var minY = Infinity;
    var maxX = -Infinity;
    var maxY = -Infinity;
    for (var r = 0; r < shape.length; r += 1) {
      var ring = shape[r];
      var x = ring[0];
      var y = ring[1];
      for (var i = 0; i < ring.length; i += 2) {
        if (i > 0) {
          x += ring[i];
          y += ring[i + 1];
        }
        if (x < minX) minX = x;
        if (y < minY) minY = y;
        if (x > maxX) maxX = x;
        if (y > maxY) maxY = y;
      }
    }
    return [minX * scale, minY * scale, maxX * scale, maxY * scale];
  }

  /** Uniform fit of the map window into a box, y pointing up. */
  function fitWindow(window, width, height) {
    var dx = window[2] - window[0];
    var dy = window[3] - window[1];
    var k = Math.min(width / dx, height / dy);
    var ox = (width - dx * k) / 2;
    var oy = (height - dy * k) / 2;
    function round(value) {
      return Math.round(value * 10) / 10;
    }
    return {
      k: k,
      place: {
        x: function (metres) {
          return round(ox + (metres - window[0]) * k);
        },
        y: function (metres) {
          return round(oy + (window[3] - metres) * k);
        },
      },
    };
  }

  /**
   * Spatial hash of the dots, so a pointer move tests a handful of candidates
   * instead of thirteen thousand.
   */
  function indexDots(dots) {
    var grid = new Map();
    for (var i = 0; i < dots.length; i += 1) {
      var dot = dots[i];
      var key = Math.floor(dot.x / CELL) + ":" + Math.floor(dot.y / CELL);
      var bucket = grid.get(key);
      if (!bucket) {
        bucket = [];
        grid.set(key, bucket);
      }
      bucket.push(dot);
    }
    return grid;
  }

  /** The dot that best answers to a pointer, or null beyond `slack`. */
  function nearestDot(grid, x, y, slack) {
    var best = null;
    var bestScore = Infinity;
    var cellX = Math.floor(x / CELL);
    var cellY = Math.floor(y / CELL);
    for (var dx = -1; dx <= 1; dx += 1) {
      for (var dy = -1; dy <= 1; dy += 1) {
        var bucket = grid.get(cellX + dx + ":" + (cellY + dy));
        if (!bucket) continue;
        for (var i = 0; i < bucket.length; i += 1) {
          var dot = bucket[i];
          var distance = Math.sqrt(
            (dot.x - x) * (dot.x - x) + (dot.y - y) * (dot.y - y),
          );
          // A wide circle is easier to hit than a narrow one.
          var score = Math.max(0, distance - dot.r);
          if (score < bestScore) {
            bestScore = score;
            best = dot;
          }
        }
      }
    }
    return bestScore <= slack ? best : null;
  }

  /* ------------------------------------------------------------------- data */

  function fetchJson(url) {
    return fetch(url).then(function (response) {
      if (!response.ok) {
        throw new Error(response.status + " " + response.statusText + " " + url);
      }
      return response.json();
    });
  }

  /** Load the three map assets that sit next to the page. */
  function useMapData() {
    var state = useState({
      status: "loading",
      error: "",
      payload: null,
      countries: null,
      lau: null,
    });
    var setState = state[1];

    useEffect(function () {
      var alive = true;
      var base = document.baseURI.slice(0, document.baseURI.lastIndexOf("/") + 1);
      Promise.all([
        fetchJson(base + "assets/map/facilities.json"),
        fetchJson(base + "assets/map/countries.json"),
        fetchJson(base + "assets/map/lau.json"),
      ])
        .then(function (files) {
          if (!alive) return;
          // A stale facilities.json from the browser cache would leave the
          // component without the per-medium measures: say so instead of
          // failing on the first read.
          var views = (files[0] && files[0].views) || [];
          if (!views.length || !views[0].measures) {
            throw new Error(
              "assets/map/facilities.json predates this page; reload with Ctrl+Shift+R",
            );
          }
          setState({
            status: "ready",
            error: "",
            payload: files[0],
            countries: files[1],
            lau: files[2],
          });
        })
        .catch(function (error) {
          if (!alive) return;
          setState({
            status: "error",
            error: String(error && error.message ? error.message : error),
            payload: null,
            countries: null,
            lau: null,
          });
        });
      return function () {
        alive = false;
      };
    }, []);

    return state[0];
  }

  /** Positional slots, named by the payload so the component stays readable. */
  function readSlots(names) {
    var out = {};
    (names || []).forEach(function (name, position) {
      out[name] = position;
    });
    return out;
  }

  /** Track the rendered width of an element so the SVG is redrawn on resize. */
  function useElementWidth(ref) {
    var state = useState(0);
    var setWidth = state[1];

    useEffect(function () {
      var node = ref.current;
      if (!node) return undefined;
      function update() {
        // Whole pixels, and the same value bails out of the render: a
        // sub-pixel jitter in a ResizeObserver callback would otherwise keep
        // re-measuring (and re-drawing thirteen thousand dots) while the page
        // settles.
        var next = Math.round(node.getBoundingClientRect().width);
        setWidth(function (current) {
          return current === next ? current : next;
        });
      }
      update();
      if (typeof ResizeObserver === "undefined") return undefined;
      var observer = new ResizeObserver(update);
      observer.observe(node);
      return function () {
        observer.disconnect();
      };
    }, [ref]);

    return state[0];
  }

  /* ---------------------------------------------------------------- tooltip */

  /** Country name for a two-letter code, or the code itself. */
  function countryName(payload, code) {
    var names = (payload.countries && payload.countries.names) || {};
    return names[code] || code || "unknown country";
  }

  /**
   * Where a permit link leads, as a host without "www." or protocol. The card
   * quotes the register rather than the whole URL, because a query string is
   * often longer than the card; the full link stays in the title attribute and
   * in the href the browser follows.
   */
  function permitHost(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch (error) {
      return url;
    }
  }

  /**
   * Substances behind a facility's total in one view, largest first. The
   * payload keeps a flat [countAll, countAir, countWater, pollutantIndex, kg,
   * medium, ...] row per facility and view, so the card can name the substances
   * instead of only the largest one, say whether each was released to air or
   * water, and count what it leaves out in the medium that is on.
   */
  function substanceRows(payload, view, measure, index) {
    var table = payload.details && payload.details[view.id];
    var row = table ? table[index] : null;
    if (!row || row.length < 4) return null;
    var rows = [];
    for (var i = 3; i + 2 < row.length; i += 3) {
      var medium = row[i + 2];
      if (!inMedium(medium, measure.id)) continue;
      var name = payload.pollutants[row[i]];
      var info = payload.substances[name] || null;
      rows.push({
        name: name,
        kilograms: row[i + 1],
        medium: medium,
        tags: (info && info.tags) || [],
        note: (info && info.note) || "",
      });
    }
    return { count: row[MEDIUM_COUNT_SLOT[measure.id] || 0], rows: rows };
  }

  /** One hazard chip of a substance row (IARC class, CLP class, WFD, PBT). */
  function Tag(props) {
    if (!props.tag) return null;
    return html`<span className=${"fm-tag fm-tag--" + props.tag.kind}
      title=${props.tag.title}>${props.tag.text}</span>`;
  }

  /** "air" / "water" / "both": the medium a substance was released to. */
  function MediumChip(props) {
    var flag = props.flag;
    var kind = flag === MEDIUM_BOTH ? "both" : flag === MEDIUM_WATER ? "water" : "air";
    return html`<span className=${"fm-medium fm-medium--" + kind}
      title=${"Released to " + mediumName(flag)}>${mediumShort(flag)}</span>`;
  }

  /**
   * The air/water split of one total. The medium the map is measuring is
   * marked, so the numbers in the card explain the dot that was drawn.
   */
  function MediumSplit(props) {
    return html`<div className="fm-tip__split">
      <span className=${"fm-tip__part" + (props.medium === "air" ? " is-active" : "")}
            title="Released to air">
        <span className="fm-tip__part-name">Air</span> ${mass(props.airKg)}
      </span>
      <span className=${"fm-tip__part" + (props.medium === "water" ? " is-active" : "")}
            title="Released to water">
        <span className="fm-tip__part-name">Water</span> ${mass(props.waterKg)}
      </span>
    </div>`;
  }



  /** Tooltip for one facility, read in the units of the current view. */
  function FacilityTip(props) {
    var row = props.row;
    var payload = props.payload;
    var slots = props.slots;
    var lauSlots = props.lauSlots;
    var view = props.view;
    var measure = props.measure;

    var level = payload.levels[row[slots.level]] || payload.levels[0];
    var lauIndex = row[slots.lauIndex];
    var lau = lauIndex >= 0 ? payload.lau[lauIndex] : null;
    var place = lau
      ? lau[lauSlots.name] +
        " \u00b7 " +
        countryName(payload, lau[lauSlots.countryCode])
      : "Municipality not matched";

    var activity = payload.ied.activities[row[slots.iedActivityIndex]] || null;
    var status = payload.ied.statuses[row[slots.iedStatusIndex]] || "";
    var permit = row[slots.permitYear];
    var sector = (payload.sectors || [])[row[slots.sectorIndex]] || "";
    var permitUrl =
      ((payload.ied && payload.ied.permitUrls) || [])[row[slots.permitUrlIndex]] || "";
    var value = measureMass(measure, slots, row);
    var detail = substanceRows(payload, view, measure, props.index);
    var hidden = detail ? detail.count - detail.rows.length : 0;
    var halves = mediumSlots(view, slots);

    return html`
      <div className="fm-tip">
        <div className="fm-tip__name">${row[slots.name]}</div>
        <div className="fm-tip__place">${place}</div>
        <div className="fm-tip__total">
          <strong>${mass(value)}</strong>
          <span>in this view</span>
        </div>
        <${MediumSplit} airKg=${halves.air >= 0 ? row[halves.air] : 0}
          waterKg=${halves.water >= 0 ? row[halves.water] : 0} medium=${measure.id} />
        <div className="fm-tip__level">
          <span className="fm-tip__swatch"
                style=${{ background: tint(level.color),
                          boxShadow: "inset 0 0 0 1px " + level.outline }}></span>
          <span>${level.short}</span>
          <span className="fm-tip__quiet">strongest class among its substances</span>
        </div>
        ${detail && detail.rows.length
          ? html`<div className="fm-tip__subs">
              <div className="fm-tip__subs-head">
                <span>Substances behind this total</span>
                <span className="fm-tip__quiet">
                  ${hidden > 0
                    ? "top " + detail.rows.length + " of " + detail.count
                    : plural(detail.count, "substance", "substances")}
                </span>
              </div>
              <ul className="fm-tip__sub-list">
                ${detail.rows.map(function (item, position) {
                  return html`<li key=${"sub-" + position} className="fm-tip__sub">
                    <span className="fm-tip__sub-name" title=${item.note || item.name}>
                      ${item.name}
                    </span>
                    <span className="fm-tip__sub-kg">${mass(item.kilograms)}</span>
                    <span className="fm-tip__sub-tags">
                      <${MediumChip} flag=${item.medium} />
                      ${item.tags.length
                        ? item.tags.slice(0, 3).map(function (tag) {
                            return html`<${Tag} key=${tag.text + position} tag=${tag} />`;
                          })
                        : html`<span className="fm-tag fm-tag--none"
                            title="No hazard classification for this substance">unclassified</span>`}
                    </span>
                  </li>`;
                })}
              </ul>
            </div>`
          : html`<p className="fm-tip__note">
              No substance of this view was released to
              ${measure.id === "combined" ? " air or water." : " " + measure.id + "."}
            </p>`}
        <dl className="fm-tip__facts">
          ${sector
            ? html`<div>
                <dt>E-PRTR sector</dt>
                <dd>${sector}</dd>
              </div>`
            : null}
          ${activity
            ? html`<div>
                <dt>IED activity</dt>
                <dd>${activity.code} \u00b7 ${activity.name}</dd>
              </div>`
            : null}
          ${status ? html`<div><dt>Status</dt><dd>${status}</dd></div>` : null}
          ${permit > 0 || permitUrl
            ? html`<div>
                <dt>Permit</dt>
                <dd>
                  ${permit > 0
                    ? html`<span title="Year the permit was granted">granted ${permit}</span>`
                    : null}
                  ${permit > 0 && permitUrl
                    ? html`<span className="fm-tip__quiet"> \u00b7 </span>`
                    : null}
                  ${permitUrl
                    ? html`<a className="fm-tip__link" href=${permitUrl}
                           target="_blank" rel="noopener noreferrer"
                           title=${"Permit record published by the country: " +
                             permitUrl}
                         >${permitHost(permitUrl)} \u2197</a>`
                    : null}
                </dd>
              </div>`
            : null}
          <div>
            <dt>CO<sub>2</sub>, left out here</dt>
            <dd>${row[slots.co2Kg] > 0 ? mass(row[slots.co2Kg]) : "none reported"}</dd>
          </div>
        </dl>
      </div>`;
  }

  /** Tooltip for a municipality: a locator, so no mass per square kilometre. */
  function LauTip(props) {
    var row = props.row;
    var payload = props.payload;
    var slots = props.slots;
    var view = props.view;
    var measure = props.measure;
    var halves = mediumSlots(view, slots);

    return html`
      <div className="fm-tip">
        <div className="fm-tip__name">${row[slots.name]}</div>
        <div className="fm-tip__place">
          ${countryName(payload, row[slots.countryCode])} \u00b7 ${area(row[slots.areaKm2])}
        </div>
        <div className="fm-tip__total">
          <strong>${mass(measureMass(measure, slots, row))}</strong>
          <span>in this view</span>
        </div>
        <${MediumSplit} airKg=${halves.air >= 0 ? row[halves.air] : 0}
          waterKg=${halves.water >= 0 ? row[halves.water] : 0} medium=${measure.id} />
        <dl className="fm-tip__facts">
          <div>
            <dt>Facilities</dt>
            <dd>${plural(row[slots.nFacilities], "facility", "facilities")}</dd>
          </div>
        </dl>
        <p className="fm-tip__note">
          Outline only: the map does not shade municipalities by mass, so the
          colour stays with the facility dots.
        </p>
      </div>`;
  }

  /** One tooltip, positioned inside the plot box. */
  function Tooltip(props) {
    var shown = props.shown;
    var payload = props.payload;

    // The card is taller than its fallback whenever a facility names four
    // substances, so it measures itself and the clamping uses the real height.
    var boxRef = useRef(null);
    var heightState = useState(0);
    var measuredHeight = heightState[0];
    var setMeasuredHeight = heightState[1];

    useLayoutEffect(function () {
      var node = boxRef.current;
      if (!node) return;
      var next = node.offsetHeight;
      if (next > 0 && Math.abs(next - measuredHeight) > 2) setMeasuredHeight(next);
    });

    if (!shown) return null;

    var boxHeight = measuredHeight || props.boxHeight;
    var interactive = props.interactive;
    var node =
      shown.kind === "facility"
        ? html`<${FacilityTip} index=${shown.index} row=${payload.facilities[shown.index]}
            payload=${payload} slots=${props.slots} lauSlots=${props.lauSlots}
            view=${props.view} measure=${props.measure} />`
        : html`<${LauTip} row=${payload.lau[shown.index]} payload=${payload}
            slots=${props.lauSlots} view=${props.view} measure=${props.measure} />`;

    // Keep the box inside the plot and clear of the floating controls: the zoom
    // buttons sit in the right-hand strip and the scale bar along the bottom.
    var gap = 14;
    var EDGE = 6;
    var CONTROLS = 42;
    var FOOTER = 26;
    var maxLeft = Math.max(EDGE, props.width - props.boxWidth - CONTROLS);
    var left = shown.x + gap;
    if (left > maxLeft) {
      left = shown.x - gap - props.boxWidth;
    }
    left = Math.max(EDGE, Math.min(left, maxLeft));
    var maxTop = Math.max(EDGE, props.height - boxHeight - FOOTER);
    var top = Math.max(EDGE, Math.min(shown.y - 60, maxTop));

    return html`
      <div className=${"fm-tip-box" + (interactive ? " is-pinned" : "")}
           role=${interactive ? "region" : "tooltip"}
           aria-label=${interactive ? "Pinned facility card" : undefined}
           ref=${boxRef}
           style=${{ left: left + "px", top: top + "px", width: props.boxWidth + "px" }}>
        ${interactive
          ? html`<button type="button" className="fm-tip__close"
                  aria-label="Close the card" title="Close the card"
                  onClick=${props.onClose}>\u00d7</button>`
          : null}
        ${node}
      </div>`;
  }

  /* ----------------------------------------------------------------- legend */

  function Legend(props) {
    var payload = props.payload;
    var view = props.view;
    var measure = props.measure;
    var radius = props.radius;
    var levelCounts = props.levelCounts;

    var levels = payload.levels.map(function (level, index) {
      return html`
        <li key=${"level-" + level.level} className="fm-legend__item"
            title=${level.description}>
          <span className="fm-legend__swatch"
                style=${{ background: tint(level.color),
                          boxShadow: "inset 0 0 0 1px " + level.outline }}></span>
          <span className="fm-legend__text">${level.short}</span>
          <span className="fm-legend__count">${d3.format(",d")(levelCounts[index] || 0)}</span>
        </li>`;
    });

    // The ticks come from the payload, so the legend labels are decades of the
    // scale itself (1 t, 1 kt, 100 kt on the combined view) and not fractions
    // of a maximum. Three circles, never at the ends: the smallest dot of the
    // scale is a speck and the largest is shared by every plant past the 1 Mt
    // cap, so neither is a size to compare against. The scale stops at 1 Mt:
    // the giants share that circle, and the band that carries the mass - 100 t
    // to 1 Mt - keeps the whole radius range.
    var box = 2 * RADIUS_MAX + 2;
    var sizes = (measure.size.ticks || []).map(function (value) {
      return html`
        <li key=${"size-" + value} className="fm-legend__size">
          <svg width=${box} height=${box} aria-hidden="true">
            <circle cx=${box / 2} cy=${box / 2} r=${radius(value)}
                    fill=${MUTED} fillOpacity=${DOT_FILL_OPACITY}></circle>
          </svg>
          <span>${tickMass(value)}</span>
        </li>`;
    });
    var capped = measure.size.cappedFacilities || 0;

    return html`
      <aside className="fm-legend">
        <section className="fm-legend__block">
          <h4 className="fm-legend__head">
            Hazard class <span className="fm-legend__quiet">dot colour</span>
          </h4>
          <ul className="fm-legend__list">${levels}</ul>
          <p className="fm-legend__note">
            Strongest class among the substances each facility reported.
          </p>
        </section>
        <section className="fm-legend__block">
          <h4 className="fm-legend__head">Dot size <span className="fm-legend__quiet">total, ${measureShort(measure)}</span></h4>
          <ul className="fm-legend__sizes">${sizes}</ul>
          <p className="fm-legend__note">
            Mass on a log scale: every step is a decade, so a dot that looks
            twice as wide carries orders of magnitude more mass.
            ${capped > 0
              ? " " +
                plural(capped, "facility", "facilities") +
                " above " +
                tickMass(measure.size.capKg) +
                " share the largest circle."
              : ""}
          </p>
        </section>
        <section className="fm-legend__block">
          <h4 className="fm-legend__head">
            Municipalities <span className="fm-legend__quiet">outlines</span>
          </h4>
          <p className="fm-legend__note">
            Thin outlines mark the ${d3.format(",d")(payload.meta.lauCount)} municipalities
            that host at least one facility; hover one for its
            total. They carry no shading, so the colour stays with the dots.
          </p>
        </section>
      </aside>`;
  }

  /* ----------------------------------------------------------------- search */

  var KIND_LABEL = ["Country", "Municipality", "Facility"];

  /**
   * Everything the search box can reach: countries, municipalities and
   * facilities, each with a pre-lowered haystack, so a keystroke is a scan of
   * plain indexOf calls instead of rebuilding strings per candidate.
   */
  function buildEntries(payload, slots, lauSlots, countries) {
    var entries = [];
    var seen = new Set();

    ((countries && countries.codes) || []).forEach(function (code) {
      var name = countryName(payload, code);
      entries.push({
        kind: SEARCH_COUNTRY,
        label: name,
        detail: "Country",
        code: code,
        rank: 0,
        search: name.toLowerCase(),
      });
    });

    payload.lau.forEach(function (row, index) {
      var name = row[lauSlots.name];
      var country = countryName(payload, row[lauSlots.countryCode]);
      var key = "m|" + name + "|" + country;
      if (seen.has(key)) return;
      seen.add(key);
      entries.push({
        kind: SEARCH_MUNICIPALITY,
        label: name,
        detail: country,
        index: index,
        rank: 1,
        search: (name + " " + country).toLowerCase(),
      });
    });

    // Facility names repeat across the register ("Waste incineration plant"),
    // so identical name-and-place pairs collapse into one hit that frames the
    // place rather than each unit separately.
    payload.facilities.forEach(function (row, index) {
      var name = row[slots.name];
      if (!name) return;
      var lauIndex = row[slots.lauIndex];
      var lau = lauIndex >= 0 ? payload.lau[lauIndex] : null;
      var detail = lau
        ? lau[lauSlots.name] +
          " \u00b7 " +
          countryName(payload, lau[lauSlots.countryCode])
        : "";
      var key = "f|" + name + "|" + detail;
      if (seen.has(key)) return;
      seen.add(key);
      entries.push({
        kind: SEARCH_FACILITY,
        label: name,
        detail: detail,
        index: index,
        rank: 2,
        search: (name + " " + detail).toLowerCase(),
      });
    });

    return entries;
  }

  /** Best matches for a query: word start first, then anywhere, short first. */
  function matchEntries(entries, query) {
    var needle = query.toLowerCase();
    var scored = [];
    for (var i = 0; i < entries.length; i += 1) {
      var haystack = entries[i].search;
      var at = haystack.indexOf(needle);
      if (at < 0) continue;
      var boundary = at === 0 ? 0 : haystack.charAt(at - 1) === " " ? 1 : 2;
      scored.push({ entry: entries[i], boundary: boundary, at: at });
    }
    scored.sort(function (a, b) {
      return (
        a.boundary - b.boundary ||
        a.at - b.at ||
        a.entry.rank - b.entry.rank ||
        a.entry.label.length - b.entry.label.length ||
        (a.entry.label < b.entry.label ? -1 : 1)
      );
    });
    var out = [];
    for (var k = 0; k < scored.length && out.length < SEARCH_LIMIT; k += 1) {
      out.push(scored[k].entry);
    }
    return out;
  }

  /** Label with the matched span marked. */
  function highlight(label, query) {
    var at = query ? label.toLowerCase().indexOf(query.toLowerCase()) : -1;
    if (at < 0) return html`<span>${label}</span>`;
    return html`<span
      >${label.slice(0, at)}<mark>${label.slice(at, at + query.length)}</mark>${label.slice(
        at + query.length,
      )}</span
    >`;
  }

  /** Search over countries, municipalities and facilities; Enter frames a hit. */
  function SearchBox(props) {
    var state = useState("");
    var query = state[0];
    var setQuery = state[1];
    var openState = useState(false);
    var open = openState[0];
    var setOpen = openState[1];
    var activeState = useState(0);
    var active = activeState[0];
    var setActive = activeState[1];
    var inputRef = useRef(null);

    // "Reset view" hands down a new token: empty the box as well.
    useEffect(
      function () {
        setQuery("");
        setOpen(false);
        setActive(0);
      },
      [props.resetToken],
    );

    var matches = useMemo(
      function () {
        var needle = query.trim();
        return needle.length < 2 ? [] : matchEntries(props.entries, needle);
      },
      [props.entries, query],
    );

    function choose(entry) {
      if (!entry) return;
      setQuery(entry.label);
      setOpen(false);
      setActive(0);
      if (inputRef.current) inputRef.current.blur();
      props.onSelect(entry);
    }

    function onKeyDown(event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setOpen(true);
        setActive(Math.min(active + 1, Math.max(matches.length - 1, 0)));
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setActive(Math.max(active - 1, 0));
      } else if (event.key === "Enter") {
        if (matches.length) {
          event.preventDefault();
          choose(matches[Math.min(active, matches.length - 1)]);
        }
      } else if (event.key === "Escape") {
        setOpen(false);
      }
    }

    var list = null;
    if (open && query.trim().length >= 2) {
      list = matches.length
        ? html`<ul className="fm-search__list" role="listbox" id="fm-search-list">
            ${matches.map(function (entry, position) {
              return html`<li key=${entry.kind + "|" + entry.label + "|" + entry.detail}
                  id=${"fm-search-" + position} role="option"
                  aria-selected=${position === active ? "true" : "false"}
                  className=${"fm-search__item" + (position === active ? " is-active" : "")}
                  onMouseDown=${function (event) {
                    event.preventDefault();
                  }}
                  onMouseEnter=${function () {
                    setActive(position);
                  }}
                  onClick=${function () {
                    choose(entry);
                  }}>
                <span className="fm-search__kind">${KIND_LABEL[entry.kind]}</span>
                <span className="fm-search__label">${highlight(entry.label, query.trim())}</span>
                <span className="fm-search__detail">${entry.detail}</span>
              </li>`;
            })}
          </ul>`
        : html`<p className="fm-search__empty">
            No country, municipality or facility matches.
          </p>`;
    }

    return html`
      <div className="fm-search">
        <svg className="fm-search__icon" viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="6.6" cy="6.6" r="4.4" fill="none" stroke="currentColor"
                  strokeWidth="1.4"></circle>
          <line x1="10" y1="10" x2="14" y2="14" stroke="currentColor"
                strokeWidth="1.4" strokeLinecap="round"></line>
        </svg>
        <input ref=${inputRef} type="text" className="fm-search__input"
               placeholder="Search a country, a municipality or a facility"
               role="combobox" aria-expanded=${open ? "true" : "false"}
               aria-controls="fm-search-list" aria-autocomplete="list"
               aria-label="Search the map"
               value=${query}
               onInput=${function (event) {
                 setQuery(event.target.value);
                 setOpen(true);
                 setActive(0);
               }}
               onFocus=${function () {
                 if (query.trim().length >= 2) setOpen(true);
               }}
               onBlur=${function () {
                 setOpen(false);
               }}
               onKeyDown=${onKeyDown} />
        ${query
          ? html`<button type="button" className="fm-search__clear"
                aria-label="Clear the search"
                onClick=${function () {
                  setQuery("");
                  setOpen(false);
                  setActive(0);
                  if (inputRef.current) inputRef.current.focus();
                }}>\u00d7</button>`
          : null}
        ${list}
      </div>`;
  }

  /* ------------------------------------------------------------------- plot */

  /*
   * The three geometry layers are pure functions of the geometry and the view,
   * and there are more than eight thousand paths on the municipality layer. They
   * are memoised components so a pointer move only re-renders the highlight, the
   * scale bar and the card - not the map itself.
   */

  var CountryLayer = React.memo(function CountryLayer(props) {
    return html`<g className="fm-base">
      ${props.paths.map(function (path, index) {
        return html`<path key=${"c" + index} d=${path}
          fill=${props.fill} stroke=${props.stroke} strokeWidth="0.9"
          strokeLinejoin="round" vectorEffect="non-scaling-stroke"></path>`;
      })}
    </g>`;
  });

  var LauLayer = React.memo(function LauLayer(props) {
    var payload = props.payload;
    var slots = props.slots;
    return html`<g className="fm-municipalities">
      ${payload.lau.map(function (row, index) {
        var shape = props.shapeOf.get(row[slots.id]);
        if (shape === undefined) return null;
        // A transparent fill keeps the whole municipality hoverable, while the
        // stroke is the only thing the reader sees.
        return html`<path key=${"l" + index} data-lau=${index} d=${props.paths[shape]}
          fill="transparent" stroke=${props.stroke} strokeWidth="0.5"
          strokeLinejoin="round" vectorEffect="non-scaling-stroke"></path>`;
      })}
    </g>`;
  });

  var DotLayer = React.memo(function DotLayer(props) {
    return html`<g className="fm-facilities" pointerEvents="none">
      ${props.levels.map(function (level, index) {
        var path = props.paths[index];
        if (!path) return null;
        // Every dot keeps a hairline edge in its own level's deeper shade: at
        // 75% fill opacity the land shows through, so the edge - not the fill -
        // is what separates a dot from the surface and from its neighbour. One
        // shared ink stroke would veil the fills instead.
        // The stroke ignores the zoom transform, or it would grow into a band.
        return html`<path key=${"d" + index} d=${path} fill=${level.color}
          fillOpacity=${DOT_FILL_OPACITY} stroke=${level.outline}
          strokeWidth=${DOT_STROKE}
          vectorEffect="non-scaling-stroke"></path>`;
      })}
    </g>`;
  });

  function MapPlot(props) {
    var payload = props.payload;
    var view = props.view;
    var measure = props.measure;
    var slots = props.slots;
    var lauSlots = props.lauSlots;
    var radius = props.radius;
    var zoom = props.zoom;
    var setZoom = props.setZoom;
    var pinned = props.pinned;
    var setPinned = props.setPinned;
    var focus = props.focus;

    var plotRef = useRef(null);
    var measured = useElementWidth(plotRef);
    var hoverState = useState(null);
    var hover = hoverState[0];
    var setHover = hoverState[1];
    var dragRef = useRef(null);
    var suppressClick = useRef(false);

    // The wheel belongs to the page until the reader puts the map in charge: a
    // press or a tab into the map, then the wheel zooms, Escape or reset gives
    // the page its scroll back. Without this gate, scrolling the article past
    // the figure zoomed it instead - the map moved under a reader who only
    // meant to read on.
    var engageState = useState(false);
    var engaged = engageState[0];
    var setEngaged = engageState[1];

    var extent = payload.meta.window;
    var width = Math.max(320, Math.round(measured || 920));
    var height = Math.max(
      380,
      Math.min(
        880,
        Math.round((width * (extent[3] - extent[1])) / (extent[2] - extent[0])),
      ),
    );
    var fit = useMemo(
      function () {
        return fitWindow(extent, width, height);
      },
      [extent, width, height],
    );

    /* ------------------------------------------------------------ zoom state */

    function clamp(next) {
      var k = Math.min(ZOOM_MAX, Math.max(1, next.k));
      var x = Math.min(0, Math.max(width - width * k, next.x));
      var y = Math.min(0, Math.max(height - height * k, next.y));
      // Returning the same object when nothing moved lets React bail out of
      // the render: a drag against an edge would otherwise redraw the dots for
      // every pointer move.
      if (k === next.k && x === next.x && y === next.y) return next;
      return { k: k, x: x, y: y };
    }

    /** Zoom by `factor` around a point of the plot box, in pixels. */
    function zoomBy(factor, atX, atY) {
      setZoom(function (current) {
        var k = Math.min(ZOOM_MAX, Math.max(1, current.k * factor));
        var ratio = k / current.k;
        return clamp({
          k: k,
          x: atX - (atX - current.x) * ratio,
          y: atY - (atY - current.y) * ratio,
        });
      });
    }

    // A search hit frames its place: countries and municipalities by their
    // bounding box, facilities at a fixed radius. The token is what the frame
    // hangs on, so a later resize (or a re-measure of the plot) cannot throw
    // the reader's own zoom away by re-applying an old search.
    var framed = useRef(null);
    useEffect(
      function () {
        if (!focus || !focus.box) return;
        if (framed.current === focus.token) return;
        var box = focus.box;
        var left = fit.place.x(box[0]);
        var right = fit.place.x(box[2]);
        var top = fit.place.y(box[3]);
        var bottom = fit.place.y(box[1]);
        var boxWidth = Math.max(right - left, 8);
        var boxHeight = Math.max(bottom - top, 8);
        var k = Math.min(
          ZOOM_MAX,
          Math.max(1, 0.92 * Math.min(width / boxWidth, height / boxHeight)),
        );
        var cx = (left + right) / 2;
        var cy = (top + bottom) / 2;
        framed.current = focus.token;
        setZoom(clamp({ k: k, x: width / 2 - cx * k, y: height / 2 - cy * k }));
      },
      [focus, fit, width, height],
    );

    // A resize keeps the zoom level and only pulls the view back inside the
    // plot: the map must not jump to another scale when the window changes.
    useEffect(
      function () {
        setZoom(function (current) {
          return clamp(current);
        });
      },
      [width, height],
    );

    // Wheel zoom has to be a non-passive listener, or the page scrolls instead.
    // It is attached only while the map is engaged (see engageState), so a
    // reader scrolling the article keeps the page's own scroll.
    useEffect(
      function () {
        var node = plotRef.current;
        if (!node || !engaged) return undefined;
        function onWheel(event) {
          if (event.ctrlKey || event.metaKey) return;
          event.preventDefault();
          var box = node.getBoundingClientRect();
          zoomBy(
            Math.exp(-event.deltaY * WHEEL_RATE),
            event.clientX - box.left,
            event.clientY - box.top,
          );
        }
        node.addEventListener("wheel", onWheel, { passive: false });
        return function () {
          node.removeEventListener("wheel", onWheel);
        };
      },
      [width, height, engaged],
    );

    /* ------------------------------------------------------------- geometry */

    var countryPaths = useMemo(
      function () {
        return props.countries.shapes.map(function (shape) {
          return shapePath(shape, props.countries.scale, fit.place);
        });
      },
      [props.countries, fit],
    );

    // The geometry file lists the municipalities in its own order (GISCO
    // feature order), so the payload rows are joined to it by LAU id.
    var lauShapeOf = useMemo(
      function () {
        var byId = new Map();
        var ids = props.lau.ids || [];
        for (var i = 0; i < ids.length; i += 1) byId.set(ids[i], i);
        return byId;
      },
      [props.lau],
    );

    var lauPaths = useMemo(
      function () {
        return props.lau.shapes.map(function (shape) {
          return shapePath(shape, props.lau.scale, fit.place);
        });
      },
      [props.lau, fit],
    );

    /*
     * Dots per hazard class, largest first so the big plants stay on top.
     * The radius is divided by the zoom fade (see DOT_FADE): the group scales
     * by k, so this keeps a dot's screen size nearly constant while its
     * neighbours spread out. The fade is quantised (see FADE_STEP) so a wheel
     * gesture redraws the layer a few dozen times instead of once per notch,
     * and only the quantised step is a dependency - a pan moves the group and
     * leaves the paths alone.
     */
    var fadeStep = Math.round(Math.log(zoom.k) / FADE_STEP);
    var fade = Math.exp(-DOT_FADE * FADE_STEP * fadeStep);
    var dots = useMemo(
      function () {
        var rows = payload.facilities;
        var order = [];
        var massOf = [];
        for (var i = 0; i < rows.length; i += 1) {
          var value = measureMass(measure, slots, rows[i]);
          massOf.push(value);
          if (value > 0) order.push(i);
        }
        order.sort(function (a, b) {
          return massOf[b] - massOf[a];
        });

        var segments = payload.levels.map(function () {
          return [];
        });
        var placed = [];
        var byFacility = new Map();
        for (var k = 0; k < order.length; k += 1) {
          var row = rows[order[k]];
          var dot = {
            facility: order[k],
            level: row[slots.level],
            x: fit.place.x(row[slots.x]),
            y: fit.place.y(row[slots.y]),
            r: radius(massOf[order[k]]) * fade,
          };
          placed.push(dot);
          byFacility.set(dot.facility, dot);
          segments[dot.level].push(dotPath(dot.x, dot.y, dot.r));
        }

        return {
          list: placed,
          byFacility: byFacility,
          grid: indexDots(placed),
          paths: segments.map(function (parts) {
            return parts.join("");
          }),
        };
      },
      [payload, slots, measure, fit, radius, fade],
    );

    /* -------------------------------------------------------------- pointer */

    function place(clientX, clientY) {
      var box = plotRef.current.getBoundingClientRect();
      var screenX = clientX - box.left;
      var screenY = clientY - box.top;
      return {
        x: (screenX - zoom.x) / zoom.k,
        y: (screenY - zoom.y) / zoom.k,
        screenX: screenX,
        screenY: screenY,
      };
    }

    /** What the pointer is over: a dot first, then the municipality below it. */
    function locate(clientX, clientY, target) {
      var at = place(clientX, clientY);
      var hit = nearestDot(dots.grid, at.x, at.y, HIT_RADIUS / zoom.k);
      if (hit) {
        return {
          kind: "facility",
          index: hit.facility,
          x: at.screenX,
          y: at.screenY,
          mapX: at.x,
          mapY: at.y,
        };
      }
      var attribute =
        target && target.getAttribute ? target.getAttribute("data-lau") : null;
      if (attribute !== null && attribute !== undefined && attribute !== "") {
        return {
          kind: "lau",
          index: Number(attribute),
          x: at.screenX,
          y: at.screenY,
          mapX: at.x,
          mapY: at.y,
        };
      }
      return null;
    }

    /* The zoom buttons sit inside the plot, so a press on one must not start a
       pan: pointer capture would retarget the press and swallow the click. */
    function fromControl(target) {
      return !!(target && target.closest && target.closest("button"));
    }

    /* A pinned card is clickable (its permit link must be reachable), so it is
       not the map below it: a press, a click or a hover on the card leaves the
       map alone and keeps the card where it is. */
    function fromCard(target) {
      return !!(target && target.closest && target.closest(".fm-tip-box"));
    }

    function onPointerDown(event) {
      if (event.button !== 0 || fromControl(event.target) || fromCard(event.target))
        return;
      setEngaged(true);
      dragRef.current = {
        clientX: event.clientX,
        clientY: event.clientY,
        x: zoom.x,
        y: zoom.y,
        moved: false,
      };
      suppressClick.current = false;
      if (event.currentTarget.setPointerCapture) {
        event.currentTarget.setPointerCapture(event.pointerId);
      }
    }

    function onPointerMove(event) {
      var drag = dragRef.current;
      if (drag) {
        var dx = event.clientX - drag.clientX;
        var dy = event.clientY - drag.clientY;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) < DRAG_SLACK) return;
        drag.moved = true;
        setHover(null);
        setZoom(clamp({ k: zoom.k, x: drag.x + dx, y: drag.y + dy }));
        return;
      }
      // No municipality card while the pointer is over a control, and no card
      // for the dot behind a pinned one while the pointer is inside it.
      if (fromControl(event.target) || fromCard(event.target)) {
        setHover(null);
        return;
      }
      setHover(locate(event.clientX, event.clientY, event.target));
    }

    function onPointerUp() {
      var drag = dragRef.current;
      dragRef.current = null;
      // A pan is not a click on whatever happened to be under the pointer.
      if (drag && drag.moved) suppressClick.current = true;
    }

    function onClick(event) {
      if (suppressClick.current) {
        suppressClick.current = false;
        return;
      }
      if (fromControl(event.target) || fromCard(event.target)) return;
      var next = locate(event.clientX, event.clientY, event.target);
      setPinned(function (current) {
        if (
          current &&
          next &&
          current.kind === next.kind &&
          current.index === next.index
        ) {
          return null;
        }
        return next;
      });
    }

    function resetView() {
      setEngaged(false);
      props.onReset();
    }

    function onKeyDown(event) {
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        zoomBy(ZOOM_STEP, width / 2, height / 2);
      } else if (event.key === "-") {
        event.preventDefault();
        zoomBy(1 / ZOOM_STEP, width / 2, height / 2);
      } else if (event.key === "0") {
        event.preventDefault();
        resetView();
      } else if (event.key === "Escape" && (pinned || hover)) {
        // The pinned card holds a link now, so Escape is the way out of it
        // without hunting for the dot behind the card.
        event.preventDefault();
        setPinned(null);
        setHover(null);
      } else if (event.key === "Escape" && engaged) {
        // Same key, second job: hand the wheel back to the page.
        setEngaged(false);
      }
    }

    /* --------------------------------------------------------------- render */

    var shown = hover || pinned;
    var hovered =
      shown && shown.kind === "facility"
        ? dots.byFacility.get(shown.index) || null
        : null;

    // Cards are anchored in map space, so a pinned one stays glued to its dot
    // when the map is zoomed or panned after the click.
    var anchor = shown;
    if (shown) {
      var anchorX;
      var anchorY;
      if (typeof shown.mapX === "number") {
        anchorX = shown.mapX;
        anchorY = shown.mapY;
      } else if (hovered) {
        anchorX = hovered.x;
        anchorY = hovered.y;
      } else if (shown.at) {
        anchorX = fit.place.x(shown.at.x);
        anchorY = fit.place.y(shown.at.y);
      } else {
        // A facility picked in the search box: anchor on its own dot.
        var anchorRow = payload.facilities[shown.index];
        anchorX = fit.place.x(anchorRow[slots.x]);
        anchorY = fit.place.y(anchorRow[slots.y]);
      }
      anchor = {
        kind: shown.kind,
        index: shown.index,
        x: anchorX * zoom.k + zoom.x,
        y: anchorY * zoom.k + zoom.y,
      };
    }

    var lauGlow = null;
    if (shown && shown.kind === "lau") {
      var glowShape = lauShapeOf.get(payload.lau[shown.index][lauSlots.id]);
      if (glowShape !== undefined) {
        lauGlow = html`<path d=${lauPaths[glowShape]} fill="none" stroke=${INK}
          strokeWidth="1.4" strokeLinejoin="round" vectorEffect="non-scaling-stroke"
          pointerEvents="none"></path>`;
      }
    }

    // The highlight keeps a constant screen width at every zoom level.
    var ring = hovered
      ? html`<circle cx=${hovered.x} cy=${hovered.y}
          r=${hovered.r + 3 / zoom.k} fill="none" stroke=${INK}
          strokeWidth=${1.2 / zoom.k} pointerEvents="none"></circle>`
      : null;

    // Scale bar: the shortest round distance that still spans ~140 px.
    var pxPerMetre = fit.k * zoom.k;
    var target = 140 / (pxPerMetre * 1000);
    var barKm =
      SCALE_DISTANCES.filter(function (km) {
        return km >= target * 0.8;
      })[0] || SCALE_DISTANCES[SCALE_DISTANCES.length - 1];
    var barPixels = barKm * 1000 * pxPerMetre;
    var barY = height - 18;
    // Below a kilometre the bar is labelled in metres: "0.25 km" is a distance
    // nobody reads at a glance.
    var barLabel =
      barKm < 1
        ? d3.format(",")(Math.round(barKm * 1000)) + " m"
        : d3.format(",")(barKm) + " km";

    var zoomed = zoom.k > 1.0001;
    var controls = html`
      <div className="fm-zoom" role="group" aria-label="Zoom the map">
        <button type="button" className="fm-zoom__btn" title="Zoom in (press +)"
                aria-label="Zoom in" disabled=${zoom.k >= ZOOM_MAX - 1e-6}
                onClick=${function () {
                  zoomBy(ZOOM_STEP, width / 2, height / 2);
                }}>+</button>
        <button type="button" className="fm-zoom__btn" title="Zoom out (press -)"
                aria-label="Zoom out" disabled=${!zoomed}
                onClick=${function () {
                  zoomBy(1 / ZOOM_STEP, width / 2, height / 2);
                }}>\u2212</button>
        <button type="button" className="fm-zoom__btn" title="Reset the view (press 0)"
                aria-label="Reset the view" onClick=${resetView}>\u21ba</button>
      </div>`;

    return html`
      <div className=${"fm-plot" + (zoomed ? " is-zoomed" : "") +
             (engaged ? " is-engaged" : "")} ref=${plotRef}
           style=${{ height: height + "px" }} tabIndex="0" role="group"
           aria-label=${"Map of " + view.heading.toLowerCase() + ", " +
             measureShort(measure) +
             ". Drag to pan; press the map once, then scroll or double-click to zoom."}
           onPointerDown=${onPointerDown}
           onPointerMove=${onPointerMove}
           onPointerUp=${onPointerUp}
           onFocus=${function () {
             setEngaged(true);
           }}
           onBlur=${function () {
             // Tabbing out of the figure hands the wheel back, like Escape.
             setEngaged(false);
           }}
           onPointerLeave=${function () {
             dragRef.current = null;
             setHover(null);
           }}
           onDoubleClick=${function (event) {
             if (fromControl(event.target) || fromCard(event.target)) return;
             var box = plotRef.current.getBoundingClientRect();
             zoomBy(ZOOM_STEP, event.clientX - box.left, event.clientY - box.top);
           }}
           onClick=${onClick}
           onKeyDown=${onKeyDown}>
        <svg width=${width} height=${height} role="img"
             aria-label=${view.heading + " by facility location, " + measureShort(measure)}>
          <rect className="fm-sea" x="0" y="0" width=${width} height=${height}></rect>
          <g transform=${"translate(" + zoom.x + " " + zoom.y + ") scale(" + zoom.k + ")"}>
            <${CountryLayer} paths=${countryPaths}
              fill=${payload.countries.fill} stroke=${payload.countries.stroke} />
            <${LauLayer} payload=${payload} slots=${lauSlots} paths=${lauPaths}
              shapeOf=${lauShapeOf} stroke=${LAU_STROKE} />
            ${lauGlow}
            <${DotLayer} levels=${payload.levels} paths=${dots.paths} />
            ${ring}
          </g>
          <g className="fm-scale" pointerEvents="none">
            <line x1="14" x2=${14 + barPixels} y1=${barY} y2=${barY}
                  stroke=${INK} strokeWidth="1.4"></line>
            <line x1="14" x2="14" y1=${barY - 4} y2=${barY + 4}
                  stroke=${INK} strokeWidth="1.4"></line>
            <line x1=${14 + barPixels} x2=${14 + barPixels} y1=${barY - 4}
                  y2=${barY + 4} stroke=${INK} strokeWidth="1.4"></line>
            <text className="fm-scale-label" x=${14 + barPixels / 2} y=${barY - 6}
                  textAnchor="middle">${barLabel}</text>
          </g>
        </svg>
        ${controls}
        <${Tooltip} shown=${anchor} payload=${payload} slots=${slots}
          lauSlots=${lauSlots} view=${view} measure=${measure}
          interactive=${!hover && !!pinned}
          onClose=${function () {
            setPinned(null);
            setHover(null);
          }}
          width=${width} height=${height}
          boxWidth=${TIP_WIDTH} boxHeight=${TIP_HEIGHT} />
      </div>`;
  }

  /* -------------------------------------------------------------------- app */

  /** Air / water / both: the second axis of every view. */
  function MediumSwitch(props) {
    var buttons = (props.media || []).map(function (item) {
      var active = item.id === props.value;
      return html`
        <button key=${item.id} type="button" role="tab"
                aria-selected=${active ? "true" : "false"}
                className=${"fm-switch__btn" + (active ? " is-active" : "")}
                title=${"Measure " + item.note}
                onClick=${function () {
                  if (item.id !== props.value) props.onChange(item.id);
                }}>
          ${item.button}
        </button>`;
    });
    return html`<div className="fm-switch fm-switch--medium" role="tablist"
                     aria-label="Released to air, water or both">
      <span className="fm-switch__lead">Released to</span>
      ${buttons}
    </div>`;
  }

  function MapFigure(props) {
    var payload = props.payload;
    var slots = useMemo(
      function () {
        return readSlots(payload.slots.facilities);
      },
      [payload],
    );
    var lauSlots = useMemo(
      function () {
        return readSlots(payload.slots.lau);
      },
      [payload],
    );

    var viewState = useState(payload.views[0].id);
    var viewId = viewState[0];
    var setViewId = viewState[1];
    var viewIndex = 0;
    for (var i = 0; i < payload.views.length; i += 1) {
      if (payload.views[i].id === viewId) viewIndex = i;
    }
    var view = payload.views[viewIndex];

    // The medium is the second axis of every view: air, water, or both. It is
    // kept next to the view so a switch keeps the zoom but re-measures every
    // dot, the legend, the counts and the cards.
    var mediumState = useState("combined");
    var mediumId = mediumState[0];
    var setMediumId = mediumState[1];
    var measure = view.measures[mediumId] || view.measures.combined;
    var media = payload.media || [];

    var zoomState = useState({ k: 1, x: 0, y: 0 });
    var zoom = zoomState[0];
    var setZoom = zoomState[1];

    var pinState = useState(null);
    var pinned = pinState[0];
    var setPinned = pinState[1];

    var focusState = useState(null);
    var focus = focusState[0];
    var setFocus = focusState[1];

    var resetState = useState(0);
    var resetToken = resetState[0];
    var setResetToken = resetState[1];

    // A new view or medium redefines every dot: release the pinned card
    // instead of leaving a reading about a value that is no longer drawn.
    useEffect(
      function () {
        setPinned(null);
      },
      [view.id, measure.id],
    );

    /*
     * Dot size: the log of the mass over the domain the payload measured for
     * this view and medium (a decade below the fifth percentile up to at most
     * 1 Mt, where the scale stops so the 100 t - 1 Mt band keeps its range).
     * A linear or square-root scale would collapse the eight decades of
     * reported mass into one dot size.
     */
    var radius = useMemo(
      function () {
        var size = measure.size || { minKg: 1, maxKg: 1e6 };
        var low = Math.log10(Math.max(Number(size.minKg) || 1, 1e-6));
        var high = Math.log10(
          Math.max(Number(size.maxKg) || 10, Math.pow(10, low + 1)),
        );
        var scale = d3
          .scaleLinear()
          .domain([low, high])
          .range([RADIUS_MIN, RADIUS_MAX])
          .clamp(true);
        return function (kilograms) {
          var value = Number(kilograms) || 0;
          return value > 0 ? scale(Math.log10(value)) : 0;
        };
      },
      [measure],
    );

    // How many facilities fall in each hazard class, in this view and medium.
    var levelCounts = useMemo(
      function () {
        var counts = payload.levels.map(function () {
          return 0;
        });
        payload.facilities.forEach(function (row) {
          if (measureMass(measure, slots, row) > 0) counts[row[slots.level]] += 1;
        });
        return counts;
      },
      [payload, slots, measure],
    );

    var entries = useMemo(
      function () {
        return buildEntries(payload, slots, lauSlots, props.countries);
      },
      [payload, slots, lauSlots, props.countries],
    );

    var lauShapeOf = useMemo(
      function () {
        var byId = new Map();
        var ids = (props.lau && props.lau.ids) || [];
        for (var i = 0; i < ids.length; i += 1) byId.set(ids[i], i);
        return byId;
      },
      [props.lau],
    );

    /** Frame a search hit: its bounding box, or a window around a facility. */
    function focusEntry(entry) {
      if (!entry) return;
      var box = null;
      if (entry.kind === SEARCH_COUNTRY) {
        var position = (props.countries.codes || []).indexOf(entry.code);
        if (position >= 0) {
          box = shapeBox(props.countries.shapes[position], props.countries.scale);
        }
      } else if (entry.kind === SEARCH_MUNICIPALITY) {
        var lauRow = payload.lau[entry.index];
        var shape = lauShapeOf.get(lauRow[lauSlots.id]);
        if (shape !== undefined) {
          box = shapeBox(props.lau.shapes[shape], props.lau.scale);
        }
      } else {
        // A facility hit frames a city-sized window: 12 km across leaves the
        // dot in a ring of its neighbours, and the reader can press the map and
        // scroll in from there (the deepest zoom shows 2.7 km).
        var row = payload.facilities[entry.index];
        box = [
          row[slots.x] - 6000,
          row[slots.y] - 6000,
          row[slots.x] + 6000,
          row[slots.y] + 6000,
        ];
      }
      if (!box) return;
      // A facility hit pins its own card on the dot; a municipality hit pins the
      // outline's card at the centre of the frame it just zoomed to.
      if (entry.kind === SEARCH_FACILITY) {
        setPinned({ kind: "facility", index: entry.index });
      } else if (entry.kind === SEARCH_MUNICIPALITY) {
        setPinned({
          kind: "lau",
          index: entry.index,
          at: { x: (box[0] + box[2]) / 2, y: (box[1] + box[3]) / 2 },
        });
      } else {
        setPinned(null);
      }
      setFocus(function (current) {
        return { token: (current ? current.token : 0) + 1, box: box };
      });
    }

    function resetView() {
      setZoom({ k: 1, x: 0, y: 0 });
      setPinned(null);
      setFocus(null);
      setResetToken(function (value) {
        return value + 1;
      });
    }

    var buttons = payload.views.map(function (item) {
      var active = item.id === view.id;
      return html`
        <button key=${item.id} type="button" role="tab"
                aria-selected=${active ? "true" : "false"}
                className=${"fm-switch__btn" + (active ? " is-active" : "")}
                onClick=${function () {
                  if (item.id !== view.id) setViewId(item.id);
                }}>
          ${item.button}
        </button>`;
    });

    var meta = payload.meta;
    var footer = [
      plural(meta.facilityCount, "facility", "facilities") +
        " in " +
        plural(meta.lauCount, "municipality", "municipalities") +
        " with at least one facility",
      "EPSG:3035 (LAEA Europe)",
      meta.source,
    ];
    if (meta.missingCoordinates) {
      footer.push(
        plural(meta.missingCoordinates, "facility", "facilities") +
          " without usable coordinates",
      );
    }
    if (meta.offMap) {
      footer.push(
        plural(meta.offMap, "facility", "facilities") +
          " outside the map window",
      );
    }
    if (meta.permitLinks) {
      footer.push(
        plural(meta.permitLinks, "facility", "facilities") +
          " with a permit link",
      );
    }

    return html`
      <figure className="fm-figure">
        <div className="fm-head">
          <div className="fm-head__text">
            <h3 className="fm-head__title">${view.heading} by location</h3>
            <p className="fm-head__sub">
              ${measure.subheading +
              " \u00b7 " +
              plural(measure.facilities, "facility", "facilities") +
              " on the map"}
            </p>
          </div>
          <div className="fm-switch" role="tablist" aria-label="Map view">
            ${buttons}
          </div>
        </div>
        <div className="fm-toolbar">
          <${SearchBox} entries=${entries} onSelect=${focusEntry}
            resetToken=${resetToken} />
          <${MediumSwitch} media=${media} value=${measure.id}
            onChange=${setMediumId} />
        </div>
        <div className="fm-body">
          <${MapPlot} payload=${payload} countries=${props.countries}
            lau=${props.lau} view=${view} measure=${measure} slots=${slots}
            lauSlots=${lauSlots} radius=${radius} zoom=${zoom} setZoom=${setZoom}
            pinned=${pinned} setPinned=${setPinned} focus=${focus}
            onReset=${resetView} />
          <${Legend} payload=${payload} view=${view} measure=${measure}
            radius=${radius} levelCounts=${levelCounts} />
        </div>
        <p className="fm-foot">
          ${footer.join(" \u00b7 ")}. Drag to pan and double-click to zoom in
          place, or press the map once to give it the scroll wheel - until then
          the page keeps its scroll - and zoom up to 2048x, where the window is
          2.7 km across and the scale bar follows the transform; the reset button
          (or 0) returns to the full window and hands the wheel back. Click a dot
          to pin its card: a pinned card can be read while the map moves, and its
          permit link opens the page the country published. Hover a dot for its
          air/water split, its strongest hazard class and the substances behind
          the total; hover a municipality outline for its total. The air / water
          buttons re-measure the same view, so the dots, the legend and the
          cards always describe the releases the toggle selects.
        </p>
      </figure>`;
  }

  function App() {
    var state = useMapData();

    if (state.status !== "ready") {
      var message =
        state.status === "error"
          ? "The map data could not be loaded (" +
            state.error +
            "). Serve the site folder over HTTP rather than opening the page from disk."
          : "Loading the facility map \u2026";
      return html`<div className="fm-status">${message}</div>`;
    }
    return html`<${MapFigure} payload=${state.payload}
      countries=${state.countries} lau=${state.lau} />`;
  }

  /* ------------------------------------------------------------------ mount */

  var mounted = false;

  function mount() {
    if (mounted || typeof React === "undefined" || typeof d3 === "undefined") {
      return false;
    }
    var root = document.getElementById(ROOT_ID);
    if (!root) return false;

    mounted = true;
    ReactDOM.createRoot(root).render(html`<${App} />`);
    return true;
  }

  // The container is in the page before this script runs, so the first attempt
  // usually succeeds; the timer only covers a lazily rendered cell output.
  if (!mount()) {
    document.addEventListener("DOMContentLoaded", mount);
    var attempts = 0;
    var timer = setInterval(function () {
      attempts += 1;
      if (mount() || attempts > 80) clearInterval(timer);
    }, 250);
  }
})();
