// Simple profile — line icons (24 px grid, 1.7 stroke, round caps), one drawing language for
// the whole profile: no emoji as an icon. Decorative by default (aria-hidden): the label next
// to it carries the meaning.
import type { ReactNode } from "react";

export type IconName =
  | "home" | "works" | "page" | "crew" | "clip" | "send" | "plus" | "close" | "menu" | "check" | "alert"
  | "chevron" | "download" | "eye" | "shield" | "search" | "pen" | "globe" | "gear" | "stop" | "back"
  | "memory" | "steps" | "seal" | "trash" | "chat" | "plan" | "summary" | "proposal" | "quiz" | "tool"
  | "upload" | "info" | "logout";

const P: Record<IconName, ReactNode> = {
  home: (<><path d="M4 11l8-7 8 7" /><path d="M6 9.5V20h12V9.5" /><path d="M10 20v-5h4v5" /></>),
  works: (<><rect x="3" y="7" width="18" height="13" rx="2.5" /><path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7M3 12h18" /></>),
  page: (<><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v4h4M9 12h6M9 16h6" /></>),
  crew: (<><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20a6.5 6.5 0 0 1 13 0" /><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5a6.5 6.5 0 0 1 3.5 5.5" /></>),
  clip: <path d="M20 11.5l-8.2 8.2a5 5 0 0 1-7.1-7.1l8.5-8.5a3.4 3.4 0 0 1 4.8 4.8l-8.4 8.4a1.8 1.8 0 0 1-2.5-2.5l7.6-7.6" />,
  send: <path d="M5 12h14M13 6l6 6-6 6" />,
  plus: <path d="M12 5v14M5 12h14" />,
  close: <path d="M6 6l12 12M18 6L6 18" />,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  check: <path d="M5 12.5l4.5 4.5L19 7" />,
  alert: (<><path d="M12 4l9 16H3z" /><path d="M12 10v4.5M12 17.5v.01" /></>),
  chevron: <path d="M9 6l6 6-6 6" />,
  download: (<><path d="M12 4v11M7.5 11l4.5 4.5 4.5-4.5" /><path d="M5 20h14" /></>),
  eye: (<><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" /><circle cx="12" cy="12" r="2.8" /></>),
  shield: (<><path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z" /><path d="M9 12l2 2 4-4" /></>),
  search: (<><circle cx="11" cy="11" r="6.5" /><path d="M20 20l-4-4" /></>),
  pen: (<><path d="M4 20l1-4.5L16.5 4a2.1 2.1 0 0 1 3 3L8 18.5z" /><path d="M14.5 6l3 3" /></>),
  globe: (<><circle cx="12" cy="12" r="8.5" /><path d="M3.5 12h17M12 3.5c2.6 2.4 2.6 14.6 0 17M12 3.5c-2.6 2.4-2.6 14.6 0 17" /></>),
  gear: (<><path d="M4 7h9M19 7h1M4 17h1M11 17h9" /><circle cx="16" cy="7" r="2.6" /><circle cx="8" cy="17" r="2.6" /></>),
  stop: <rect x="6.5" y="6.5" width="11" height="11" rx="2" />,
  back: <path d="M19 12H5M11 6l-6 6 6 6" />,
  memory: (<><path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z" /><path d="M5 17a3 3 0 0 1 3-3h11M9 8h6" /></>),
  steps: (<><path d="M9 6h11M9 12h11M9 18h11" /><path d="M4 6l1 1 1.5-2M4 12l1 1 1.5-2M4 18l1 1 1.5-2" /></>),
  seal: (<><circle cx="12" cy="10" r="6" /><path d="M9.5 10l1.8 1.8L14.8 8.3M8.5 15l-1.5 6 5-2.5 5 2.5-1.5-6" /></>),
  trash: (<><path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13M10 11v5.5M14 11v5.5" /></>),
  chat: <path d="M5 5h14v10.5H10L5.5 19.5V15.5H5z" />,
  plan: (<><rect x="4" y="5" width="16" height="15" rx="2.5" /><path d="M8 3v4M16 3v4M4 10h16M8 14h3M8 17h6" /></>),
  summary: (<><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v4h4M9 11h6M9 14.5h6M9 18h3" /></>),
  proposal: (<><path d="M4 7.5A2.5 2.5 0 0 1 6.5 5h11A2.5 2.5 0 0 1 20 7.5v9a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 16.5z" /><path d="M4.5 8l7.5 5.5L19.5 8" /></>),
  quiz: (<><circle cx="12" cy="12" r="8.5" /><path d="M9.7 9.5a2.4 2.4 0 1 1 3.4 2.2c-.700.4-1.1.9-1.1 1.8M12 16.8v.01" /></>),
  tool: (<><circle cx="12" cy="12" r="2.5" /><path d="M12 4v3M12 17v3M4 12h3M17 12h3" /></>),
  upload: (<><path d="M12 16V5M7.5 9.5L12 5l4.5 4.5" /><path d="M5 20h14" /></>),
  info: (<><circle cx="12" cy="12" r="8.5" /><path d="M12 11v5.5M12 7.8v.01" /></>),
  logout: (<><path d="M14 5H6.5A1.5 1.5 0 0 0 5 6.5v11A1.5 1.5 0 0 0 6.5 19H14" /><path d="M10 12h10M16.5 8.5L20 12l-3.5 3.5" /></>),
};

export default function Icon({ name, small, label }: { name: IconName; small?: boolean; label?: string }) {
  return (
    <svg className={small ? "sk-icon sk-icon-sm" : "sk-icon"} viewBox="0 0 24 24"
      role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true} focusable="false">
      {P[name]}
    </svg>
  );
}

/** Which icon tells an activity kind (backend/plain.py). */
export function activityIcon(kind: string): IconName {
  if (kind.startsWith("read_document") || kind === "read_draft" || kind === "read_file") return "page";
  if (kind.startsWith("search_") || kind === "web_search") return "search";
  if (kind === "write_draft" || kind === "write_file") return "pen";
  if (kind === "web_read") return "globe";
  if (kind === "read_memory" || kind === "save_memory") return "memory";
  if (kind === "plan_steps") return "steps";
  if (kind === "delegate" || kind === "agents") return "crew";
  return "tool";
}

export function templateIcon(id: string): IconName {
  return id === "training-plan" ? "plan" : id === "document-summary" ? "summary"
    : id === "client-proposal" ? "proposal" : id === "assessment-quiz" ? "quiz" : "page";
}
