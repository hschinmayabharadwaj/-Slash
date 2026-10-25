// Tiny Markdown subset (paragraphs, "- " lists, `code`, **bold**) rendered as
// React elements -- model output never reaches the DOM as HTML.
function inline(text) {
  return text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).map((part, i) => {
    // eslint-disable-next-line react/no-array-index-key
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) return <code key={i}>{part.slice(1, -1)}</code>;
    // eslint-disable-next-line react/no-array-index-key
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) return <strong key={i}>{part.slice(2, -2)}</strong>;
    return part;
  });
}

export default function Markdown({ text }) {
  const blocks = [];
  let list = null;
  for (const line of (text || '').split('\n')) {
    const item = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
    if (item) {
      if (!list) blocks.push((list = { type: 'list', items: [] }));
      list.items.push(item[1]);
      continue;
    }
    list = null;
    if (line.trim()) blocks.push({ type: 'p', text: line.replace(/^#+\s*/, '') });
  }
  return (
    <div className="md">
      {blocks.map((b, i) => (b.type === 'list'
        // eslint-disable-next-line react/no-array-index-key
        ? <ul key={i}>{b.items.map((it, j) => <li key={j}>{inline(it)}</li>)}</ul>
        // eslint-disable-next-line react/no-array-index-key
        : <p key={i}>{inline(b.text)}</p>))}
    </div>
  );
}
