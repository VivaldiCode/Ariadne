import { Fragment, type ReactNode } from "react";

/** Markdown mínimo e seguro (a saída do `read`): títulos, listas, parágrafos, **negrito**, *itálico*
 *  e `código`. Tudo vira elementos React: nunca HTML cru. */
export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  let paragraph: string[] = [];
  let list: string[] = [];
  let key = 0;

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push(<p key={key++}>{inline(paragraph.join(" "))}</p>);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list.length) {
      blocks.push(
        <ul key={key++} style={{ paddingLeft: "1.25rem", listStyle: "disc" }}>
          {list.map((item, i) => (
            <li key={i}>{inline(item)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      const level = Math.min(heading[1].length + 1, 4);
      const Tag = `h${level}` as "h2" | "h3" | "h4";
      blocks.push(<Tag key={key++}>{inline(heading[2])}</Tag>);
    } else if (bullet) {
      flushParagraph();
      list.push(bullet[1]);
    } else if (line.trim() === "") {
      flushParagraph();
      flushList();
    } else {
      flushList();
      paragraph.push(line.trim());
    }
  }
  flushParagraph();
  flushList();
  return <div className="article-text">{blocks}</div>;
}

function inline(text: string): ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) return <code key={i}>{part.slice(1, -1)}</code>;
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) return <em key={i}>{part.slice(1, -1)}</em>;
    return <Fragment key={i}>{part}</Fragment>;
  });
}
