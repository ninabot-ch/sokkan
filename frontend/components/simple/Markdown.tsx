"use client";
import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** What an agent wrote, rendered with the simple profile's own typography (.sk-md). Links open
 *  in a new tab; no raw HTML is ever interpreted (react-markdown's default). */
function Markdown({ text }: { text: string }) {
  return (
    <div className="sk-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]}
        components={{ a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer noopener">{children}</a> }}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
export default memo(Markdown);
