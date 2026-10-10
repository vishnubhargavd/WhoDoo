import {
  applicationUserId,
  type ApplicationUserId,
  type SupabaseAuthUserId,
} from './membership-repository.ts';
import { verifyAccount, type AuthPort } from './auth-flow.ts';

/** Roles are read from the existing application row, never from Auth metadata. */
export type ExistingApplicationRole = 'president' | 'treasurer' | 'event_lead' | 'volunteer' | 'member';

export type TrustedApplicationIdentity = {
  readonly supabaseAuthUserId: SupabaseAuthUserId;
  readonly applicationUserId: ApplicationUserId;
  readonly clubId: string;
  readonly role: ExistingApplicationRole;
};

/** Adapter must query by auth_user_id only; do not fall back to email matching. */
export interface ReadOnlyApplicationUserMappingRepository {
  findBySupabaseAuthUserId(authUserId: SupabaseAuthUserId): Promise<unknown | null>;
}

export type ApplicationIdentityResult =
  | { readonly status: 'authenticated-unmapped' }
  | { readonly status: 'unauthenticated'; readonly destination: string }
  | { readonly status: 'unavailable' }
  | { readonly status: 'mapped'; readonly identity: TrustedApplicationIdentity };

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const roles = new Set<ExistingApplicationRole>(['president', 'treasurer', 'event_lead', 'volunteer', 'member']);

function parseMapping(authUserId: string, value: unknown): TrustedApplicationIdentity | null {
  if (!value || typeof value !== 'object') return null;
  const row = value as Record<string, unknown>;
  if (typeof row.auth_user_id !== 'string' || row.auth_user_id !== authUserId || !uuid.test(row.auth_user_id)) return null;
  if (typeof row.id !== 'string' || !uuid.test(row.id)) return null;
  if (typeof row.club_id !== 'string' || !uuid.test(row.club_id)) return null;
  if (typeof row.role !== 'string' || !roles.has(row.role as ExistingApplicationRole)) return null;

  return {
    supabaseAuthUserId: authUserId as SupabaseAuthUserId,
    applicationUserId: applicationUserId(row.id),
    clubId: row.club_id,
    role: row.role as ExistingApplicationRole,
  };
}

/**
 * Maps a server-verified Auth identity to the existing application user row.
 * This is a contract/service only: it makes no database, role, or schema claim.
 */
export async function resolveApplicationIdentity(
  auth: AuthPort | null,
  repository: ReadOnlyApplicationUserMappingRepository,
): Promise<ApplicationIdentityResult> {
  const verified = await verifyAccount(auth);
  if (verified.status === 'login') return { status: 'unauthenticated', destination: verified.destination };
  if (verified.status !== 'authenticated' || !uuid.test(verified.authUserId)) return { status: 'unavailable' };

  const authUserId = verified.authUserId as SupabaseAuthUserId;
  try {
    const row = await repository.findBySupabaseAuthUserId(authUserId);
    if (row === null) return { status: 'authenticated-unmapped' };
    const identity = parseMapping(verified.authUserId, row);
    return identity ? { status: 'mapped', identity } : { status: 'unavailable' };
  } catch {
    return { status: 'unavailable' };
  }
}
