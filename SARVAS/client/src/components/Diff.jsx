function lineClass(line) {
  if (line.startsWith('@@') || line.startsWith('---') || line.startsWith('+++') || line.startsWith('diff ')) return 'hunk';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return '';
}

export default function Diff({ text }) {
  return (
    <pre className="diff">
      {(text || '').replace(/\n$/, '').split('\n').map((line, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <span key={i} className={lineClass(line)}>{line}{'\n'}</span>
      ))}
    </pre>
  );
}

// Only ever link to GitHub (the server returns GitHub URLs).
export const safeGitHubUrl = (url) => (typeof url === 'string' && url.startsWith('https://github.com/') ? url : null);
