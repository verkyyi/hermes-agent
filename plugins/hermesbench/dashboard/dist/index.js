(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useEffect, useState } = SDK.hooks;
  const { Badge, Card, CardContent, CardHeader, CardTitle, Button } = SDK.components;
  const { cn } = SDK.utils;

  const API = "/api/plugins/hermesbench";
  const h = React.createElement;

  function fmtScore(v) {
    return v == null ? "—" : Number(v).toFixed(1);
  }

  function suiteCellClass(s) {
    if (s.skipped) return "text-muted-foreground";
    if (s.error) return "text-destructive";
    if (s.passed === false) return "text-destructive";
    if (s.passed === true) return "text-emerald-500";
    return "";
  }

  function suiteText(s) {
    if (s.skipped) return "skip";
    if (s.error) return "err";
    return fmtScore(s.score);
  }

  // SVG line chart of overall_score over runs (oldest -> newest, left -> right).
  function Chart(props) {
    const runs = props.runs;
    const W = 800, H = 220, pad = 28;
    const pts = runs
      .map(function (r, i) { return { i: i, s: r.overall_score }; })
      .filter(function (p) { return p.s != null; });
    if (!pts.length) {
      return h("div", { className: "text-sm text-muted-foreground py-10 text-center" },
        "No scored runs yet — run: python -m evals.hermesbench.run");
    }
    const n = Math.max(1, pts.length - 1);
    const x = function (i) { return pad + (W - 2 * pad) * (i / n); };
    const y = function (s) { return H - pad - (H - 2 * pad) * (s / 100); };
    const path = pts.map(function (p, k) {
      return (k ? "L" : "M") + x(p.i).toFixed(1) + " " + y(p.s).toFixed(1);
    }).join(" ");
    const gridVals = [0, 25, 50, 75, 100];
    const children = [];
    gridVals.forEach(function (v) {
      children.push(h("line", {
        key: "g" + v, x1: pad, y1: y(v), x2: W - pad, y2: y(v),
        stroke: "currentColor", strokeOpacity: 0.12,
      }));
      children.push(h("text", {
        key: "t" + v, x: 2, y: y(v) + 4, fontSize: 10, fill: "currentColor", fillOpacity: 0.5,
      }, String(v)));
    });
    children.push(h("path", {
      key: "line", d: path, fill: "none", stroke: "currentColor",
      strokeWidth: 2, className: "text-primary",
    }));
    pts.forEach(function (p) {
      children.push(h("circle", {
        key: "c" + p.i, cx: x(p.i).toFixed(1), cy: y(p.s).toFixed(1), r: 3,
        fill: "currentColor", className: "text-primary",
      }));
    });
    return h("svg", {
      viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none",
      className: "w-full", style: { height: "220px" },
    }, children);
  }

  function Table(props) {
    const runs = props.runs;
    const suiteIds = [];
    runs.forEach(function (r) {
      (r.suites || []).forEach(function (s) {
        const id = s.id || s.suite_id;
        if (id && suiteIds.indexOf(id) === -1) suiteIds.push(id);
      });
    });
    const thStyle = "text-left font-medium text-muted-foreground px-2 py-1.5";
    const tdStyle = "px-2 py-1.5 border-t border-border tabular-nums";
    const head = h("tr", null,
      h("th", { className: thStyle }, "run"),
      h("th", { className: thStyle }, "tier"),
      h("th", { className: thStyle }, "overall"),
      suiteIds.map(function (id) { return h("th", { key: id, className: thStyle }, id); })
    );
    const rows = runs.map(function (r) {
      const cells = suiteIds.map(function (id) {
        const s = (r.suites || []).find(function (x) { return (x.id || x.suite_id) === id; });
        if (!s) return h("td", { key: id, className: tdStyle }, "—");
        return h("td", { key: id, className: cn(tdStyle, suiteCellClass(s)) }, suiteText(s));
      });
      const overallCls = r.passed ? "text-emerald-500" : "text-destructive";
      return h("tr", { key: r.run_id },
        h("td", { className: tdStyle }, (r.ts || "").replace("T", " ").slice(0, 16)),
        h("td", { className: tdStyle }, r.tier),
        h("td", { className: cn(tdStyle, overallCls, "font-medium") }, fmtScore(r.overall_score)),
        cells
      );
    });
    return h("div", { className: "overflow-x-auto" },
      h("table", { className: "w-full text-sm border-collapse" },
        h("thead", null, head),
        h("tbody", null, rows)
      )
    );
  }

  function HermesBenchPage() {
    const [runs, setRuns] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    function load() {
      setLoading(true);
      setError(null);
      fetch(API + "/trend?limit=30", { credentials: "include" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.error) setError(d.error);
          setRuns((d.runs || []).slice());
        })
        .catch(function (e) { setError(String(e)); })
        .finally(function () { setLoading(false); });
    }
    useEffect(function () { load(); }, []);

    // store returns newest-first; chart wants oldest-first, table keeps newest-first.
    const chartRuns = runs.slice().reverse();
    const latest = runs[0];

    return h("div", { className: "space-y-4 p-1" },
      h("div", { className: "flex items-center justify-between" },
        h("div", null,
          h("h2", { className: "text-lg font-semibold" }, "HermesBench"),
          h("p", { className: "text-sm text-muted-foreground" },
            "Consolidated daily benchmark — overall score over time (local profile).")
        ),
        h("div", { className: "flex items-center gap-2" },
          latest ? h(Badge, { variant: latest.passed ? "default" : "destructive" },
            "latest " + fmtScore(latest.overall_score)) : null,
          h(Button, { variant: "outline", size: "sm", disabled: loading, onClick: load },
            loading ? "Loading…" : "Refresh")
        )
      ),
      error ? h("div", {
        className: "rounded-md border border-destructive/40 text-destructive px-3 py-2 text-sm",
      }, "Could not load trend: " + error) : null,
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-sm" }, "Overall score")),
        h(CardContent, null, h(Chart, { runs: chartRuns }))
      ),
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-sm" }, "Recent runs")),
        h(CardContent, null,
          runs.length
            ? h(Table, { runs: runs })
            : h("div", { className: "text-sm text-muted-foreground py-6 text-center" },
                loading ? "Loading…" : "No runs recorded yet.")
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("hermesbench", HermesBenchPage);
})();
