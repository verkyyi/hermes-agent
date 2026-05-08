(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useEffect, useMemo, useState } = SDK.hooks;
  const {
    Badge,
    Button,
    Card,
    CardContent,
    CardHeader,
    CardTitle,
    Label,
    Select,
    SelectOption,
    Separator,
    Tabs,
    TabsList,
    TabsTrigger,
  } = SDK.components;
  const { cn } = SDK.utils;

  const API = "/api/plugins/profile-memory";

  function option(label, value) {
    return React.createElement(SelectOption, { key: value, value: value }, label);
  }

  function selectChangeHandler(setter) {
    return {
      onValueChange: function (v) { setter(v); },
      onChange: function (e) { setter(e && e.target ? e.target.value : e); },
    };
  }

  function Notice(props) {
    return React.createElement("div", {
      className: cn(
        "rounded-md border px-3 py-2 text-sm",
        props.tone === "danger" ? "border-destructive/40 text-destructive" : "border-border text-muted-foreground",
      ),
    }, props.children);
  }

  function ProfileMemoryPage() {
    const [profiles, setProfiles] = useState([]);
    const [profile, setProfile] = useState("default");
    const [files, setFiles] = useState([]);
    const [activeKey, setActiveKey] = useState("user");
    const [draft, setDraft] = useState("");
    const [dirty, setDirty] = useState(false);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");
    const [status, setStatus] = useState("");

    const activeFile = useMemo(function () {
      return files.find(function (f) { return f.key === activeKey; }) || files[0] || null;
    }, [files, activeKey]);

    function loadProfiles() {
      return SDK.fetchJSON(API + "/profiles")
        .then(function (data) {
          setProfiles(data.profiles || []);
        });
    }

    function loadFiles(nextProfile) {
      setLoading(true);
      setError("");
      setStatus("");
      return SDK.fetchJSON(API + "/files?profile=" + encodeURIComponent(nextProfile || profile))
        .then(function (data) {
          const nextFiles = data.files || [];
          setFiles(nextFiles);
          const selected = nextFiles.find(function (f) { return f.key === activeKey; }) || nextFiles[0];
          if (selected) {
            setActiveKey(selected.key);
            setDraft(selected.content || "");
          } else {
            setDraft("");
          }
          setDirty(false);
        })
        .catch(function (err) {
          setError(err && err.message ? err.message : "Failed to load memory files.");
        })
        .finally(function () { setLoading(false); });
    }

    useEffect(function () {
      loadProfiles().catch(function () { setProfiles([{ id: "default", label: "default" }]); });
    }, []);

    useEffect(function () {
      loadFiles(profile);
    }, [profile]);

    useEffect(function () {
      if (!activeFile) return;
      setDraft(activeFile.content || "");
      setDirty(false);
    }, [activeKey]);

    function save() {
      if (!activeFile || !activeFile.editable || !dirty) return;
      setSaving(true);
      setError("");
      setStatus("");
      SDK.fetchJSON(API + "/files/" + encodeURIComponent(profile) + "/" + encodeURIComponent(activeFile.key), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: draft }),
      }).then(function (data) {
        setStatus(data.backup && data.backup.created ? "Saved with timestamped backup." : "Saved.");
        return loadFiles(profile);
      }).catch(function (err) {
        setError(err && err.message ? err.message : "Save failed.");
      }).finally(function () { setSaving(false); });
    }

    const warnings = activeFile && activeFile.warnings ? activeFile.warnings : [];

    return React.createElement("div", { className: "flex flex-col gap-6" },
      React.createElement(Card, null,
        React.createElement(CardHeader, null,
          React.createElement("div", { className: "flex flex-wrap items-center justify-between gap-3" },
            React.createElement("div", null,
              React.createElement(CardTitle, { className: "flex items-center gap-2 text-lg" },
                "Profile Memory",
                React.createElement(Badge, { variant: "outline" }, "USER.md / MEMORY.md"),
              ),
              React.createElement("p", { className: "mt-1 text-sm text-muted-foreground" },
                "View and safely maintain profile-scoped memory files. Saves create timestamped local backups."
              ),
            ),
            React.createElement("div", { className: "flex min-w-[220px] flex-col gap-1" },
              React.createElement(Label, null, "Profile"),
              React.createElement(Select, Object.assign({ value: profile }, selectChangeHandler(function (value) { setProfile(value || "default"); })),
                profiles.map(function (p) { return option(p.label || p.id, p.id); }),
              ),
            ),
          ),
        ),
        React.createElement(CardContent, { className: "flex flex-col gap-4" },
          React.createElement(Notice, null,
            "Use USER.md for stable user facts and MEMORY.md for durable environment/project facts. Keep procedures in skills/docs, and never store secrets or temporary task progress here. SOUL.md is shown read-only in this MVP."
          ),
          error ? React.createElement(Notice, { tone: "danger" }, error) : null,
          status ? React.createElement(Notice, null, status) : null,
        ),
      ),

      React.createElement(Card, null,
        React.createElement(CardContent, { className: "flex flex-col gap-4 pt-6" },
          loading ? React.createElement("p", { className: "text-sm text-muted-foreground" }, "Loading memory files...") : null,
          !loading && files.length ? React.createElement(Tabs, { value: activeKey, onValueChange: setActiveKey },
            React.createElement(TabsList, null,
              files.map(function (f) {
                return React.createElement(TabsTrigger, { key: f.key, value: f.key }, f.label);
              }),
            ),
          ) : null,
          activeFile ? React.createElement("div", { className: "flex flex-col gap-3" },
            React.createElement("div", { className: "flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground" },
              React.createElement("span", null, activeFile.description),
              React.createElement("span", { className: "font-mono" }, activeFile.relative_path),
            ),
            warnings.length ? React.createElement(Notice, { tone: "danger" }, warnings.join(" ")) : null,
            React.createElement("textarea", {
              className: cn(
                "min-h-[520px] w-full rounded-md border border-border bg-background/50 p-3 font-mono text-sm outline-none",
                "focus:border-ring disabled:cursor-not-allowed disabled:opacity-70",
              ),
              value: draft,
              disabled: !activeFile.editable,
              spellCheck: false,
              onChange: function (e) { setDraft(e.target.value); setDirty(true); },
            }),
            React.createElement(Separator, null),
            React.createElement("div", { className: "flex flex-wrap items-center justify-between gap-3" },
              React.createElement("div", { className: "text-xs text-muted-foreground" },
                activeFile.updated_at ? "Last updated: " + activeFile.updated_at : "File does not exist yet.",
                " · ", activeFile.size || 0, " bytes",
              ),
              React.createElement("div", { className: "flex gap-2" },
                React.createElement(Button, {
                  variant: "outline",
                  disabled: loading || saving,
                  onClick: function () { loadFiles(profile); },
                }, "Reload"),
                React.createElement(Button, {
                  disabled: !activeFile.editable || !dirty || saving,
                  onClick: save,
                }, saving ? "Saving..." : activeFile.editable ? "Save with backup" : "Read-only"),
              ),
            ),
          ) : null,
        ),
      ),
    );
  }

  window.__HERMES_PLUGINS__.register("profile-memory", ProfileMemoryPage);
})();
