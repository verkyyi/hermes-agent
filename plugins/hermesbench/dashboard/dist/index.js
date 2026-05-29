(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useEffect, useState } = SDK.hooks;
  const { Badge, Card, CardContent, CardHeader, CardTitle, Button } = SDK.components;
  const { cn } = SDK.utils;

  const API = "/api/plugins/hermesbench";
  const h = React.createElement;

  // Fixed palette so series stay readable on any dashboard theme.
  const OVERALL_COLOR = "#58a6ff";
  const SUITE_COLOR = {
    responsiveness: "#3fb950",
    kanban_scale: "#58a6ff",
    orchestrator: "#a371f7",
    origin_return: "#f0883e",
  };
  const FALLBACK = "#8b949e";

  function fmt(v) { return v == null ? "—" : Number(v).toFixed(1); }
  function suiteId(s) { return s.id || s.suite_id; }
  function suiteScore(s) {
    if (!s || s.skipped || s.error) return null;
    return s.score;
  }

  // Generic multi-series line chart over a shared x-axis (run index).
  // props: { series:[{label,color,points:[{x,y}]}], xMax, height, legend }
  function LineChart(props) {
    const series = props.series || [];
    const xMax = Math.max(1, props.xMax || 1);
    const W = 800, H = props.height || 200, pad = 28;
    const x = function (i) { return pad + (W - 2 * pad) * (i / xMax); };
    const y = function (s) { return H - pad - (H - 2 * pad) * (s / 100); };
    const anyPts = series.some(function (s) { return s.points.length; });

    const kids = [];
    [0, 25, 50, 75, 100].forEach(function (v) {
      kids.push(h("line", {
        key: "g" + v, x1: pad, y1: y(v), x2: W - pad, y2: y(v),
        stroke: "currentColor", strokeOpacity: 0.12,
      }));
      kids.push(h("text", {
        key: "l" + v, x: 2, y: y(v) + 4, fontSize: 10,
        fill: "currentColor", fillOpacity: 0.5,
      }, String(v)));
    });
    series.forEach(function (ser, si) {
      const pts = ser.points.slice().sort(function (a, b) { return a.x - b.x; });
      if (pts.length > 1) {
        const d = pts.map(function (p, k) {
          return (k ? "L" : "M") + x(p.x).toFixed(1) + " " + y(p.y).toFixed(1);
        }).join(" ");
        kids.push(h("path", { key: "p" + si, d: d, fill: "none", stroke: ser.color, strokeWidth: 2 }));
      }
      pts.forEach(function (p, k) {
        kids.push(h("circle", {
          key: "c" + si + "_" + k, cx: x(p.x).toFixed(1), cy: y(p.y).toFixed(1),
          r: 3, fill: ser.color,
        }));
      });
    });

    if (!anyPts) {
      return h("div", { className: "text-sm text-muted-foreground py-8 text-center" }, "No data yet.");
    }
    const svg = h("svg", {
      viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none",
      className: "w-full", style: { height: H + "px" },
    }, kids);
    let legend = null;
    if (props.legend) {
      legend = h("div", { className: "flex flex-wrap gap-3 mb-1" }, series.map(function (s, i) {
        return h("span", { key: i, className: "inline-flex items-center gap-1.5 text-xs text-muted-foreground" },
          h("span", { style: { width: "10px", height: "10px", background: s.color, borderRadius: "2px", display: "inline-block" } }),
          s.label + (s.points.length ? "" : " (none)"));
      }));
    }
    return h("div", null, legend, svg);
  }

  function Table(props) {
    const runs = props.runs;
    const ids = props.suiteIds;
    const thStyle = "text-left font-medium text-muted-foreground px-2 py-1.5";
    const tdStyle = "px-2 py-1.5 border-t border-border tabular-nums";
    const head = h("tr", null,
      h("th", { className: thStyle }, "run"),
      h("th", { className: thStyle }, "overall"),
      ids.map(function (id) { return h("th", { key: id, className: thStyle }, id); })
    );
    const rows = runs.map(function (r) {
      const cells = ids.map(function (id) {
        const s = (r.suites || []).find(function (x) { return suiteId(x) === id; });
        if (!s) return h("td", { key: id, className: tdStyle }, "—");
        const txt = s.skipped ? "skip" : s.error ? "err" : fmt(s.score);
        const cls = s.skipped ? "text-muted-foreground"
          : (s.error || s.passed === false) ? "text-destructive"
          : s.passed === true ? "text-emerald-500" : "";
        return h("td", { key: id, className: cn(tdStyle, cls) }, txt);
      });
      const ovCls = r.passed ? "text-emerald-500" : "text-destructive";
      return h("tr", { key: r.run_id },
        h("td", { className: tdStyle }, (r.ts || "").replace("T", " ").slice(0, 16)),
        h("td", { className: cn(tdStyle, ovCls, "font-medium") }, fmt(r.overall_score)),
        cells
      );
    });
    return h("div", { className: "overflow-x-auto" },
      h("table", { className: "w-full text-sm border-collapse" },
        h("thead", null, head), h("tbody", null, rows)));
  }

  function HermesBenchPage() {
    const [runs, setRuns] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    function load() {
      setLoading(true); setError(null);
      fetch(API + "/trend?limit=60", { credentials: "include" })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d.error) setError(d.error); setRuns((d.runs || []).slice()); })
        .catch(function (e) { setError(String(e)); })
        .finally(function () { setLoading(false); });
    }
    useEffect(function () { load(); }, []);

    // store returns newest-first; charts plot oldest -> newest.
    const chrono = runs.slice().reverse();
    const xMax = Math.max(1, chrono.length - 1);

    const overallSeries = {
      label: "Overall", color: OVERALL_COLOR,
      points: chrono.map(function (r, i) { return { x: i, y: r.overall_score }; })
        .filter(function (p) { return p.y != null; }),
    };

    // Discover suites in first-seen order.
    const meta = []; const seen = {};
    chrono.forEach(function (r) {
      (r.suites || []).forEach(function (s) {
        const id = suiteId(s);
        if (id && !seen[id]) { seen[id] = true; meta.push({ id: id, category: s.category || id, mode: s.mode || "" }); }
      });
    });
    const suiteIds = meta.map(function (m) { return m.id; });

    function suiteSeries(id) {
      return {
        label: id, color: SUITE_COLOR[id] || FALLBACK,
        points: chrono.map(function (r, i) {
          const s = (r.suites || []).find(function (x) { return suiteId(x) === id; });
          return { x: i, y: suiteScore(s) };
        }).filter(function (p) { return p.y != null; }),
      };
    }
    function latestSuiteScore(id) {
      const r = runs[0];
      if (!r) return null;
      const s = (r.suites || []).find(function (x) { return suiteId(x) === id; });
      return suiteScore(s);
    }

    const latest = runs[0];

    return h("div", { className: "space-y-4 p-1" },
      h("div", { className: "flex items-center justify-between" },
        h("div", null,
          h("h2", { className: "text-lg font-semibold" }, "HermesBench"),
          h("p", { className: "text-sm text-muted-foreground" },
            "Consolidated daily benchmark — overall and per-category score trends (local profile).")
        ),
        h("div", { className: "flex items-center gap-2" },
          latest ? h(Badge, { variant: latest.passed ? "default" : "destructive" },
            "latest " + fmt(latest.overall_score)) : null,
          h(Button, { variant: "outline", size: "sm", disabled: loading, onClick: load },
            loading ? "Loading…" : "Refresh")
        )
      ),
      error ? h("div", {
        className: "rounded-md border border-destructive/40 text-destructive px-3 py-2 text-sm",
      }, "Could not load trend: " + error) : null,

      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-sm" }, "Overall score")),
        h(CardContent, null, h(LineChart, { series: [overallSeries], xMax: xMax, height: 220 }))
      ),

      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-sm" }, "Per-category trends")),
        h(CardContent, null,
          meta.length
            ? h("div", { className: "grid grid-cols-1 md:grid-cols-2 gap-4" }, meta.map(function (m) {
                const color = SUITE_COLOR[m.id] || FALLBACK;
                return h("div", { key: m.id, className: "rounded-md border border-border p-3" },
                  h("div", { className: "flex items-start justify-between mb-1" },
                    h("div", null,
                      h("div", { className: "text-sm font-medium" }, m.category),
                      h("div", { className: "text-xs text-muted-foreground" }, m.id + " · " + m.mode)),
                    h("span", { className: "text-xs tabular-nums", style: { color: color } },
                      fmt(latestSuiteScore(m.id)))
                  ),
                  h(LineChart, { series: [suiteSeries(m.id)], xMax: xMax, height: 130 })
                );
              }))
            : h("div", { className: "text-sm text-muted-foreground py-6 text-center" },
                loading ? "Loading…" : "No runs recorded yet.")
        )
      ),

      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-sm" }, "Recent runs")),
        h(CardContent, null,
          runs.length
            ? h(Table, { runs: runs, suiteIds: suiteIds })
            : h("div", { className: "text-sm text-muted-foreground py-6 text-center" },
                loading ? "Loading…" : "No runs recorded yet.")
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("hermesbench", HermesBenchPage);
})();
