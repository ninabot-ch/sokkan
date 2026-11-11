"use client";
// Simple profile — hook points for the study mode (feature `study_mode`, another branch): the
// scenario of a work (S1…S6) under the thread's title, and the two questions at the end of a
// task, once the deliverable is validated. Nothing here depends on that branch: without the
// feature in the registry no request is made and nothing is rendered; with it, every call
// tolerates a missing route or a refusal (the hook then stays silent).
//   GET|PUT /api/sessions/{sid}/study            {tagged, scenario, …} / {scenario}
//   POST    /api/sessions/{sid}/study/feedback   {useful: 1-5, verify_minutes}
//   GET     /api/study/me                        {enabled, active, scenarios[], consent{given}}
import { useEffect, useState } from "react";
import { useFeatures } from "@/lib/features";
import { useT } from "@/lib/simple/i18n";
import Icon from "./icons";

export interface Study { scenarios: string[]; tag: { tagged: boolean; scenario?: string | null; mine?: boolean } | null }

async function tolerant<T>(url: string, init?: RequestInit): Promise<T | null> {
  try {
    const r = await fetch(url, { cache: "no-store", ...init });
    return r.ok ? ((await r.json()) as T) : null;
  } catch { return null; }
}

export function useStudy(sid: string): Study | null {
  const on = !!useFeatures().registry?.items.find((i) => i.id === "study_mode")?.enabled;
  const [study, setStudy] = useState<Study | null>(null);
  useEffect(() => {
    setStudy(null);
    if (!on) return;
    let live = true;
    (async () => {
      const me = await tolerant<{ enabled: boolean; active: boolean; scenarios?: string[]; consent?: { given?: boolean } | null }>("/api/study/me");
      if (!live || !me?.enabled || !me.active || !me.consent?.given) return;
      const tag = await tolerant<Study["tag"]>(`/api/sessions/${sid}/study`);
      if (live) setStudy({ scenarios: me.scenarios || [], tag });
    })();
    return () => { live = false; };
  }, [on, sid]);
  return study;
}

export function StudyTag({ sid, study }: { sid: string; study: Study }) {
  const t = useT();
  const [value, setValue] = useState(study.tag?.scenario || "");
  if (!study.scenarios.length) return null;
  return (
    <div className="sk-study-row">
      <label htmlFor={`sk-sc-${sid}`} className="sk-small sk-muted">{t("st.scenario")}</label>
      <select id={`sk-sc-${sid}`} className="sk-input sk-select" value={value} onChange={async (e) => {
        const v = e.target.value;
        setValue(v);
        await tolerant(`/api/sessions/${sid}/study`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ scenario: v || null }) });
      }}>
        <option value="">{t("st.scenario.none")}</option>
        {study.scenarios.map((s) => <option key={s} value={s}>{s}</option>)}
      </select>
    </div>
  );
}

export function StudyFeedback({ sid }: { sid: string }) {
  const t = useT();
  const [useful, setUseful] = useState(0);
  const [minutes, setMinutes] = useState("");
  const [state, setState] = useState<"ask" | "busy" | "sent">("ask");
  if (state === "sent") return <p className="sk-small sk-validated" role="status"><Icon name="check" small />{t("st.fb.thanks")}</p>;
  const ready = useful > 0 && minutes.trim() !== "" && Number(minutes) >= 0;
  return (
    <section className="sk-question" aria-labelledby={`sk-fb-${sid}`}>
      <h2 className="sk-h3" id={`sk-fb-${sid}`}>{t("st.fb.title")}</h2>
      <fieldset>
        <legend className="sk-legend">{t("st.fb.useful")}</legend>
        <div className="sk-row">
          <span className="sk-small sk-muted">{t("st.fb.low")}</span>
          {[1, 2, 3, 4, 5].map((n) => <button key={n} type="button" className="sk-opt" aria-pressed={useful === n} onClick={() => setUseful(n)}>{n}</button>)}
          <span className="sk-small sk-muted">{t("st.fb.high")}</span>
        </div>
      </fieldset>
      <div className="sk-field">
        <label htmlFor={`sk-fbm-${sid}`} className="sk-legend">{t("st.fb.verify")}</label>
        <input id={`sk-fbm-${sid}`} className="sk-input sk-input-short" inputMode="decimal" value={minutes} onChange={(e) => setMinutes(e.target.value.replace(",", "."))} />
      </div>
      <div><button className="sk-btn sk-btn-primary" disabled={!ready || state === "busy"} onClick={async () => {
        setState("busy");
        const r = await tolerant(`/api/sessions/${sid}/study/feedback`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ useful, verify_minutes: Number(minutes) }) });
        setState(r ? "sent" : "ask");
      }}>{t("st.fb.send")}</button></div>
    </section>
  );
}
