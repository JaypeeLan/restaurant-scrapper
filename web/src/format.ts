/** Small formatting helpers shared across views. */

export function compactNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  if (Math.abs(n) < 1000) return String(n);
  return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(n);
}

export function fullNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  return new Intl.NumberFormat('en').format(n);
}

const DAY = 86_400_000;

/** "3h ago", "in 4h", "just now". Returns "—" for missing timestamps. */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';

  const diff = then - Date.now();
  const abs = Math.abs(diff);
  if (abs < 60_000) return 'just now';

  const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ['day', DAY],
    ['hour', 3_600_000],
    ['minute', 60_000],
  ];
  for (const [unit, ms] of units) {
    if (abs >= ms) return rtf.format(Math.round(diff / ms), unit);
  }
  return 'just now';
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('en', { month: 'short', day: 'numeric' });
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('en', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function isOverdue(iso: string | null | undefined): boolean {
  if (!iso) return false;
  const t = new Date(iso).getTime();
  return !Number.isNaN(t) && t < Date.now();
}

export function sourceLabel(name: string): string {
  switch (name) {
    case 'graph':
      return 'Graph';
    case 'web_json':
      return 'Scraped';
    case 'embed':
      return 'Embed';
    default:
      return name;
  }
}
