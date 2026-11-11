"use client";
// Feature `study_mode` — the permanent banner. Standalone: it reads the study state itself
// (lib/study.ts) and renders nothing when the feature is off or no study is active. It cannot
// be dismissed: while actions are recorded, the screen says so.
import { useStudy } from "@/lib/study";
import { TEXT, fill, fmtDate, useLang } from "@/lib/studyText";

export default function StudyBanner({ onOpen }: { onOpen: (section?: string) => void }) {
  const s = useStudy();
  const [lang] = useLang();
  if (!s || !s.enabled || !s.active) return null;
  const t = TEXT[lang];
  const open = (section?: string) => (e: React.MouseEvent) => { e.preventDefault(); onOpen(section); };
  const consented = !!s.consent?.current;
  const pending = s.pending_feedback || 0;
  return (
    <div role="status" aria-label={t.title} data-study-banner
      className={`relative z-30 flex flex-wrap items-center gap-x-2.5 gap-y-1 border-b px-3 py-1.5 text-[12.5px] md:px-4 ${consented
        ? "border-line bg-panel2/70 text-slate-300"
        : "border-brass/40 bg-brass/10 text-amber-100"}`}>
      <span aria-hidden className={`h-2 w-2 shrink-0 rounded-full ${consented ? "bg-emerald-400" : "bg-brass"}`} />
      <span className="font-medium text-slate-100">
        {fill(t.banner_on, { study: s.study_id || "" })}
        {s.expires_at ? <span className="font-normal text-slate-300"> {fill(t.banner_until, { date: fmtDate(s.expires_at, lang) })}</span> : null}
      </span>
      {consented ? (
        <span>· {fill(t.banner_as, { p: s.pseudonym || "" })}</span>
      ) : (
        <span>· {t.banner_ask}</span>
      )}
      {s.notice && s.days_left !== null && s.days_left !== undefined && (
        <span className="rounded-full border border-amber-400/60 bg-amber-500/10 px-2 py-0.5 text-[12px] font-medium text-amber-100">
          {fill(t.banner_notice, { n: s.days_left })}
        </span>
      )}
      {consented && pending > 0 && (
        <a href="/?plane=setup&tab=study&section=tasks" onClick={open("tasks")}
          className="ui-focus rounded-full border border-brass/50 px-2 py-0.5 text-[12px] text-amber-100 hover:bg-brass/15">
          {pending === 1 ? t.banner_answer_one : fill(t.banner_answer_many, { n: pending })}
        </a>
      )}
      <a href="/?plane=setup&tab=study" onClick={open(consented ? "recorded" : "consent")}
        className={`ui-focus ml-auto shrink-0 rounded px-1 underline underline-offset-2 hover:text-white ${consented ? "text-slate-200" : "font-medium text-amber-50"}`}>
        {consented ? t.banner_link : t.banner_decide}
      </a>
    </div>
  );
}
