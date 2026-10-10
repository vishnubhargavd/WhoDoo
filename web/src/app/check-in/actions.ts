'use server';
import { getBackend } from '../../lib/backend';
import { redeemBackend } from '../../modules/gatesync/backend-tickets';
export async function redeemTicket(eventId: string, credential: string) {
  return redeemBackend(await getBackend(), eventId, credential);
}
