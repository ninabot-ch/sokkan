"use client";
// 4.0 — a person's first sign-in: their position (a list read from the server), a tour of
// four cards (about a minute), a first work of their trade on an example document. Each part
// can be skipped; « welcomed » is kept on the server, so it shows once per person.
import { useEffect, useRef, useState } from "react";
import { fetchSample, saveMe, startIn, type Onboarding, type Role } from "@/lib/simple/onboarding";
import { newRequestId } from "@/lib/simple/api";
import { useOT, type OKey } from "@/lib/simple/dictOnboarding";
import { useUI } from "@/lib/simple/i18n";
import { currentProject } from "@/lib/project";
import Icon, { type IconName } from "../icons";
import { Preview, RolePicker } from "./parts";

const TOUR: { icon: IconName; title: OKey; text: OKey }[] = [
  { icon: "send", title: "wl.t1.title", text: "wl.t1.text" },
  { icon: "shield", title: "wl.t2.title", text: "wl.t2.text" },
  { icon: "seal", title: "wl.t3.title", text: "wl.t3.text" },
  { icon: "works", title: "wl.t4.title", text: "wl.t4.text" },
];

export default function Welcome({ ob, name, canStart, onClose, onStarted }: {
  ob: Onboarding; name: string; canStart: boolean; onClose: () => void; onStarted: (sid: string) => void;
}) {
  const t = useOT();
  const { lang } = useUI();
  const [part, setPart] = useState<"role" | "tour" | "first">("role");
  const [tour, setTour] = useState(0);
  const [role, setRole] = useState<Role | null>(null);
  const [sample, setSample] = useState<{ ask: string; document: { id: string; name: string } | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { box.current?.querySelector<HTMLElement>("h2")?.focus(); }, [part, tour]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }); // eslint-disable-line react-hooks/exhaustive-deps

  const close = () => { saveMe({ welcomed: true, tour_done: part !== "role" }).catch(() => {}); onClose(); };
  const afterRole = () => {
    if (role) saveMe({ role_id: role.id }).catch(() => {});
    setPart("tour");
  };
  const afterTour = () => {
    saveMe({ tour_done: true }).catch(() => {});
    if (canStart) setPart("first"); else close();
  };
  const go = async () => {
    setBusy(true); setErr("");
    try {
      const s = sample || await fetchSample(role?.id || "other", lang);
      const r = await startIn(currentProject(), s.ask, s.document ? [s.document.id] : [], lang, newRequestId());
      await saveMe({ welcomed: true, first_sid: r.session_id }).catch(() => {});
      onStarted(r.session_id);
    } catch { setErr(t("ob.failed.generic")); setBusy(false); }
  };
  const fallbackRole: Role = { id: "other", label: "", description: "", sample: true };

  return (
    <div className="sk-scrim sk-ob-scrim">
      <div className="sk-dialog sk-dialog-wide sk-ob-welcome" role="dialog" aria-modal="true" aria-labelledby="sk-wl-h" ref={box}>
        {part === "role" && (
          <>
            <div className="sk-ob-head">
              <div className="sk-grow">
                <div className="sk-xs sk-muted">{name ? t("wl.title", { name }) : t("wl.title.anon")}</div>
                <h2 className="sk-h2" id="sk-wl-h" tabIndex={-1}>{t("wl.role")}</h2>
              </div>
              <button className="sk-btn sk-btn-quiet sk-btn-icon" onClick={close} aria-label={t("wl.later")}><Icon name="close" /></button>
            </div>
            <p className="sk-muted">{t("wl.role.sub")}</p>
            <RolePicker value={role?.id || ""} onPick={setRole} />
            <div className="sk-ob-foot">
              <button className="sk-btn sk-btn-quiet" onClick={() => setPart("tour")}>{t("wl.skip")}</button>
              <span className="sk-grow" />
              <button className="sk-btn sk-btn-primary" onClick={afterRole} disabled={!role}><Icon name="chevron" />{t("wl.continue")}</button>
            </div>
          </>
        )}
        {part === "tour" && (
          <>
            <div className="sk-xs sk-muted">{t("wl.tour.n", { n: tour + 1, total: TOUR.length })}</div>
            <div className="sk-ob-tour">
              <span className="sk-ob-tour-ico" aria-hidden><Icon name={TOUR[tour].icon} /></span>
              <h2 className="sk-h2" id="sk-wl-h" tabIndex={-1}>{t(TOUR[tour].title)}</h2>
              <p className="sk-lede">{t(TOUR[tour].text)}</p>
            </div>
            <div className="sk-ob-dots" aria-hidden>{TOUR.map((_, i) => <i key={i} className={i === tour ? "is-on" : ""} />)}</div>
            <div className="sk-ob-foot">
              <button className="sk-btn sk-btn-quiet" onClick={afterTour}>{t("wl.skip")}</button>
              <span className="sk-grow" />
              {tour > 0 && <button className="sk-btn" onClick={() => setTour(tour - 1)}><Icon name="back" />{t("ob.back")}</button>}
              <button className="sk-btn sk-btn-primary" onClick={() => tour < TOUR.length - 1 ? setTour(tour + 1) : afterTour()}>
                <Icon name="chevron" />{t("wl.continue")}</button>
            </div>
          </>
        )}
        {part === "first" && (
          <>
            <h2 className="sk-h2" id="sk-wl-h" tabIndex={-1}>{t("wl.first.title")}</h2>
            <p className="sk-muted">{t("wl.first.text")}</p>
            <Preview role={role || fallbackRole} sample={sample} setSample={setSample} />
            {err && <div className="sk-error" role="alert"><Icon name="alert" small /><span>{err}</span></div>}
            <div className="sk-ob-foot">
              <button className="sk-btn sk-btn-quiet" onClick={close}>{t("wl.first.mine")}</button>
              <span className="sk-grow" />
              <button className="sk-btn sk-btn-primary" onClick={go} disabled={busy}>
                {busy ? <span className="sk-spin" aria-hidden /> : <Icon name="send" />}{t(busy ? "ob.first.going" : "wl.first.go")}</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
