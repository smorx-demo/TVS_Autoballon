/**
 * General tolerances for open dimensions — drawing banner, source badge and the
 * "teach once" prompt shown when an inspector edits an open dimension's tolerance.
 */
import { useEffect, useMemo, useState } from "react";
import { api, type GtJobState, type GtRow, type GtTeach, type ReportRow } from "../api";
import "./general-tolerance.css";

const ISO_CLASSES: [string, string][] = [["f", "f (fine)"], ["m", "m (medium)"], ["c", "c (coarse)"], ["v", "v (very coarse)"]];

export function isOpenRow(r: ReportRow): boolean {
  return Boolean(r.tol_category);
}

/** CSS class for the tolerance inputs of a report row. */
export function gtCellClass(r: ReportRow): string {
  if (r.tol_auto) return "gt-auto";
  if (r.tol_source && r.tol_source.startsWith("None")) return "gt-none";
  if (r.tol_source === "Manual" && isOpenRow(r)) return "gt-manual";
  return "";
}

function badgeOf(r: ReportRow): { label: string; cls: string } | null {
  const s = r.tol_source || "";
  if (!s || s === "Drawing") return null;
  if (s === "Manual") return isOpenRow(r) ? { label: "M", cls: "gt-badge-manual" } : null;
  if (s.startsWith("Rule:")) return { label: "Rule", cls: "gt-badge-rule" };
  if (s.startsWith("None")) return { label: "!", cls: "gt-badge-none" };
  return { label: "GT", cls: "gt-badge-gt" };
}

export function GtBadge({ row, onReset }: { row: ReportRow; onReset?: () => void }) {
  const b = badgeOf(row);
  if (!b) return null;
  const tip = [
    row.tol_source,
    row.tol_angle ? `Chamfer angle: ${row.tol_angle}` : "",
    ...(row.tol_flags || []).map((f) => `• ${f}`),
    row.tol_source === "Manual" && onReset ? "Click to go back to the automatic value." : "",
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <button
      type="button"
      className={`gt-badge ${b.cls}${(row.tol_flags || []).length ? " gt-badge-flagged" : ""}`}
      title={tip}
      onClick={row.tol_source === "Manual" && onReset ? onReset : undefined}
    >
      {b.label}
      {(row.tol_flags || []).length > 0 && b.label !== "!" ? "*" : ""}
    </button>
  );
}

export function GtBanner({
  jobId,
  state,
  busy,
  onChanged,
  onError,
}: {
  jobId: string;
  state: GtJobState | null;
  busy: boolean;
  onChanged: (s: GtJobState) => Promise<void> | void;
  onError: (m: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [std, setStd] = useState("");
  const [cls, setCls] = useState("m");
  const [proc, setProc] = useState("");
  const [thk, setThk] = useState("");
  const [working, setWorking] = useState(false);

  useEffect(() => {
    if (!state) return;
    const ds = state.drawing_settings as { standard?: { id: string; cls?: string } | null; process?: string | null; sheet_thickness?: number | null };
    setStd(ds.standard?.id || "");
    setCls(ds.standard?.cls || state.banner.iso_class || "m");
    setProc(ds.process || "");
    setThk(ds.sheet_thickness != null ? String(ds.sheet_thickness) : "");
  }, [state]);

  if (!state) return null;
  const b = state.banner;
  const counts = state.counts || {};
  const unresolved = counts.unresolved || [];
  const tsds = (std || b.standard_id) === "TSDS0407";

  const apply = async () => {
    setWorking(true);
    try {
      const body: Parameters<typeof api.gtSetDrawing>[1] = {};
      if (std) body.standard = { id: std, cls: std === "ISO2768" ? cls : null };
      else body.clear_standard = true;
      if (proc) body.process = proc;
      else body.clear_process = true;
      if (thk.trim()) body.sheet_thickness = Number(thk.replace(",", "."));
      const s = await api.gtSetDrawing(jobId, body);
      await onChanged(s);
      setOpen(false);
    } catch (e: unknown) {
      onError(e instanceof Error ? e.message : "Could not change the general tolerance");
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className={`gt-banner${b.needs_process ? " gt-banner-warn" : ""}`}>
      <div className="gt-banner-main">
        <span className="gt-banner-tag">GT</span>
        <span className="gt-banner-text">{b.text}</span>
        {typeof counts.open === "number" && (
          <span className="gt-banner-counts muted">
            · {counts.open} open dim(s): {counts.standard || 0} by standard, {counts.rule || 0} by rule
            {counts.manual ? `, ${counts.manual} manual` : ""}
            {unresolved.length ? `, ${unresolved.length} unresolved (balloon ${unresolved.join(", ")})` : ""}
          </span>
        )}
        <button type="button" className="btn btn-ghost btn-sm gt-banner-btn" disabled={busy || working} onClick={() => setOpen((v) => !v)}>
          {open ? "Close" : "Change standard / process"}
        </button>
      </div>
      {(b.conflict || b.flags.length > 0 || b.needs_process || b.process_hint) && (
        <ul className="gt-banner-flags">
          {b.needs_process && <li>Process not stated on the drawing — machining assumed. Confirm the process below.</li>}
          {b.process_hint && <li>{b.process_hint}</li>}
          {b.flags.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      )}
      {open && (
        <div className="gt-banner-edit">
          <label>
            Standard for this drawing
            <select value={std} onChange={(e) => setStd(e.target.value)}>
              <option value="">As read from the drawing{b.standard ? ` (${b.standard})` : ""}</option>
              <option value="ISO2768">ISO 2768 / IS 2102</option>
              <option value="TSDS0407">TVS TSDS0407</option>
            </select>
          </label>
          {std === "ISO2768" && (
            <label>
              Class
              <select value={cls} onChange={(e) => setCls(e.target.value)}>
                {ISO_CLASSES.map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
            </label>
          )}
          {tsds && (
            <label>
              Process
              <select value={proc} onChange={(e) => setProc(e.target.value)}>
                <option value="">As read from the drawing{b.process ? ` (${state.processes[b.process] || b.process})` : ""}</option>
                {Object.entries(state.processes).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
          )}
          {tsds && (proc === "pressing" || b.process === "pressing") && (
            <label>
              Sheet thickness t (mm)
              <input value={thk} onChange={(e) => setThk(e.target.value)} placeholder="e.g. 1.6" />
            </label>
          )}
          <button type="button" className="btn btn-primary btn-sm" style={{ width: "auto" }} disabled={working} onClick={() => void apply()}>
            Apply to this drawing
          </button>
          <span className="muted gt-banner-note">Saved for drawing {b.drawing_key}; used for every revision.</span>
        </div>
      )}
    </div>
  );
}

export type TeachRequest = { row: ReportRow; low: string; high: string };

function num(v: string): number | null {
  const s = (v || "").trim().replace(",", ".");
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

export function GtTeachDialog({
  req,
  state,
  onCancel,
  onApply,
}: {
  req: TeachRequest;
  state: GtJobState | null;
  onCancel: () => void;
  onApply: (body: GtTeach) => Promise<void>;
}) {
  const gtRow: GtRow | undefined = useMemo(
    () => state?.rows.find((x) => x.balloon === req.row.balloon && (x.page || 1) === (req.row.page || 1)),
    [state, req],
  );
  const sug = gtRow?.suggest || null;
  const [scope, setScope] = useState<GtTeach["scope"]>("drawing");
  const [smin, setSmin] = useState(sug?.size_min != null ? String(sug.size_min) : "");
  const [smax, setSmax] = useState(sug?.size_max != null ? String(sug.size_max) : "");
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    setSmin(sug?.size_min != null ? String(sug.size_min) : "");
    setSmax(sug?.size_max != null ? String(sug.size_max) : "");
  }, [sug]);

  const up = num(req.high);
  const lo = num(req.low);
  const cat = sug?.category_label || req.row.tol_category || "dimension";
  const range = `${smin ? `>${smin}` : ""}${smin && smax ? " – " : ""}${smax ? `≤${smax}` : ""}` || "any size";
  const angular = sug?.category === "angular" || sug?.category === "chamfer_angle";

  const submit = async () => {
    if (up === null || lo === null) {
      setErr("Enter both the lower and the upper tolerance (e.g. -0.3 and +0.3).");
      return;
    }
    if (up < lo) {
      setErr("Upper tolerance must be ≥ lower tolerance.");
      return;
    }
    setSaving(true);
    setErr("");
    try {
      await onApply({
        balloon: req.row.balloon,
        page: req.row.page || 1,
        scope,
        upper: up,
        lower: lo,
        unit: angular ? "deg" : "mm",
        category: sug?.category,
        size_min: num(smin),
        size_max: num(smax),
        name: name.trim(),
      });
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Could not save");
      setSaving(false);
    }
  };

  const opts: { v: GtTeach["scope"]; label: string; hint: string; disabled?: boolean }[] = [
    { v: "dimension", label: "This dimension only", hint: `Balloon ${req.row.balloon}; kept on later revisions while the feature is unchanged.` },
    { v: "drawing", label: `All open ${cat} in ${range} on this drawing`, hint: `Drawing ${state?.scopes.drawing || ""}` },
    {
      v: "family",
      label: `All open ${cat} in ${range} — this part family`,
      hint: state?.family_enabled ? `Family ${state.scopes.family}` : "Set a part-family pattern in General Tolerance Settings (Inspection report toolbar)",
      disabled: !state?.family_enabled,
    },
    {
      v: "customer",
      label: `All open ${cat} in ${range} — all drawings of this customer`,
      hint: "Never overrides a standard a drawing states (a warning is shown instead).",
    },
  ];

  return (
    <div className="gt-teach-backdrop" role="dialog" aria-modal="true">
      <div className="gt-teach">
        <h3>Apply this tolerance to other open dimensions?</h3>
        <p className="muted">
          Balloon {req.row.balloon} · {req.row.nominal} · tolerance {req.high || "?"} / {req.low || "?"}
          {sug?.band ? ` · default range = band ${sug.band} of the active standard` : ""}
        </p>
        <div className="gt-teach-opts">
          {opts
            .filter((o) => !(o.v === "family" && o.disabled))
            .map((o) => (
              <label key={o.v} className={`gt-teach-opt${scope === o.v ? " sel" : ""}`}>
                <input type="radio" name="gt-scope" checked={scope === o.v} onChange={() => setScope(o.v)} />
                <span>
                  <b>{o.label}</b>
                  <small className="muted">{o.hint}</small>
                </span>
              </label>
            ))}
        </div>
        {scope !== "dimension" && (
          <div className="gt-teach-range">
            <label>
              Size over (mm, exclusive)
              <input value={smin} onChange={(e) => setSmin(e.target.value)} placeholder="no minimum" />
            </label>
            <label>
              up to and including (mm)
              <input value={smax} onChange={(e) => setSmax(e.target.value)} placeholder="no maximum" />
            </label>
            <label className="gt-teach-name">
              Rule name (optional)
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder={`${cat} ${range} ${req.high}/${req.low}`} />
            </label>
          </div>
        )}
        {err && <p className="gt-teach-err">{err}</p>}
        <div className="gt-teach-actions">
          <button type="button" className="btn btn-ghost btn-sm" disabled={saving} onClick={onCancel}>
            Keep as a manual value (decide later)
          </button>
          <button type="button" className="btn btn-primary btn-sm" style={{ width: "auto" }} disabled={saving} onClick={() => void submit()}>
            {saving ? "Applying…" : scope === "dimension" ? "Save for this dimension" : "Save rule and apply"}
          </button>
        </div>
      </div>
    </div>
  );
}
