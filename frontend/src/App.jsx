import { useEffect, useRef, useState } from "react";
import "./App.css";

// MHC Typer (bola_gui) — bovine MHC (BoLA) genotyping from ONT amplicon reads.
// Shares the Kapur Lab pipeline shell (header + status strip + collapsible
// panels + dark log). Relative ./api/... URLs survive the OOD proxy; the job
// log is POLLED via /logtext (SSE is unreliable through OOD's Apache).

const APP_VERSION = "0.1.0";

const AMPLICONS = [
  { id: "drb3", label: "DRB3 (Class II) — reliable", classI: false },
  { id: "bov711", label: "Bov7/11 (Class I) — provisional", classI: true },
  { id: "bosex", label: "BosEx (Class I) — provisional", classI: true },
  { id: "utr", label: "5′UTR (Class I) — provisional", classI: true },
];

const SETTING_FIELDS = [
  ["runs_root", "ONT runs root", "Folder holding run folders (barcodeNN/ of *.fastq.gz)."],
  ["barcode_map", "Barcode→animal map", "barcode_sample_map.tsv — maps barcodes to animal IDs."],
  ["bola_refs", "BoLA references", "blast_db/BoLA_{nuc,gen}, chr23 contig, haplotypes.json."],
  ["ont_env_bin", "ONT env bin", "minimap2, samtools, medaka, nanoq."],
  ["phase_env_bin", "Phase env bin", "bcftools, vsearch, HAPCUT2."],
  ["medaka_model", "medaka model", "MUST match the basecaller (R10.4.1 SUP)."],
];

export default function App() {
  const [config, setConfig] = useState(null);
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState("");
  const [newProject, setNewProject] = useState("");
  const [runs, setRuns] = useState([]);
  const [runPath, setRunPath] = useState("");
  const [barcodes, setBarcodes] = useState([]);     // [{barcode, sample, tissue}]
  const [selected, setSelected] = useState({});      // barcode -> bool
  const [amplicon, setAmplicon] = useState("drb3");
  const [threads, setThreads] = useState("");
  const [job, setJob] = useState(null);
  const [log, setLog] = useState("");
  const [table, setTable] = useState(null);          // {rows, summary}
  const [results, setResults] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const pollRef = useRef(null);
  const logRef = useRef(null);

  const classIEnabled = !!config?.enable_class_i;
  const jobStatus = job?.status || "idle";
  const statusText = { idle: "Idle", running: "Running…", succeeded: "Done", failed: "Failed" }[jobStatus] || "Idle";
  const chosen = barcodes.filter((b) => selected[b.barcode]);
  const runName = runs.find((r) => r.path === runPath)?.name || "";
  const sum = table?.summary || { pass: 0, review: 0, fail: 0 };

  useEffect(() => { load(); return () => clearInterval(pollRef.current); }, []);
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [log]);

  async function load() {
    try {
      const [c, p, r] = await Promise.all([
        fetch("./api/config").then((x) => x.json()),
        fetch("./api/projects").then((x) => x.json()),
        fetch("./api/runs").then((x) => x.json()),
      ]);
      setConfig(c); setProjects(p || []); setRuns(r || []);
      if (p?.length && !project) setProject(p[0].name);
    } catch (e) { setError(String(e)); }
  }

  function pickRun(path) {
    setRunPath(path);
    const bcs = runs.find((x) => x.path === path)?.barcodes || [];
    setBarcodes(bcs);
    setSelected(Object.fromEntries(bcs.map((b) => [b.barcode, true])));
    setTable(null); setResults([]);
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
      setNewProject(""); await load(); setProject(created.name);
    } catch (e) { setError(String(e)); }
  }

  async function saveConfig(patch) {
    setConfig((c) => ({ ...c, ...patch }));
    await fetch("./api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
  }

  async function runTyping() {
    setError("");
    if (!project) return setError("Select or create a project first.");
    if (!runPath) return setError("Pick a run folder.");
    if (!chosen.length) return setError("Select at least one animal.");
    setBusy(true); setLog(""); setResults([]); setTable(null);
    try {
      const body = {
        project, run_dir: runPath, amplicon,
        barcodes: chosen.map((b) => `${b.barcode}:${b.sample || b.barcode}`),
      };
      if (threads) body.threads = Number(threads);
      const res = await fetch("./api/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
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
        setLog(r.log || ""); setJob({ id, status: r.status });
        if (r.status === "succeeded" || r.status === "failed") {
          clearInterval(pollRef.current); setBusy(false);
          const [t, res] = await Promise.all([
            fetch(`./api/jobs/${id}/table`).then((x) => x.json()).catch(() => null),
            fetch(`./api/jobs/${id}/results`).then((x) => x.json()).catch(() => []),
          ]);
          setTable(t); setResults(res.files || res || []);
        }
      } catch (_) { /* keep polling */ }
    }, 1500);
  }

  const allOn = barcodes.length > 0 && barcodes.every((b) => selected[b.barcode]);

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-brand">
          <img className="app-logo" src="./mhc_icon.svg" alt="BoLA MHC DNA icon" />
          <div>
            <h1>MHC Typer <span className="version-tag">v{APP_VERSION}</span></h1>
            <p>Bovine MHC (BoLA) genotyping from Oxford Nanopore amplicon reads</p>
          </div>
        </div>
        <div className="status-pill"><span className="dot" data-state={jobStatus} /><span>{statusText}</span></div>
      </header>

      <main className="layout">
        <section className="status-strip">
          <div className="status-item"><span className="status-label">Run</span><span className="status-value">{runName || "—"}</span></div>
          <div className="status-item"><span className="status-label">Selected</span><span className="status-value">{chosen.length || "—"}</span></div>
          <div className="status-item"><span className="status-label">Pass</span><span className="status-value" style={{ color: "#2f7d4f" }}>{table ? sum.pass : "—"}</span></div>
          <div className="status-item"><span className="status-label">Review</span><span className="status-value" style={{ color: "#a9741f" }}>{table ? sum.review : "—"}</span></div>
          <div className="status-item"><span className="status-label">Fail</span><span className="status-value" style={{ color: "#b04a29" }}>{table ? sum.fail : "—"}</span></div>
          <div className="status-item"><span className="status-label">Job</span><span className="status-value cap">{jobStatus === "running" ? <><span className="pulse-dot" />running</> : statusText}</span></div>
        </section>

        {error && <div className="alert-banner error">{error}</div>}

        {/* Step 1 — Data */}
        <div className="row-header"><h2>1 · Choose data</h2></div>
        <div className="panel">
          <div className="row-grid row-grid-split">
            <div className="input-column">
              <div className="panel-header"><h3>Project</h3></div>
              <div className="project-list">
                {projects.map((p) => (
                  <div key={p.name} className={`list-item${p.name === project ? " active" : ""}`} onClick={() => setProject(p.name)}>
                    <div className="item-top">
                      <span className="list-title">{p.name}</span>
                      <span className={`scope-badge scope-${p.scope || "personal"}`}>{p.scope || "personal"}</span>
                    </div>
                  </div>
                ))}
                {!projects.length && <p className="empty-msg">No projects yet — create one below.</p>}
              </div>
              <div className="row">
                <input placeholder="new project name" value={newProject} onChange={(e) => setNewProject(e.target.value)} />
                <button className="ghost action" onClick={createProject}>Create</button>
              </div>
            </div>

            <div className="input-column">
              <div className="panel-header"><h3>Run &amp; animals</h3></div>
              <select value={runPath} onChange={(e) => pickRun(e.target.value)}>
                <option value="">— select a run folder —</option>
                {runs.map((r) => <option key={r.path} value={r.path}>{r.name} ({r.barcodes.length})</option>)}
              </select>
              {barcodes.length > 0 && (
                <>
                  <label className="checkbox-label" style={{ marginTop: 8 }}>
                    <input type="checkbox" checked={allOn}
                           onChange={(e) => setSelected(Object.fromEntries(barcodes.map((b) => [b.barcode, e.target.checked])))} />
                    Select all ({chosen.length}/{barcodes.length} animals)
                  </label>
                  <div className="sample-list">
                    {barcodes.map((b) => (
                      <label key={b.barcode} className="checkbox-label sample-item">
                        <input type="checkbox" checked={!!selected[b.barcode]} onChange={(e) => setSelected((s) => ({ ...s, [b.barcode]: e.target.checked }))} />
                        <span className="sample-name">{b.sample || b.barcode}</span>
                        {b.sample ? <span className="muted"> · {b.barcode}</span> : null}
                        {b.tissue ? <span className="read-badge">{b.tissue}</span> : null}
                      </label>
                    ))}
                  </div>
                </>
              )}
              {!barcodes.length && <p className="empty-msg">Pick a run to list its animals.</p>}
            </div>
          </div>
        </div>

        {/* Step 2 — Run */}
        <div className="row-header"><h2>2 · Run typing</h2></div>
        <div className="panel">
          <div className="row-grid row-grid-split">
            <div className="input-column">
              <label className="form-label">Amplicon</label>
              <select value={amplicon} onChange={(e) => setAmplicon(e.target.value)}>
                {AMPLICONS.map((a) => (
                  <option key={a.id} value={a.id} disabled={a.classI && !classIEnabled}>
                    {a.label}{a.classI && !classIEnabled ? " (enable in Settings)" : ""}
                  </option>
                ))}
              </select>
              <label className="form-label" style={{ marginTop: 10 }}>Threads</label>
              <input placeholder="auto (12)" value={threads} onChange={(e) => setThreads(e.target.value.replace(/[^0-9]/g, ""))} />
              <button className="run-btn" disabled={busy} onClick={runTyping} style={{ marginTop: 12 }}>
                {busy ? "Running…" : `▶ Run ${amplicon.toUpperCase()} typing`}
              </button>
            </div>
            <div className="input-column">
              <div className="selection-box">
                <div className="sel-title">Current run</div>
                {job
                  ? <><div className="sel-row"><span className="sel-name">{runName} · {amplicon.toUpperCase()}</span></div>
                       <div className="note">{chosen.length} animals · job {job.id.slice(0, 8)} · {statusText}</div></>
                  : <p className="empty-msg">Select animals, set the amplicon, and Run.</p>}
              </div>
            </div>
          </div>
        </div>

        {/* Step 3 — Results (genotype QC table) */}
        <div className="row-header"><h2>3 · Genotypes</h2></div>
        <div className="panel">
          {table && table.rows?.length ? (
            <>
              <div className="note" style={{ marginBottom: 8 }}>
                {sum.total} animals · <b style={{ color: "#2f7d4f" }}>{sum.pass} pass</b> · <b style={{ color: "#a9741f" }}>{sum.review} review</b> · <b style={{ color: "#b04a29" }}>{sum.fail} fail</b>
              </div>
              <div style={{ overflowX: "auto" }}>
                <table className="geno-table">
                  <thead>
                    <tr><th>Animal</th><th>Barcode</th><th>DRB3 allele 1</th><th>DRB3 allele 2</th><th>Zyg.</th><th>Reads a1/a2</th><th>QC</th></tr>
                  </thead>
                  <tbody>
                    {table.rows.map((r) => (
                      <tr key={r.barcode}>
                        <td><b>{r.animal || "—"}</b>{r.tissue ? <span className="muted"> · {r.tissue}</span> : null}</td>
                        <td className="muted">{r.barcode}</td>
                        <td>{r.allele1 || "—"}</td>
                        <td>{r.allele2 || "—"}</td>
                        <td className="cap">{r.zygosity}</td>
                        <td className="muted">{r.count1}/{r.count2}</td>
                        <td><span className={`qc-badge qc-${r.qc}`}>{r.qc}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {results.length > 0 && (
                <ul className="results-list" style={{ marginTop: 10 }}>
                  {results.map((f) => (
                    <li className="results-item" key={f.name}>
                      <span className="result-icon">{f.name.endsWith(".html") ? "📄" : f.name.endsWith(".tsv") ? "▦" : "•"}</span>
                      <a className="result-name result-link"
                         href={`./api/jobs/${job.id}/file?path=${encodeURIComponent(f.name)}${f.openable ? "&inline=1" : ""}`}
                         target={f.openable ? "_blank" : undefined} rel="noreferrer">{f.label || f.name}</a>
                    </li>
                  ))}
                </ul>
              )}
            </>
          ) : <p className="empty-msg">Run typing to produce the per-animal DRB3 genotype table.</p>}
        </div>

        {/* Pipeline Log */}
        <div className="row-header"><h2>Pipeline Log</h2></div>
        <div className="panel">
          <div className="log-meta"><span className="dot" data-state={jobStatus} /> {statusText}{job ? ` · ${job.id.slice(0, 8)}` : ""}</div>
          <pre className="log" ref={logRef}>{log || <span className="log-placeholder">Select a run and click Run to start.</span>}</pre>
        </div>

        {/* Settings (advanced, tucked at the bottom) */}
        <div className="row-header">
          <h2>Settings</h2>
          <button className="ghost" onClick={() => setShowSettings((s) => !s)}>{showSettings ? "Hide" : "Show"}</button>
        </div>
        {showSettings && config && (
          <div className="panel">
            <div className="form-section">
              {SETTING_FIELDS.map(([k, label, hint]) => (
                <div className="row" key={k}>
                  <label className="form-label">{label}</label>
                  <input value={config[k] || ""}
                         onChange={(e) => setConfig({ ...config, [k]: e.target.value })}
                         onBlur={(e) => saveConfig({ [k]: e.target.value })} />
                  <p className="form-hint">{hint}</p>
                </div>
              ))}
              <label className="checkbox-label">
                <input type="checkbox" checked={!!config.enable_class_i}
                       onChange={(e) => saveConfig({ enable_class_i: e.target.checked })} />
                Enable Class I amplicons <span className="note">— provisional; short-amplicon Class I calls are leads, not genotypes</span>
              </label>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
