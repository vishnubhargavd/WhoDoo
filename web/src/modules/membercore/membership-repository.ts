import type { Membership } from './ticket-discount.ts';

declare const applicationUserIdBrand: unique symbol;
declare const supabaseAuthUserIdBrand: unique symbol;

/** MemberCore-local distinction, not a new global identity contract. */
export type ApplicationUserId = string & { readonly [applicationUserIdBrand]: true };
export type SupabaseAuthUserId = string & { readonly [supabaseAuthUserIdBrand]: true };

/** Labels an existing application ID; this does NOT authenticate the caller. */
export function applicationUserId(value: string): ApplicationUserId {
  if (typeof value !== 'string' || !value || value.trim() !== value) {
    throw new TypeError('Application user ID must be a nonempty string without surrounding whitespace');
  }
  return value as ApplicationUserId;
}

export interface MembershipRecord {
  readonly applicationUserId: ApplicationUserId;
  readonly membership: Readonly<Membership>;
}

export interface ReadOnlyMembershipRepository {
  /**
   * Return the one record selected for this application user and club, or null.
   * A future adapter must resolve renewals/history deterministically under the
   * approved schema. Storage failures must reject, never masquerade as null.
   */
  findForUserAndClub(userId: ApplicationUserId, clubId: string): Promise<MembershipRecord | null>;
}

/** Supplied by trusted server code after identity mapping and authorization. */
export interface TrustedMemberContext {
  readonly applicationUserId: ApplicationUserId;
  readonly clubId: string;
}
