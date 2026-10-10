import type { ApplicationUserId, SupabaseAuthUserId, ReadOnlyMembershipRepository } from '../src/modules/membercore/membership-repository.ts';

declare const authUserId: SupabaseAuthUserId;
declare const applicationId: ApplicationUserId;
declare const repository: ReadOnlyMembershipRepository;

repository.findForUserAndClub(applicationId, 'club-a');
// @ts-expect-error Auth subjects require an approved mapping to application IDs.
repository.findForUserAndClub(authUserId, 'club-a');
// @ts-expect-error Unlabelled strings must not accidentally cross the ID boundary.
repository.findForUserAndClub('raw-user-id', 'club-a');
