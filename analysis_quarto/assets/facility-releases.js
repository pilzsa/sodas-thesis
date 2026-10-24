/* Release rankings - React for the DOM, D3 for the scales.
 *
 * One stacked-bar ranking at a time. Three views (releases excluding CO2,
 * hazardous mass, carcinogenic mass) share one row rhythm, one label column
 * and one colour per stack slot, and switch through a segmented control.
 * A second segmented control switches the grouping, i.e. what the rows are:
 * facilities, E-PRTR sectors or IED Annex I activities. A payload with one
 * grouping renders just the view switch; a payload with several renders both.
 * React renders the SVG (so resize, hover and the switches are plain state
 * changes), while D3 supplies the scales, ticks and number formats. No build
 * step: React, ReactDOM, htm and d3 are loaded as globals by
 * facility-chart-scripts.html.
 *
 * Data contract: JSON from facility_chart_data.build_payload() or
 * group_chart_data.build_payload(), injected by the notebook cell into a
 * <script type="application/json"> inside the figure's [data-fr-root] div. One
 * payload can carry several groupings; each grouping lists its own views plus
 * the noun ("facility", "sector", "activity") that the hover box and the footer
 * use, and a footnote about its row set. Masses arrive in kilograms; each view
 * carries the axis unit so every row shares one scale, and the substance table
 * carries the classification that the hover box reports.
 */

(function () {
  "use strict";

  var html = htm.bind(React.createElement);
  var useState = React.useState;
  var useEffect = React.useEffect;
  var useRef = React.useRef;
  var useMemo = React.useMemo;

  /* EPRTR Atlas design tokens, so the figure sits in the same palette as the
   * map dashboard (ink, hairlines, teal brand). */
  var INK = "#17212c";
  var MUTED = "#536273";
  var GRID = "#d7dfe8";
  var ROW_TINT = "#f5f8fb";
  var RULE = "#7b8796";
  /* The panel the bars are drawn on: a slice boundary is a 1px gap in that
     colour, because adjacent fills sit 15 dE apart and the lightest one is
     2.5:1 against white - the gap, not the fill, says where a slice ends. */
  var SURFACE = "#ffffff";
  var FONT =
    '"Source Sans Pro", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

  var OTHER_FULL = "Other pollutants (too small to stack on their own)";

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
    var units = [[1e9, "Mt"], [1e6, "kt"], [1e3, "t"]];
    for (var i = 0; i < units.length; i += 1) {
      if (Math.abs(value) >= units[i][0]) {
        var scaled = value / units[i][0];
        return d3.format(",." + decimalsFor(scaled) + "f")(scaled) + " " + units[i][1];
      }
    }
    return d3.format(",." + decimalsFor(value) + "f")(value) + " kg";
  }

  function percent(share) {
    if (share >= 0.9995) return "100%";
    if (share >= 0.1) return Math.round(share * 100) + "%";
    if (share >= 0.01) return (share * 100).toFixed(1) + "%";
    return "<1%";
  }

  /* ----------------------------------------------------------- text metrics */

  var measure = document.createElement("canvas").getContext("2d");

  function textWidth(text, size, weight) {
    measure.font = (weight || 400) + " " + size + "px " + FONT;
    return measure.measureText(text).width;
  }

  /** Shorten to fit the label column, keeping the ellipsis inside the budget. */
  function clip(text, maxWidth, size, weight) {
    if (maxWidth <= 0) return "";
    if (textWidth(text, size, weight) <= maxWidth) return text;
    var low = 0;
    var high = text.length;
    while (low < high) {
      var mid = Math.ceil((low + high) / 2);
      if (textWidth(text.slice(0, mid) + "\u2026", size, weight) <= maxWidth) {
        low = mid;
      } else {
        high = mid - 1;
      }
    }
    return text.slice(0, Math.max(1, low)).trimEnd() + "\u2026";
  }

  /* ------------------------------------------------------------ measurements */

  /** Track the rendered width of an element so the SVG can be redrawn on resize. */
  function useElementWidth(ref) {
    var state = useState(0);
    var width = state[0];
    var setWidth = state[1];

    useEffect(function () {
      var node = ref.current;
      if (!node) return undefined;
      function update() {
        setWidth(node.getBoundingClientRect().width);
      }
      update();
      if (typeof ResizeObserver === "undefined") return undefined;
      var observer = new ResizeObserver(update);
      observer.observe(node);
      return function () {
        observer.disconnect();
      };
    }, [ref]);

    return width;
  }

  /* ----------------------------------------------------------------- tooltip */

  function TipRow(props) {
    var segment = props.segment;
    return html`
      <div className=${"fr-tip__row" + (props.focus ? " is-focus" : "")}>
        <span className="fr-tip__swatch" style=${{ background: segment.color }}></span>
        <span className="fr-tip__label">${segment.label}</span>
        <span className="fr-tip__value">${mass(segment.value)}</span>
        <span className="fr-tip__share">${percent(segment.share)}</span>
      </div>`;
  }

  /** One classification badge (IARC group, CLP class, SVHC, WFD ...). */
  function Tag(props) {
    var tag = props.tag;
    return html`
      <span className=${"fr-tag fr-tag--" + tag.kind} title=${tag.title || ""}>
        ${tag.text}
      </span>`;
  }

  /** Classification block for the segment under the pointer. */
  function Classification(props) {
    var segment = props.segment;
    var view = props.view;
    var data = props.data;
    var meta =
      segment.full === OTHER_FULL && view.pool
        ? view.pool
        : data.substances[segment.full];

    if (!meta) {
      return html`
        <div className="fr-tip__class">
          <p className="fr-tip__note">No classification recorded for this substance.</p>
        </div>`;
    }

    var tags = meta.tags || [];
    return html`
      <div className="fr-tip__class">
        <div className="fr-tip__class-head">
          <span className="fr-tip__swatch" style=${{ background: segment.color }}></span>
          <span>${segment.full === OTHER_FULL ? OTHER_FULL : segment.full}</span>
        </div>
        <div className="fr-tip__tags">
          ${tags.length
            ? tags.map(function (tag) {
                return html`<${Tag} key=${tag.kind + tag.text} tag=${tag} />`;
              })
            : html`<span className="fr-tag fr-tag--none">Not classified</span>`}
        </div>
        <p className="fr-tip__note">${meta.note}</p>
        ${meta.examples && meta.examples.length
          ? html`<p className="fr-tip__note fr-tip__note--quiet">Largest in this bucket: ${meta.examples.join(", ")}</p>`
          : null}
      </div>`;
  }

  function Tooltip(props) {
    var view = props.view;
    var row = props.row;
    var focus = props.focus;
    var segment = focus >= 0 ? row.segments[focus] : null;
    var multiple = row.total > 0 ? row.co2 / row.total : 0;

    return html`
      <div className="fr-tip" style=${{ left: props.left + "px", top: props.top + "px" }}>
        <div className="fr-tip__name">${row.name}</div>
        <div className="fr-tip__place">${row.place}</div>
        <div className="fr-tip__total">
          <strong>${mass(row.total)}</strong>
          <span>in this view</span>
        </div>
        <div className="fr-tip__body">
          ${row.segments.map(function (item, index) {
            return html`
              <${TipRow} key=${item.full} segment=${item} focus=${index === focus} />`;
          })}
        </div>
        ${segment
          ? html`<${Classification} segment=${segment} view=${view} data=${props.data} />`
          : html`<div className="fr-tip__hint">
              Point at a segment to see which classifications it carries.
            </div>`}
        <div className="fr-tip__co2">
          ${row.co2 > 0
            ? html`<span>CO<sub>2</sub>, left out of this view: <strong>${mass(row.co2)}</strong></span>${multiple >= 2
                ? html`<span className="fr-tip__aside">${d3.format(",.0f")(multiple)}\u00d7 this bar</span>`
                : null}`
            : html`<span>This ${props.noun || "facility"} reported no CO<sub>2</sub>.</span>`}
        </div>
      </div>`;
  }


  /* ------------------------------------------------------------------- chart */

  var ROW_H = 22;
  var BAR_H = 11;
  var TIP_WIDTH = 320;
  var TIP_HEIGHT = 300;

  function Chart(props) {
    var view = props.view;
    var data = props.data;
    var labelWidth = props.labelWidth;
    var highlight = props.highlight;
    var viewKey = props.viewKey || view.id;

    var plotRef = useRef(null);
    var measured = useElementWidth(plotRef);
    var hoverState = useState(null);
    var hover = hoverState[0];
    var setHover = hoverState[1];

    // A new ranking - another view, or the same view at another grouping -
    // brings new rows: drop the tooltip instead of leaving it on a bar that no
    // longer exists.
    useEffect(function () {
      setHover(null);
    }, [viewKey]);

    var width = measured || 320;
    var topPad = 6;
    var rightPad = 64;
    var plotBottom = topPad + view.rows.length * ROW_H;
    var height = plotBottom + 38;

    var plotWidth = Math.max(60, width - labelWidth - rightPad);
    var maxUnits =
      d3.max(view.rows, function (row) {
        return row.total;
      }) / view.scale || 1;
    var x = d3.scaleLinear().domain([0, maxUnits]).nice(4).range([0, plotWidth]);
    var ticks = x.ticks(5);
    var tickLabel = d3.format("~g");
    var valueLabel = d3.format(",." + view.decimals + "f");

    function focusAt(row, localX) {
      var cursor = 0;
      for (var index = 0; index < row.segments.length; index += 1) {
        var start = x(cursor / view.scale);
        cursor += row.segments[index].value;
        var end = x(cursor / view.scale);
        // Half a pixel of slack, so a segment drawn at the minimum width still
        // answers to the pointer; slivers stay unhoverable rather than
        // borrowing the classification of the neighbour.
        if (localX >= labelWidth + start - 0.5 && localX <= labelWidth + end + 0.5) {
          return index;
        }
      }
      return -1;
    }

    function move(event, row, index) {
      var box = plotRef.current.getBoundingClientRect();
      var localX = event.clientX - box.left;
      setHover({
        index: index,
        focus: focusAt(row, localX),
        x: localX,
        y: event.clientY - box.top,
      });
    }

    var band = hover
      ? html`<rect x="0" y=${topPad + hover.index * ROW_H} width=${width}
                   height=${ROW_H} rx="2" fill=${ROW_TINT}></rect>`
      : null;

    var grid = ticks.map(function (tick) {
      var tickX = labelWidth + x(tick);
      return html`
        <g key=${"tick-" + tick}>
          <line x1=${tickX} x2=${tickX} y1=${topPad} y2=${plotBottom}
                stroke=${tick === 0 ? RULE : GRID} strokeWidth="1"></line>
          <text x=${tickX} y=${plotBottom + 16} textAnchor="middle"
                fontSize="10.5" fill=${MUTED}>${tickLabel(tick)}</text>
        </g>`;
    });

    var rows = view.rows.map(function (row, index) {
      var y = topPad + index * ROW_H;
      var barY = y + (ROW_H - BAR_H) / 2;
      var cursor = 0;

      var segments = row.segments.map(function (segment) {
        var start = x(cursor / view.scale);
        cursor += segment.value;
        var end = x(cursor / view.scale);
        var faded = highlight && highlight !== segment.full;
        return html`
          <rect key=${row.id + "-" + segment.full}
                x=${labelWidth + start} y=${barY}
                width=${Math.max(end - start, 0.8)} height=${BAR_H}
                fill=${segment.color} opacity=${faded ? 0.2 : 1}
                stroke=${SURFACE} strokeWidth="1"
                shapeRendering="crispEdges"></rect>`;
      });

      var totalLabel = valueLabel(row.total / view.scale);
      var endX = labelWidth + x(row.total / view.scale);
      var fits = endX + 6 + textWidth(totalLabel, 10.5, 600) <= width - 2;
      var dimLabel = hover && hover.index !== index ? 0.45 : 1;

      return html`
        <g key=${row.id}>
          ${segments}
          <text x=${labelWidth - 12} y=${y + ROW_H / 2} textAnchor="end"
                dominantBaseline="middle" fontSize="11" fill=${INK}
                opacity=${dimLabel}>${clip(row.label, labelWidth - 26, 11, 400)}</text>
          <text x=${fits ? endX + 6 : width - 2} y=${y + ROW_H / 2}
                textAnchor=${fits ? "start" : "end"} dominantBaseline="middle"
                fontSize="10.5" fontWeight="600" fill=${MUTED}>${totalLabel}</text>
          <rect x="0" y=${y} width=${width} height=${ROW_H} fill="transparent"
                pointerEvents="all"
                onMouseMove=${function (event) { move(event, row, index); }}
                onMouseLeave=${function () { setHover(null); }}></rect>
        </g>`;
    });

    var tooltip = null;
    if (hover) {
      var tipLeft = Math.min(
        Math.max(hover.x + 16, 0),
        Math.max(width - TIP_WIDTH - 6, 0),
      );
      var tipTop = hover.y + 14;
      if (tipTop + TIP_HEIGHT > height) tipTop = Math.max(0, hover.y - TIP_HEIGHT);
      tooltip = html`
        <${Tooltip} view=${view} data=${data} row=${view.rows[hover.index]}
          focus=${hover.focus} left=${tipLeft} top=${tipTop}
          noun=${props.noun} />`;
    }

    return html`
      <div className="fr-plot" ref=${plotRef}>
        <svg width=${width} height=${height} role="img"
             aria-label=${view.heading + ": " + view.axisTitle}>
          <g key=${viewKey} className="fr-swap">
            ${band}${grid}${rows}
            <text x=${labelWidth} y=${height - 4} fontSize="10.5" fill=${MUTED}>
              ${view.axisTitle}
            </text>
          </g>
        </svg>
        ${tooltip}
      </div>`;
  }


  /* --------------------------------------------------------------------- app */

  function App(props) {
    var data = props.data;
    var groups = data.groups;
    var figureRef = useRef(null);
    var figureWidth = useElementWidth(figureRef);
    var groupState = useState(groups[0].id);
    var groupId = groupState[0];
    var setGroupId = groupState[1];
    var viewState = useState(groups[0].views[0].id);
    var viewId = viewState[0];
    var setViewId = viewState[1];
    var highlightState = useState(null);
    var highlight = highlightState[0];
    var setHighlight = highlightState[1];

    var group = useMemo(
      function () {
        var match = groups.filter(function (item) {
          return item.id === groupId;
        });
        return match.length ? match[0] : groups[0];
      },
      [groups, groupId],
    );

    // The view ids are shared by every grouping, so a reader who switches the
    // grouping keeps the ranking (releases, hazardous, carcinogenic) on screen.
    var view = useMemo(
      function () {
        var match = group.views.filter(function (item) {
          return item.id === viewId;
        });
        return match.length ? match[0] : group.views[0];
      },
      [group, viewId],
    );

    var viewKey = group.id + "|" + view.id;
    var noun = group.noun || "facility";

    // One label column for every view, so the bars start on the same line
    // whichever ranking is on screen.
    var labelWidth = useMemo(
      function () {
        return Math.round(Math.min(300, Math.max(140, figureWidth * 0.24)));
      },
      [figureWidth],
    );

    var groupButtons =
      groups.length > 1
        ? html`
            <div className="fr-switch fr-switch--group" role="tablist"
                 aria-label=${group.switchLabel || "Ranked by"}>
              <span className="fr-switch__lead">
                ${group.switchLabel || "Ranked by"}
              </span>
              ${groups.map(function (item) {
                var active = item.id === group.id;
                return html`
                  <button key=${item.id} type="button" role="tab"
                          aria-selected=${active ? "true" : "false"}
                          className=${"fr-switch__btn" + (active ? " is-active" : "")}
                          onClick=${function () {
                            if (item.id === group.id) return;
                            setGroupId(item.id);
                            setHighlight(null);
                          }}>
                    ${item.button}
                  </button>`;
              })}
            </div>`
        : null;

    var buttons = group.views.map(function (item) {
      var active = item.id === view.id;
      return html`
        <button key=${item.id} type="button" role="tab"
                aria-selected=${active ? "true" : "false"}
                className=${"fr-switch__btn" + (active ? " is-active" : "")}
                onClick=${function () {
                  if (item.id === view.id) return;
                  setViewId(item.id);
                  setHighlight(null);
                }}>
          ${item.button}
        </button>`;
    });

    var chips = view.legend.map(function (item) {
      var active = !highlight || highlight === item.full;
      return html`
        <li key=${item.full} className="fr-legend__item"
            title=${item.full + " \u2014 " + item.classification}
            onMouseEnter=${function () { setHighlight(item.full); }}
            onMouseLeave=${function () { setHighlight(null); }}
            style=${{ opacity: active ? 1 : 0.4 }}>
          <span className="fr-legend__swatch" style=${{ background: item.color }}></span>
          <span>${item.label}</span>
        </li>`;
    });

    var co2 = data.meta.co2;
    var footer =
      "CO\u2082 is left out of all three views: " +
      mass(co2.total) +
      " (" +
      Math.round(co2.shareOfAllMass * 100) +
      "% of all reported mass, " +
      d3.format(",.0f")(co2.ratio) +
      "\u00d7 every other pollutant combined). Hover a bar for the CO\u2082 " +
      "this " +
      noun +
      " reported and for the classification behind each substance." +
      (group.footnote ? " " + group.footnote : "");

    return html`
      <figure className="fr-figure" ref=${figureRef}>
        <div className="fr-head">
          <div className="fr-head__text">
            <h3 className="fr-head__title">${view.heading}</h3>
            <p className="fr-head__sub">${view.subheading}</p>
          </div>
          <div className="fr-controls">
            ${groupButtons}
            <div className="fr-switch" role="tablist" aria-label="Ranking view">
              ${buttons}
            </div>
          </div>
        </div>
        <ul className="fr-legend" aria-label="Substances in this view">${chips}</ul>
        <${Chart} view=${view} viewKey=${viewKey} data=${data} noun=${noun}
          labelWidth=${labelWidth} highlight=${highlight} />
        <p className="fr-foot">${footer}</p>
      </figure>`;
  }

  /* ------------------------------------------------------------------- mount */

  var mounted = new WeakSet();

  /** Render every figure that carries a JSON payload but no React tree yet. */
  function mountAll() {
    if (typeof React === "undefined" || typeof d3 === "undefined") return 0;

    var roots = document.querySelectorAll("[data-fr-root]");
    var count = 0;
    for (var index = 0; index < roots.length; index += 1) {
      var root = roots[index];
      if (mounted.has(root)) {
        count += 1;
        continue;
      }
      var payload = root.querySelector('script[type="application/json"]');
      if (!payload) continue;

      mounted.add(root);
      var data = JSON.parse(payload.textContent);
      payload.remove();
      ReactDOM.createRoot(root).render(html`<${App} data=${data} />`);
      count += 1;
    }
    return count;
  }

  // The notebook emits the containers and their JSON before this script runs,
  // but a cell may render late, and page 4 carries two figures. So: mount what
  // is there, then poll until the count of mounted roots has been stable for a
  // while (a new figure resets the countdown).
  var mountedCount = mountAll();
  var idle = 0;
  var timer = setInterval(function () {
    var count = mountAll();
    if (count !== mountedCount) {
      mountedCount = count;
      idle = 0;
    } else {
      idle += 1;
    }
    if (idle > 40) clearInterval(timer); // ~10 s without a new figure
  }, 250);
  document.addEventListener("DOMContentLoaded", mountAll);
})();

