"use client";
// Terminal de MAINTENANCE flotte — modal xterm ↔ WS /api/fleet/term/<name>.
// Accès root sur la machine du client, réservé admin/grant : maintenance et
// incidents, pas un poste de travail. Chaque ouverture est auditée côté backend.
import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

export default function FleetTerm({ name, onClose }: { name: string; onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!box.current) return;
    const term = new Terminal({
      fontSize: 13, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
      theme: { background: "#0b0f16" }, cursorBlink: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(box.current);
    fit.fit();

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/fleet/term/${name}?cols=${term.cols}&rows=${term.rows}`);
    ws.onmessage = (e) => term.write(e.data as string);
    ws.onclose = () => term.write("\r\n\x1b[33m[session terminée]\x1b[0m\r\n");
    term.onData((d) => { if (ws.readyState === WebSocket.OPEN) ws.send(d); });

    const onResize = () => {
      fit.fit();
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ resize: [term.cols, term.rows] }));
    };
    window.addEventListener("resize", onResize);
    return () => { window.removeEventListener("resize", onResize); ws.close(); term.dispose(); };
  }, [name]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6" onClick={onClose}>
      <div className="flex h-[75vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-line bg-[#0b0f16]"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-line bg-panel px-3 py-1.5 text-[12px]">
          <span className="text-amber-300">⌨</span>
          <span className="font-medium text-slate-100">root@{name}.fleet</span>
          <span className="text-[10.5px] text-mut">maintenance — session auditée</span>
          <button onClick={onClose} className="ml-auto rounded px-1.5 text-mut hover:text-slate-200">✕</button>
        </div>
        <div ref={box} className="min-h-0 flex-1 p-1.5" />
      </div>
    </div>
  );
}
