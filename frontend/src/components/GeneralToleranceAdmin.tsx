/**
 * General Tolerance Settings (Inspection report toolbar, next to Instrument Settings): organisation settings, teach-once rules (list / edit / disable /
 * delete / history / CSV-Excel import-export), preview on a drawing, and the loaded standards (read-only).
 */
import { useEffect, useRef, useState } from "react";
import { api, type GtOrgSettings, type GtRule, type GtStandards } from "../api";
import "./general-tolerance.css";

const SCOPES: [GtRule["scope"], string][] = [["drawing", "Drawing"], ["family", "Part family"], ["customer", "Customer / organisation"]];

const EMPTY_RULE: Partial<GtRule> = {
  scope: "customer", scope_key: "", category: "radius", process: null, hole_type: null,
  size_min: null, size_max: null, unit: "mm", upper: 0.3, lower: -0.3, name: "", active: true,
};

function n(v: string): number | null {
  const s = v.trim().replace(",", ".");
  if (!s) return null;
  const x = Number(s);
  return Number.isFinite(x) ? x : null;
}

function tolText(r: GtRule): string {
  const suf = r.unit === "%" ? "%" : r.unit === "deg" ? "°" : "";
  if (Math.abs(r.upper + r.lower) < 1e-9) return `±${r.upper}${suf}`;
  return `${r.upper >= 0 ? "+" : ""}${r.upper}${suf} / ${r.lower >= 0 ? "+" : ""}${r.lower}${suf}`;
}

function rangeText(r: GtRule): string {
  if (r.size_min == null && r.size_max == null) return "any size";
  return `${r.size_min != null ? `>${r.size_min}` : ""}${r.size_min != null && r.size_max != null ? " – " : ""}${r.size_max != null ? `≤${r.size_max}` : ""}`;
}

/** Dialog opened from the Inspection report toolbar (next to Instrument Settings). */
export function GeneralToleranceSettingsModal({ jobId, onClose }: { jobId?: string; onClose: () => void }) {
  return (
    <div className="instr-settings-backdrop" onClick={onClose}>
      <div className="instr-settings-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="General tolerance settings">
        <div className="instr-settings-head">
          <div>
            <h2>General Tolerance Settings</h2>
            <p className="muted">Standards, defaults and taught rules for open dimensions. Closing re-calculates this drawing.</p>
          </div>
          <button className="btn btn-ghost btn-sm" style={{ width: "auto" }} onClick={onClose}>
            Close
          </button>
        </div>
        <GeneralToleranceAdmin jobId={jobId} />
      </div>
    </div>
  );
}

export default function GeneralToleranceAdmin({ jobId }: { jobId?: string } = {}) {
  const [settings, setSettings] = useState<GtOrgSettings | null>(null);
  const [canEdit, setCanEdit] = useState(false);
  const [rules, setRules] = useState<GtRule[]>([]);
  const [std, setStd] = useState<GtStandards | null>(null);
  const [draft, setDraft] = useState<Partial<GtRule>>(EMPTY_RULE);
  const [editing, setEditing] = useState<string | null>(null);
  const [history, setHistory] = useState<{ id: string; rows: { action: string; changed_by: string; changed_at: string }[] } | null>(null);
  const [previewJob, setPreviewJob] = useState(jobId || "");
  const [preview, setPreview] = useState<string[] | null>(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    try {
      const [s, r, st] = await Promise.all([api.gtSettings(), api.gtRules(), api.gtStandards()]);
      setSettings(s.settings);
      setCanEdit(s.can_edit);
      setRules(r.rules);
      setStd(st);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Could not load general-tolerance settings");
    }
  };
  useEffect(() => {
    void load();
  }, []);

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setErr("");
    setMsg("");
    try {
      await fn();
      setMsg(ok);
      await load();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed");
    }
  };

  const saveDraft = () =>
    run(async () => {
      if (editing) await api.gtUpdateRule(editing, draft);
      else await api.gtCreateRule(draft);
      setDraft(EMPTY_RULE);
      setEditing(null);
    }, editing ? "Rule updated." : "Rule created.");

  const doPreview = async (what: "rule" | "settings") => {
    setErr("");
    setPreview(null);
    if (!previewJob.trim()) {
      setErr("Enter a job ID (shown in the Inspection page URL) to preview.");
      return;
    }
    try {
      const res = await api.gtPreview(previewJob.trim(), what === "rule" ? { rule: draft } : { settings: settings! });
      setPreview(
        res.changes.map(
          (c) =>
            `Balloon ${c.balloon} (${c.nominal}): ${c.before.tol_high || "–"}/${c.before.tol_low || "–"} [${c.before.source || "none"}] → ` +
            `${c.after.tol_high || "–"}/${c.after.tol_low || "–"} [${c.after.source || "none"}]`,
        ),
      );
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Preview failed");
    }
  };

  const cats = std?.categories || [];
  const procs = std?.processes || {};
  const setD = (patch: Partial<GtRule>) => setDraft((d) => ({ ...d, ...patch }));

  return (
    <div>
      <p className="muted">
        Open dimensions (no tolerance printed) get the general tolerance the drawing states — ISO 2768 / IS 2102 or TVS
        TSDS0407 with its process — or a rule taught by an inspector. A drawing's own standard always wins over
        part-family and customer rules.
      </p>
      {err && <p className="error">{err}</p>}
      {msg && <p className="muted">{msg}</p>}

      <h3>Organisation settings</h3>
      {settings && (
        <div className="gt-admin-grid">
          <label className="gt-check">
            <input type="checkbox" checked={settings.enabled} disabled={!canEdit}
              onChange={(e) => setSettings({ ...settings, enabled: e.target.checked })} />
            Apply general tolerances automatically
          </label>
          <label>
            Default standard (drawing states none)
            <select value={settings.default_standard} disabled={!canEdit}
              onChange={(e) => setSettings({ ...settings, default_standard: e.target.value as GtOrgSettings["default_standard"] })}>
              <option value="">None — leave blank and flag</option>
              <option value="ISO2768">ISO 2768 / IS 2102</option>
              <option value="TSDS0407">TVS TSDS0407</option>
            </select>
          </label>
          <label>
            Default ISO class
            <select value={settings.default_iso_class} disabled={!canEdit}
              onChange={(e) => setSettings({ ...settings, default_iso_class: e.target.value })}>
              {["f", "m", "c", "v"].map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <label>
            Default TSDS0407 process
            <select value={settings.default_process} disabled={!canEdit}
              onChange={(e) => setSettings({ ...settings, default_process: e.target.value })}>
              {Object.entries(procs).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <label>
            Preferred standard on a conflict
            <select value={settings.preferred_standard} disabled={!canEdit}
              onChange={(e) => setSettings({ ...settings, preferred_standard: e.target.value as GtOrgSettings["preferred_standard"] })}>
              <option value="">None — leave blank and flag</option>
              <option value="ISO2768">ISO 2768 / IS 2102</option>
              <option value="TSDS0407">TVS TSDS0407</option>
            </select>
          </label>
          <label>
            Range boundaries (TSDS0407)
            <select value={settings.range_convention} disabled={!canEdit}
              onChange={(e) => setSettings({ ...settings, range_convention: e.target.value as GtOrgSettings["range_convention"] })}>
              <option value="upper_inclusive">Over X up to and including Y (4 → '1–4')</option>
              <option value="lower_inclusive">From X up to but excluding Y (4 → '4–16')</option>
            </select>
          </label>
          <label>
            Part-family pattern (regex on part number)
            <input value={settings.family_pattern} disabled={!canEdit} placeholder="e.g. ^([A-Z]\d{4})"
              onChange={(e) => setSettings({ ...settings, family_pattern: e.target.value })} />
          </label>
        </div>
      )}
      {settings && (
        <div className="gt-admin-actions">
          {canEdit && (
            <button className="btn btn-primary btn-sm" onClick={() => void run(() => api.gtSaveSettings(settings), "Settings saved.")}>
              Save settings
            </button>
          )}
          {!canEdit && <span className="muted">Organisation settings can be changed by the super admin; rules below can be added by any inspector.</span>}
        </div>
      )}

      <h3>Rules</h3>
      <div className="gt-admin-actions">
        <button className="btn btn-ghost btn-sm" onClick={() => void api.gtExportRules("csv").catch((e) => setErr(String(e)))}>Export CSV</button>
        <button className="btn btn-ghost btn-sm" onClick={() => void api.gtExportRules("xlsx").catch((e) => setErr(String(e)))}>Export Excel</button>
        <button className="btn btn-ghost btn-sm" onClick={() => fileRef.current?.click()}>Import CSV / Excel</button>
        <input ref={fileRef} type="file" accept=".csv,.xlsx" style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) void run(async () => {
              const r = await api.gtImportRules(f);
              if (r.errors.length) throw new Error(`Imported ${r.created}; errors: ${r.errors.join("; ")}`);
            }, "Rules imported.");
          }} />
      </div>
      <table className="table mt">
        <thead>
          <tr>
            <th>Name</th><th>Scope</th><th>Process</th><th>Feature</th><th>Size range (mm)</th><th>Tolerance</th>
            <th>Created</th><th>Active</th><th></th>
          </tr>
        </thead>
        <tbody>
          {rules.map((r) => (
            <tr key={r.id} className={r.active ? "" : "gt-rule-off"}>
              <td>{r.name}</td>
              <td>{SCOPES.find(([k]) => k === r.scope)?.[1]} <span className="muted">{r.scope_key}</span></td>
              <td>{r.process ? procs[r.process] || r.process : "any"}</td>
              <td>{cats.find((c) => c.id === r.category)?.label || r.category}{r.hole_type ? ` (${r.hole_type})` : ""}</td>
              <td>{rangeText(r)}</td>
              <td>{tolText(r)}</td>
              <td className="muted">{r.created_by} {r.created_at?.slice(0, 10)}</td>
              <td>
                <input type="checkbox" checked={r.active}
                  onChange={(e) => void run(() => api.gtUpdateRule(r.id, { active: e.target.checked }), e.target.checked ? "Rule enabled." : "Rule disabled.")} />
              </td>
              <td className="row">
                <button className="btn btn-ghost btn-sm" onClick={() => { setEditing(r.id); setDraft(r); }}>Edit</button>
                <button className="btn btn-ghost btn-sm"
                  onClick={() => void api.gtRuleHistory(r.id).then((h) => setHistory({ id: r.id, rows: h.history }))}>History</button>
                <button className="btn btn-ghost btn-sm"
                  onClick={() => { if (window.confirm(`Delete rule "${r.name}"?`)) void run(() => api.gtDeleteRule(r.id), "Rule deleted."); }}>Delete</button>
              </td>
            </tr>
          ))}
          {!rules.length && (
            <tr><td colSpan={9} className="muted">No rules yet. Inspectors create them from the Inspection report ("apply to all similar").</td></tr>
          )}
        </tbody>
      </table>
      {history && (
        <div className="gt-banner">
          <b>History of {rules.find((r) => r.id === history.id)?.name || history.id}</b>
          <ul className="gt-preview-list">
            {history.rows.map((h, i) => <li key={i}>{h.changed_at} — {h.action} by {h.changed_by || "?"}</li>)}
          </ul>
          <button className="btn btn-ghost btn-sm" style={{ width: "auto" }} onClick={() => setHistory(null)}>Close</button>
        </div>
      )}

      <h3>{editing ? "Edit rule" : "New rule"}</h3>
      <div className="gt-admin-grid">
        <label>
          Scope
          <select value={draft.scope} onChange={(e) => setD({ scope: e.target.value as GtRule["scope"] })}>
            {SCOPES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
        </label>
        {draft.scope !== "customer" && (
          <label>
            {draft.scope === "drawing" ? "Drawing key (e.g. PN-K1234)" : "Family key"}
            <input value={draft.scope_key || ""} onChange={(e) => setD({ scope_key: e.target.value })} />
          </label>
        )}
        <label>
          Feature
          <select value={draft.category} onChange={(e) => setD({ category: e.target.value })}>
            {cats.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          </select>
        </label>
        {draft.category === "inside_dia" && (
          <label>
            Hole type
            <select value={draft.hole_type || ""} onChange={(e) => setD({ hole_type: e.target.value || null })}>
              <option value="">any</option><option value="bored">bored</option><option value="drilled">drilled</option>
            </select>
          </label>
        )}
        <label>
          Process (TSDS0407)
          <select value={draft.process || ""} onChange={(e) => setD({ process: e.target.value || null })}>
            <option value="">any</option>
            {Object.entries(procs).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>
          Size over (exclusive)
          <input value={draft.size_min ?? ""} onChange={(e) => setD({ size_min: n(e.target.value) })} placeholder="no minimum" />
        </label>
        <label>
          Up to and including
          <input value={draft.size_max ?? ""} onChange={(e) => setD({ size_max: n(e.target.value) })} placeholder="no maximum" />
        </label>
        <label>
          Unit
          <select value={draft.unit} onChange={(e) => setD({ unit: e.target.value as GtRule["unit"] })}>
            <option value="mm">mm</option><option value="deg">degrees</option><option value="%">% of nominal</option>
          </select>
        </label>
        <label>
          Upper
          <input value={draft.upper ?? ""} onChange={(e) => setD({ upper: n(e.target.value) ?? undefined })} />
        </label>
        <label>
          Lower
          <input value={draft.lower ?? ""} onChange={(e) => setD({ lower: n(e.target.value) ?? undefined })} />
        </label>
        <label>
          Name
          <input value={draft.name || ""} onChange={(e) => setD({ name: e.target.value })} placeholder="generated if empty" />
        </label>
      </div>
      <div className="gt-admin-actions">
        <button className="btn btn-primary btn-sm" onClick={() => void saveDraft()}>{editing ? "Save changes" : "Create rule"}</button>
        {editing && <button className="btn btn-ghost btn-sm" onClick={() => { setEditing(null); setDraft(EMPTY_RULE); }}>Cancel</button>}
        {!jobId && (
          <input style={{ maxWidth: 260 }} value={previewJob} onChange={(e) => setPreviewJob(e.target.value)} placeholder="Job ID to preview on" />
        )}
        <button className="btn btn-ghost btn-sm" onClick={() => void doPreview("rule")}>
          Preview this rule{jobId ? " on this drawing" : ""}
        </button>
        <button className="btn btn-ghost btn-sm" onClick={() => void doPreview("settings")}>
          Preview settings{jobId ? " on this drawing" : ""}
        </button>
      </div>
      {preview && (
        <div className="gt-banner">
          <b>{preview.length ? `${preview.length} open dimension(s) would change` : "No open dimension on that drawing would change."}</b>
          <ul className="gt-preview-list">{preview.map((p) => <li key={p}>{p}</li>)}</ul>
        </div>
      )}

      <h3>Loaded standards (read-only)</h3>
      {std?.standards.map((s) => (
        <details key={s.id} className="mb">
          <summary>{s.standard} — {s.title}</summary>
          {s.transcription?.status && <p className="gt-transcription">{s.transcription.status}</p>}
          {Object.entries(s.tables).map(([tid, t]) => (
            <div key={tid}>
              <b>{t.title}</b> <span className="muted">({t.unit})</span>
              <table className="table gt-std-table">
                <thead>
                  <tr>
                    <th>Size</th>
                    {Object.values(t.columns).map((c) => <th key={c.label}>{c.label}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {(t.bands.length ? t.bands : [["all sizes", null, null] as [string, null, null]]).map((b, i) => (
                    <tr key={String(b[0])}>
                      <td>{b[0]}</td>
                      {Object.values(t.columns).map((c) => (
                        <td key={c.label}>{c.values ? c.values[i] ?? "—" : (c.all_sizes ?? c.all_sizes_text ?? "")}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </details>
      ))}
    </div>
  );
}
