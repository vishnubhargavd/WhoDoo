import { createBookingApi, type BookingApiResponse } from '../src/modules/fairseat/booking-api.ts';
import type { BookingRequest } from '../src/modules/fairseat/booking-contracts.ts';

const request: BookingRequest = {
  eventId: 'event-1' as BookingRequest['eventId'],
  ticketCategoryId: 'category-1' as BookingRequest['ticketCategoryId'],
  quantity: 1,
  idempotencyKey: 'retry-key-00000001'
};
const approvedApi = createBookingApi({
  reservationEndpoint: process.env.FAIRSEAT_RESERVATION_ENDPOINT,
  getAuthorizationHeader: async () => null
});
void approvedApi.reserve({ request, acceptedQuoteId: 'quote-00000001' });

const sampleResponse: BookingApiResponse = {
  kind: 'rejected', reason: 'closed', message: 'Sales are closed.'
};
void sampleResponse;
