/** Wordmark SOKKAN partagé (header + login) : S [icône-roue = O] K [K dégradé IA] AN.
 *  Marges/tailles en `em` → scale proprement à n'importe quelle taille de police. */
export default function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`font-baloo inline-flex select-none items-center font-extrabold leading-none tracking-tight ${className}`}>
      <span className="txt-gold">S</span>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/sokkan-icon.svg" alt="O" className="-mx-[0.12em] inline-block h-[1.12em] w-[1.12em] align-middle" />
      <span className="txt-gold">K</span>
      <span className="txt-ai">K</span>
      <span className="txt-gold">AN</span>
    </span>
  );
}
