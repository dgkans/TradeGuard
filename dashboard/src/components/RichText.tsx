import type { ReactNode } from "react";

/** Inline **bold**; any other asterisks are dropped so no markdown symbols show. */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  parts.forEach((part, index) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      out.push(<strong key={index}>{part.slice(2, -2)}</strong>);
    } else if (part) {
      out.push(part.replace(/\*\*|__|`/g, ""));
    }
  });
  return out;
}

const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/;

/** The agent's answer as simple text: paragraphs, bullet lists and bold. No raw HTML. */
export function RichText({ text }: { text: string }) {
  const blocks = text.replace(/\r/g, "").split(/\n\s*\n/);
  return (
    <>
      {blocks.map((block, index) => {
        const lines = block.split("\n").filter((line) => line.trim());
        if (lines.length > 0 && lines.every((line) => BULLET.test(line))) {
          return (
            <ul key={index}>
              {lines.map((line, item) => (
                <li key={item}>{inline(line.replace(BULLET, "").replace(/^#+\s*/, ""))}</li>
              ))}
            </ul>
          );
        }
        return (
          <p key={index}>
            {lines.map((line, item) => (
              <span key={item}>
                {item > 0 ? <br /> : null}
                {inline(line.replace(/^#+\s*/, ""))}
              </span>
            ))}
          </p>
        );
      })}
    </>
  );
}
