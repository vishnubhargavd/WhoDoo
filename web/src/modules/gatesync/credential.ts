import { createHash } from 'node:crypto';

export const GATESYNC_CREDENTIAL_VERSION = 1 as const;
const PREFIX = 'whodoo.ticket.v1.';
const OPAQUE_TOKEN = /^[A-Za-z0-9_-]{43}$/;

/** GateSync-local IDs; no shared booking/event ID types exist yet. */
export type TicketId = string;
export type EventId = string;
export type ClubId = string;
export type CredentialDigest = string;

/** Trusted server-side credential record contract; it is not an approved SQL schema. */
export interface TicketCredentialRecord {
  readonly version: typeof GATESYNC_CREDENTIAL_VERSION;
  readonly ticketId: TicketId;
  readonly eventId: EventId;
  readonly clubId: ClubId;
  readonly issuedAt: string;
  readonly expiresAt: string;
  /** SHA-256 digest of the opaque bearer credential; never the QR token itself. */
  readonly credentialId: CredentialDigest;
  readonly state: 'issued' | 'checked_in' | 'revoked' | 'cancelled';
  readonly checkedInAt: string | null;
}

export type ParsedCredential = { readonly ok: true; readonly digest: CredentialDigest }
  | { readonly ok: false };

/** QR data is only an unpredictable bearer token. Never encode PII or ticket fields. */
export function digestOpaqueCredential(value: unknown): ParsedCredential {
  if (typeof value !== 'string' || value.length !== PREFIX.length + 43 || !value.startsWith(PREFIX)) return { ok: false };
  const token = value.slice(PREFIX.length);
  if (!OPAQUE_TOKEN.test(token)) return { ok: false };
  const bytes = Buffer.from(token, 'base64url');
  if (bytes.byteLength !== 32 || bytes.toString('base64url') !== token) return { ok: false };
  const digest = createHash('sha256').update(PREFIX, 'utf8').update(bytes).digest('hex');
  return { ok: true, digest };
}

export interface ReadOnlyCredentialRepository {
  /** Resolve a digest to server-owned booking/ticket state; never store the QR bearer token. */
  findByCredentialDigest(digest: CredentialDigest): Promise<TicketCredentialRecord | null>;
}

export type CredentialResolution =
  | { readonly status: 'resolved'; readonly record: TicketCredentialRecord }
  | { readonly status: 'invalid' }
  | { readonly status: 'unavailable' };

/** Lookup is read-only. This is not redemption, duplicate prevention or staff authorization. */
export async function resolveCredential(value: unknown, repository: ReadOnlyCredentialRepository): Promise<CredentialResolution> {
  const parsed = digestOpaqueCredential(value);
  if (!parsed.ok) return { status: 'invalid' };
  try {
    const record = await repository.findByCredentialDigest(parsed.digest);
    if (!record || record.credentialId !== parsed.digest) return { status: 'invalid' };
    return { status: 'resolved', record };
  } catch {
    return { status: 'unavailable' };
  }
}
