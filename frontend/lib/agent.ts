// lib/agent.ts — client WebSocket du chat SDK (Chantier B).
// Protocole : cf. CHANTIER-B.md.

export interface AgentQuestionOption { label: string; description?: string }
export interface AgentQuestion {
  question: string;
  header?: string;
  options: AgentQuestionOption[];
  multiSelect?: boolean;
}

// events serveur → client
export type AgentEvent =
  | { type: "session"; claude_session_id: string }
  | { type: "status"; state: "idle" | "working" }
  | { type: "text"; text: string }
  | { type: "thinking"; text: string }
  | { type: "tool_use"; id?: string; tool: string; title: string; input: Record<string, unknown> }
  | { type: "tool_result"; tool_use_id?: string; text: string; is_error: boolean; truncated: boolean }
  | { type: "permission"; id: string; tool: string; title: string; input: Record<string, unknown> }
  | { type: "question"; id: string; questions: AgentQuestion[] }
  | { type: "permission_resolved"; id: string }
  | { type: "question_resolved"; id: string }
  | { type: "result"; text: string; is_error: boolean; num_turns?: number; cost_usd?: number }
  | { type: "error"; message: string };

export async function createAgentSession(): Promise<string> {
  const r = await fetch("/api/agent/session", { method: "POST" });
  if (!r.ok) throw new Error(`POST /api/agent/session → ${r.status}`);
  return (await r.json()).sid as string;
}

export async function fetchAgentCommands(): Promise<{ name: string; desc: string }[]> {
  const r = await fetch("/api/agent/commands", { cache: "no-store" });
  if (!r.ok) return [];
  return r.json();
}

function wsUrl(sid: string, resume?: string | null): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const q = resume ? `?resume=${encodeURIComponent(resume)}` : "";
  return `${proto}//${location.host}/api/agent/ws/${sid}${q}`;
}

/** Connexion fine au WS agent. Reconnexion auto (backoff) tant que `closed` est faux. */
export class AgentSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private backoff = 500;

  constructor(
    private sid: string,
    private onEvent: (e: AgentEvent) => void,
    private onOpenChange: (open: boolean) => void,
    private getResume: () => string | null,
  ) {
    this.connect();
  }

  private connect() {
    if (this.closed) return;
    const ws = new WebSocket(wsUrl(this.sid, this.getResume()));
    this.ws = ws;
    ws.onopen = () => { this.backoff = 500; this.onOpenChange(true); };
    ws.onmessage = (ev) => {
      try { this.onEvent(JSON.parse(ev.data) as AgentEvent); } catch { /* ignore */ }
    };
    ws.onclose = () => {
      this.onOpenChange(false);
      if (this.closed) return;
      setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 8000);
    };
    ws.onerror = () => ws.close();
  }

  private send(obj: unknown) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  sendUser(text: string) { this.send({ type: "user", text }); }
  answerPermission(id: string, decision: "allow" | "deny", message?: string) {
    this.send({ type: "permission", id, decision, message });
  }
  answerQuestion(id: string, answers: Record<string, string | string[]>) {
    this.send({ type: "answer", id, answers });
  }
  interrupt() { this.send({ type: "interrupt" }); }

  close() { this.closed = true; this.ws?.close(); }
}
