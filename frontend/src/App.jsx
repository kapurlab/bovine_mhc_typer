import { useEffect, useRef, useState } from "react";
import "./App.css";

// MHC Typer (bola_gui) — bovine MHC (BoLA) genotyping from ONT amplicon reads.
// v1 vertical slice: pick a run folder -> select barcodes -> DRB3 typing ->
// live (polled) log -> per-animal report. Class I is gated behind config.
// All backend calls are RELATIVE (./api/...) so they survive the OOD proxy;
// the job log is POLLED via /logtext (SSE is unreliable through OOD's Apache).

const AMPLICONS = [
  { id: "drb3", label: "DRB3 (Class II) — reliable", classI: false },
  { id: "bov711", label: "Bov7/11 (Class I) — provisional", classI: true },
  { id: "bosex", label: "BosEx (Class I) — provisional", classI: true },
  { id: "utr", label: "5′UTR (Class I) — provisional", classI: true },
];

export default function App() {
  const [config, setConfig] = useState(null);
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState("");
  const [newProject, setNewProject] = useState("");
  const [runs, setRuns] = useState([]);
  const [runPath, setRunPath] = useState("");
  const [barcodes, setBarcodes] = useState([]);        // available for the picked run
  const [selected, setSelected] = useState({});         // barcode -> bool
  const [amplicon, setAmplicon] = useState("drb3");
  const [job, setJob] = useState(null);                 // {id,status}
  const [log, setLog] = useState("");
  const [results, setResults] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pollRef = useRef(null);
  const logRef = useRef(null);

  const classIEnabled = !!config?.enable_class_i;

  useEffect(() => { load(); return () => clearInterval(pollRef.current); }, []);
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [log]);

  async function load() {
    try {
      const [c, p, r] = await Promise.all([
        fetch("./api/config").then((x) => x.json()),
        fetch("./api/projects").then((x) => x.json()),
        fetch("./api/runs").then((x) => x.json()),
      ]);
      setConfig(c);
      setProjects(p || []);
      setRuns(r || []);
      if (p?.length && !project) setProject(p[0].name);
    } catch (e) { setError(String(e)); }
  }

  function pickRun(path) {
    setRunPath(path);
    const run = runs.find((x) => x.path === path);
    const bcs = run?.barcodes || [];
    setBarcodes(bcs);
    setSelected(Object.fromEntries(bcs.map((b) => [b, true])));
  }

  async function createProject() {
    const name = newProject.trim();
    if (!name) return;
    try {
      const res = await fetch("./api/projects", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, scope: "personal" }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || res.status);
      const created = await res.json();
      setNewProject("");
      await load();
      setProject(created.name);
    } catch (e) { setError(String(e)); }
  }

  async function saveConfig(patch) {
    const next = { ...config, ...patch };
    setConfig(next);
    await fetch("./api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
  }

  const chosenBarcodes = () => barcodes.filter((b) => selected[b]);

  async function runTyping() {
    setError("");
    if (!project) return setError("Select or create a project first.");
    if (!runPath) return setError("Pick a run folder.");
    const bcs = chosenBarcodes();
    if (!bcs.length) return setError("Select at least one barcode.");
    setBusy(true); setLog(""); setResults([]);
    try {
      const res = await fetch("./api/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project, run_dir: runPath, amplicon, barcodes: bcs }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || res.status);
      const { job_id } = await res.json();
      setJob({ id: job_id, status: "running" });
      watch(job_id);
    } catch (e) { setError(String(e)); setBusy(false); }
  }

  function watch(id) {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const r = await fetch(`./api/jobs/${id}/logtext`).then((x) => x.json());
        setLog(r.log || "");
        setJob({ id, status: r.status });
        if (r.status === "succeeded" || r.status === "failed") {
          clearInterval(pollRef.current);
          setBusy(false);
          const res = await fetch(`./api/jobs/${id}/results`).then((x) => x.json());
          setResults(res.files || res || []);
        }
      } catch (_) { /* keep polling */ }
    }, 1500);
  }

  const allOn = barcodes.length && barcodes.every((b) => selected[b]);

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>MHC Typer <span className="version-tag">bola · v1</span></h1>
          <div className="subtitle">Bovine MHC (BoLA) genotyping from Oxford Nanopore amplicon reads</div>
        </div>
        {job && <span className={`status-pill ${job.status}`}>{job.status}</span>}
      </header>

      {error && <div className="panel error">{error}</div>}

      <section className="panel">
        <div className="row-header">Project</div>
        <div className="row-grid">
          <select value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">— select project —</option>
            {projects.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
          </select>
          <input placeholder="new project name" value={newProject}
                 onChange={(e) => setNewProject(e.target.value)} />
          <button onClick={createProject}>Create</button>
        </div>
      </section>

      <section className="panel">
        <div className="row-header">Run &amp; barcodes</div>
        <div className="row-grid">
          <select value={runPath} onChange={(e) => pickRun(e.target.value)}>
            <option value="">— select a run folder —</option>
            {runs.map((r) => <option key={r.path} value={r.path}>{r.name} ({r.barcodes.length} barcodes)</option>)}
          </select>
          <select value={amplicon} onChange={(e) => setAmplicon(e.target.value)}>
            {AMPLICONS.map((a) => (
              <option key={a.id} value={a.id} disabled={a.classI && !classIEnabled}>
                {a.label}{a.classI && !classIEnabled ? " (enable in Settings)" : ""}
              </option>
            ))}
          </select>
        </div>
        {barcodes.length > 0 && (
          <>
            <div className="row-subhead">
              <label>
                <input type="checkbox" checked={!!allOn}
                       onChange={(e) => setSelected(Object.fromEntries(barcodes.map((b) => [b, e.target.checked])))} />
                {" "}select all ({chosenBarcodes().length}/{barcodes.length})
              </label>
            </div>
            <div className="barcode-grid">
              {barcodes.map((b) => (
                <label key={b} className="barcode-chip">
                  <input type="checkbox" checked={!!selected[b]}
                         onChange={(e) => setSelected((s) => ({ ...s, [b]: e.target.checked }))} />
                  {" "}{b}
                </label>
              ))}
            </div>
          </>
        )}
        <div className="row-actions">
          <button className="primary" disabled={busy} onClick={runTyping}>
            {busy ? "Running…" : `Run ${amplicon.toUpperCase()} typing`}
          </button>
        </div>
      </section>

      {(log || job) && (
        <section className="panel">
          <div className="row-header">Pipeline log</div>
          <pre className="log" ref={logRef}>{log || "Waiting for output…"}</pre>
        </section>
      )}

      {results.length > 0 && (
        <section className="panel">
          <div className="row-header">Results</div>
          <ul className="results">
            {results.map((f) => (
              <li key={f.name}>
                {f.openable
                  ? <a href={`./api/jobs/${job.id}/file?path=${encodeURIComponent(f.name)}&inline=1`} target="_blank" rel="noreferrer">{f.label || f.name}</a>
                  : <a href={`./api/jobs/${job.id}/file?path=${encodeURIComponent(f.name)}`}>{f.label || f.name} ↓</a>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <details className="panel">
        <summary className="row-header">Settings</summary>
        {config && (
          <div className="settings-grid">
            {["runs_root", "bola_refs", "ont_env_bin", "phase_env_bin", "medaka_model"].map((k) => (
              <label key={k} className="setting">
                <span>{k}</span>
                <input value={config[k] || ""} onChange={(e) => setConfig({ ...config, [k]: e.target.value })}
                       onBlur={(e) => saveConfig({ [k]: e.target.value })} />
              </label>
            ))}
            <label className="setting">
              <span>enable Class I (provisional)</span>
              <input type="checkbox" checked={!!config.enable_class_i}
                     onChange={(e) => saveConfig({ enable_class_i: e.target.checked })} />
            </label>
          </div>
        )}
      </details>
    </div>
  );
}
