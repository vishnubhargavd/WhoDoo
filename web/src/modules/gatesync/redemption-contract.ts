import type { RedemptionPayload } from './scanner-state.ts';

/** The exact endpoint is supplied by the approved FastAPI integration later. */
export function parseGateSyncEndpoint(value: unknown): string | null {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) return null;
    return url.toString();
  } catch { return null; }
}

export function parseRedemptionPayload(value: unknown): RedemptionPayload | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const r = value as Record<string, unknown>;
  const simple = ['invalid_credential', 'unrecognized', 'wrong_event', 'expired', 'revoked',
    'already_checked_in', 'authorization_denied', 'unavailable'] as const;
  if (simple.includes(r.status as typeof simple[number])) return { status: r.status as typeof simple[number] };
  if (r.status === 'redeemed' && typeof r.redemptionId === 'string' && r.redemptionId.length > 0
      && typeof r.eventId === 'string' && r.eventId.trim() && r.eventId.length <= 200
      && typeof r.checkedInAt === 'string' && Number.isFinite(Date.parse(r.checkedInAt))
      && new Date(r.checkedInAt).toISOString() === r.checkedInAt
      && typeof r.eventName === 'string' && r.eventName.trim() && r.eventName.length <= 160) {
    return { status: 'redeemed', redemptionId: r.redemptionId, checkedInAt: r.checkedInAt, eventId: r.eventId, eventName: r.eventName };
  }
  return null;
}
