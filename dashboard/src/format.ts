// Display helpers. "unknown"/"n/a" mirror the CLI's honest unknowns — the
// UI never guesses a number the store did not provide.

export function shortHash(value: string | null | undefined, len = 12): string {
  if (!value) return 'n/a';
  return value.length <= len ? value : `${value.slice(0, len)}\u2026`;
}

export function fmtCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'unknown';
  return `$${value.toFixed(6)}`;
}

export function fmtSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'unknown';
  return `${Math.round(value)}s`;
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return 'n/a';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
}

export function fmtPct(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'n/a';
  return `${(value * 100).toFixed(1)}%`;
}

export function fmtTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'n/a';
  return value.toLocaleString('en-US');
}
