"use client";
// 4.0 — Settings › Organisation: the administrator fills, once, what every work knows about the
// organisation (backend/orgprofile.py). Others read it (the IBAN shows only its ends to them).
// The onboarding lot's « organisation » step uses the same routes.
import { useEffect, useState } from "react";
import { ApiError } from "@/lib/simple/api";
import { dropOrgLogo, fetchOrgPreview, fetchOrgProfile, saveOrgProfile, uploadOrgLogo,
  type OrgPerson, type OrgProfile as Profile } from "@/lib/simple/org";
import { useOrgT, type OrgKey } from "@/lib/simple/dictOrg";
import Icon from "./icons";

const TEXT: (keyof Profile)[] = ["name", "legal_name", "legal_form", "sector", "street", "postcode", "city", "country",
  "uid", "vat", "iban", "bank", "phone", "email", "website", "tone"];

export default function OrgProfile() {
  const orgT = useOrgT();
  const [p, setP] = useState<Profile | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [preview, setPreview] = useState<{ text: string; tokens_est: number } | null>(null);
  useEffect(() => { fetchOrgProfile().then(setP).catch(() => setP(null)); }, []);
  if (!p) return null;
  const edit = !!p.can_edit;
  const set = (k: keyof Profile, v: unknown) => setP({ ...p, [k]: v } as Profile);
  const save = async () => {
    setBusy(true); setMsg(""); setErr("");
    try {
      const body: Partial<Profile> = {};
      for (const k of TEXT) (body as Record<string, unknown>)[k] = p[k];
      Object.assign(body, { lang: p.lang, address_form: p.address_form, formats: p.formats,
        people: p.people.filter((x) => x.name.trim()), signatories: p.signatories.filter((x) => x.name.trim()) });
      setP(await saveOrgProfile(body)); setMsg(orgT("org.saved"));
    } catch (e) {
      const k = e instanceof ApiError ? (`org.err.${e.code}` as OrgKey) : null;
      setErr(k && orgT(k) !== k ? orgT(k) : orgT("org.failed", { why: e instanceof Error ? e.message : "" }));
    } finally { setBusy(false); }
  };
  const field = (k: keyof Profile, hint?: string) => (
    <label key={k} className="sk-small sk-org-field">
      <span>{orgT(`org.${k}` as OrgKey)}</span>
      <input type="text" value={String(p[k] ?? "")} disabled={!edit} spellCheck={false}
        onChange={(e) => set(k, e.target.value)} />
      {hint && <span className="sk-xs sk-muted">{hint}</span>}
    </label>
  );
  const rows = (k: "people" | "signatories") => (
    <div className="sk-list" role="group" aria-label={orgT(`org.${k}` as OrgKey)}>
      {p[k].map((x: OrgPerson, i: number) => (
        <div key={i} className="sk-row sk-org-person">
          {k === "people" && <input type="text" value={x.initials || ""} disabled={!edit} aria-label={orgT("org.person.initials")}
            placeholder={orgT("org.person.initials")} size={4} onChange={(e) => set(k, p[k].map((y, j) => j === i ? { ...y, initials: e.target.value } : y))} />}
          <input type="text" value={x.name} disabled={!edit} aria-label={orgT("org.person.name")} placeholder={orgT("org.person.name")}
            onChange={(e) => set(k, p[k].map((y, j) => j === i ? { ...y, name: e.target.value } : y))} />
          <input type="text" value={x.function} disabled={!edit} aria-label={orgT("org.person.function")} placeholder={orgT("org.person.function")}
            onChange={(e) => set(k, p[k].map((y, j) => j === i ? { ...y, function: e.target.value } : y))} />
          {k === "people" && <input type="email" value={x.email || ""} disabled={!edit} aria-label={orgT("org.person.email")}
            placeholder={orgT("org.person.email")} onChange={(e) => set(k, p[k].map((y, j) => j === i ? { ...y, email: e.target.value } : y))} />}
          {edit && <button className="sk-btn sk-btn-quiet sk-btn-icon" aria-label={orgT("org.remove")}
            onClick={() => set(k, p[k].filter((_, j) => j !== i))}><Icon name="close" small /></button>}
        </div>
      ))}
      {edit && <div><button className="sk-btn sk-btn-quiet" onClick={() => set(k, [...p[k], { name: "", function: "" }])}>
        {orgT(k === "people" ? "org.person.add" : "org.signatory.add")}</button></div>}
    </div>
  );
  return (
    <fieldset className="sk-claude-set sk-org">
      <legend className="sk-legend">{orgT("org.title")}</legend>
      <p className="sk-small sk-muted">{orgT("org.hint")}</p>
      {!edit && <p className="sk-small sk-muted">{orgT("org.readonly")}</p>}
      {TEXT.map((k) => field(k, k === "iban" ? orgT("org.iban.hint") : undefined))}
      <label className="sk-small sk-org-field"><span>{orgT("org.lang")}</span>
        <select value={p.lang} disabled={!edit} onChange={(e) => set("lang", e.target.value)}>
          <option value="fr">Français</option><option value="en">English</option>
          <option value="de">Deutsch</option><option value="it">Italiano</option>
        </select></label>
      <label className="sk-small sk-org-field"><span>{orgT("org.address")}</span>
        <select value={p.address_form} disabled={!edit} onChange={(e) => set("address_form", e.target.value)}>
          <option value="vous">{orgT("org.address.vous")}</option><option value="tu">{orgT("org.address.tu")}</option>
        </select></label>
      <label className="sk-small sk-org-field"><span>{orgT("org.formats")}</span>
        <select value={p.formats} disabled={!edit} onChange={(e) => set("formats", e.target.value)}>
          {(["ch", "fr", "de", "en"] as const).map((f) => <option key={f} value={f}>{orgT(`org.formats.${f}` as OrgKey)}</option>)}
        </select></label>
      <div className="sk-small"><strong>{orgT("org.signatories")}</strong></div>
      {rows("signatories")}
      <div className="sk-small"><strong>{orgT("org.people")}</strong></div>
      <p className="sk-xs sk-muted">{orgT("org.people.hint")}</p>
      {rows("people")}
      <div className="sk-small"><strong>{orgT("org.logo")}</strong></div>
      {p.logo && <img src={`/api/org/logo?v=${p.updated_at || 0}`} alt={orgT("org.logo")} className="sk-org-logo" />}
      {edit && (
        <div className="sk-row">
          <label className="sk-btn sk-btn-quiet"><input type="file" accept=".png,.jpg,.jpeg,.svg" hidden onChange={async (e) => {
            const f = e.target.files?.[0]; if (!f) return;
            try { await uploadOrgLogo(f); setP(await fetchOrgProfile()); } catch (x) { setErr(orgT("org.failed", { why: x instanceof Error ? x.message : "" })); }
          }} />{orgT("org.logo.add")}</label>
          {p.logo && <button className="sk-btn sk-btn-quiet" onClick={async () => { await dropOrgLogo(); setP(await fetchOrgProfile()); }}>{orgT("org.logo.drop")}</button>}
        </div>
      )}
      {p.completeness && p.completeness.missing.length > 0 && (
        <p className="sk-xs sk-muted">{orgT("org.missing", { list: p.completeness.missing.join(", ") })}</p>)}
      {edit && (
        <div className="sk-row">
          <button className="sk-btn sk-btn-primary" disabled={busy} onClick={save}>
            {busy ? <span className="sk-spin" aria-hidden /> : <Icon name="check" />}{orgT("org.save")}</button>
          <button className="sk-btn sk-btn-quiet" onClick={async () => setPreview(preview ? null : await fetchOrgPreview())}>{orgT("org.preview")}</button>
        </div>
      )}
      {preview && (
        <div className="sk-notice sk-org-preview"><pre className="sk-xs">{preview.text}</pre>
          <span className="sk-xs sk-muted">{orgT("org.preview.size", { n: preview.tokens_est })}</span></div>)}
      {msg && <div className="sk-small" role="status">{msg}</div>}
      {err && <div className="sk-error" role="alert"><Icon name="alert" small /><span>{err}</span></div>}
    </fieldset>
  );
}
