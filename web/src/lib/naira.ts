/**
 * Nigerian restaurant menus — prices are always Naira (NGN).
 * Stored values under 1000 are usually OCR shorthand (32 → ₦32,000).
 */
export function normalizeNairaAmount(price: number | null | undefined): number | null {
  const n = Number(price ?? 0);
  if (!n || n <= 0) return null;
  if (n >= 1000) return n;
  if (n <= 300) return n * 1000;
  return n;
}

export function formatNaira(price: number | null | undefined): string {
  const n = normalizeNairaAmount(price);
  if (!n) return '—';
  return `₦${n.toLocaleString('en-NG')}`;
}
