"use client";
// 4.0 — « Remember for next time? », under a deliverable the person just validated
// (backend/learn.py). 1 to 3 facts from THIS work — the person's own corrections, the template
// it followed, the structure that was validated — shown exactly as they will be saved; the
// person unticks or rewrites, then ONE click keeps them in the project's memory (written and
// approved through the quarantine, journaled). « Not now » is remembered: no second offer.
import { useEffect, useState } from "react";
import { ApiError } from "@/lib/simple/api";
import { dismissLearn, fetchLearn, retainLearn, type LearnOffer as Offer } from "@/lib/simple/org";
import { useOrgT } from "@/lib/simple/dictOrg";
import { useUI } from "@/lib/simple/i18n";
import Icon from "./icons";

export default function LearnOffer({ sid, name, validatedAt }: { sid: string; name: string; validatedAt: number | null }) {
  const orgT = useOrgT();
  const { lang } = useUI();
  const [o, setO] = useState<Offer | null>(null);
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => {
    setO(null); setDone(""); setErr("");
    fetchLearn(sid, name, lang).then((x) => {
      setO(x);
      setTexts(Object.fromEntries(x.facts.map((f) => [f.id, f.text])));
      setPicked(new Set(x.facts.map((f) => f.id)));
    }).catch(() => setO(null));
  }, [sid, name, lang, validatedAt]);
  if (done) return <p className="sk-small sk-validated" role="status"><Icon name="check" small />{done}</p>;
  if (!o || o.state !== "offer" || !o.facts.length) return null;
  const keep = async () => {
    setBusy(true); setErr("");
    try {
      const r = await retainLearn(sid, name, o.facts.filter((f) => picked.has(f.id))
        .map((f) => ({ text: (texts[f.id] || "").trim(), kind: f.kind })), lang);
      const n = r.notes?.length || 0;
      setDone(n > 1 ? orgT("learn.done.n", { n }) : orgT("learn.done"));
    } catch (e) { setErr(orgT("learn.failed", { why: e instanceof ApiError ? e.message : "" })); } finally { setBusy(false); }
  };
  const later = async () => {
    try { await dismissLearn(sid, name); } catch { /* the offer simply stays */ }
    setDone(orgT("learn.dismissed"));
  };
  const n = picked.size;
  return (
    <section className="sk-learn" aria-label={orgT("learn.title")} style={{ flexBasis: "100%" }}>
      <div className="sk-small"><strong>{orgT("learn.title")}</strong></div>
      <p className="sk-xs sk-muted">{orgT("learn.hint")}</p>
      <div className="sk-list">
        {o.facts.map((f) => (
          <div key={f.id} className="sk-learn-fact">
            <label className="sk-small sk-row">
              <input type="checkbox" checked={picked.has(f.id)} onChange={(e) => {
                const s = new Set(picked); if (e.target.checked) s.add(f.id); else s.delete(f.id); setPicked(s);
              }} />
              <span className="sk-xs sk-muted">{f.why}</span>
            </label>
            <textarea className="sk-small" rows={2} value={texts[f.id] || ""} aria-label={orgT("learn.edit")}
              onChange={(e) => setTexts({ ...texts, [f.id]: e.target.value })} />
          </div>
        ))}
      </div>
      <div className="sk-row">
        <button className="sk-btn sk-btn-primary" disabled={busy || n === 0} onClick={keep}>
          {busy ? <span className="sk-spin" aria-hidden /> : <Icon name="check" />}{n > 1 ? orgT("learn.keep.n", { n }) : orgT("learn.keep")}</button>
        <button className="sk-btn sk-btn-quiet" disabled={busy} onClick={later}>{orgT("learn.later")}</button>
      </div>
      {err && <div className="sk-error" role="alert"><Icon name="alert" small /><span>{err}</span></div>}
    </section>
  );
}
