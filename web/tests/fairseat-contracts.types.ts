import type {
  ApplicationUserId,
  AuthenticatedPurchaser,
  BookingTransactionPort,
  SupabaseAuthUserId
} from '../src/modules/fairseat/booking-contracts.ts';

declare const authId: SupabaseAuthUserId;
declare const applicationId: ApplicationUserId;
const purchaser: AuthenticatedPurchaser = {
  supabaseAuthUserId: authId,
  applicationUserId: applicationId
};
void purchaser;

// These identities are distinct until a trusted server resolves the approved mapping.
// @ts-expect-error Supabase Auth IDs cannot be used as existing application user IDs.
const wrongApplicationId: ApplicationUserId = authId;
// @ts-expect-error Existing application user IDs cannot be used as Supabase Auth IDs.
const wrongAuthId: SupabaseAuthUserId = applicationId;
void wrongApplicationId;
void wrongAuthId;

declare const transactionPort: BookingTransactionPort;
void transactionPort;
