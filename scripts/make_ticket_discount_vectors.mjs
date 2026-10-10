// Runs MemberCore's TypeScript ticket-discount calculator over a fixed grid of inputs
// and writes the answers to tests/fixtures/ticket_discount_vectors.json. The Python port
// (app/services/fairseat_pricing.py) is tested against that file, so the two cannot drift.
//
//   make fairseat-vectors     (node >= 22.6; no npm install needed)
import { writeFileSync } from 'node:fs';
import { calculateTicketDiscount } from '../web/src/modules/membercore/ticket-discount.ts';

const START = '2026-10-01T00:00:00Z';
const EXPIRY = '2026-11-01T00:00:00Z';
const at = (iso) => Date.parse(iso);
const member = (changes = {}) => ({ status: 'active', clubId: 'club-a', startsAt: START, expiresAt: EXPIRY, discountBps: 1500, ...changes });

const prices = [0, 1, 2, 3, 7, 49, 50, 51, 99, 100, 101, 333, 9999, 10000, 12345, 79900, 99999, 100001, 123456789, Number.MAX_SAFE_INTEGER];
const rates = [0, 1, 49, 50, 51, 333, 1500, 2500, 3333, 5000, 6667, 9999, 10000];
const inputs = [];

// Every price against every rate, for a membership that is plainly valid: this is the rounding.
for (const basePricePaise of prices) for (const discountBps of rates) {
  inputs.push({ basePricePaise, eventClubId: 'club-a', membership: member({ discountBps }), nowMs: at('2026-10-10T12:00:00Z') });
}
// Eligibility: each way a membership can fail to apply, and the edges of its validity.
const cases = [
  null,
  member({ status: 'cancelled' }),
  member({ status: 'expired' }),
  member({ clubId: 'club-b' }),
  member({ discountBps: 0 }),
  member(),
];
const moments = ['2026-09-30T23:59:59.999Z', '2026-10-01T00:00:00.000Z', '2026-10-01T00:00:00.001Z',
  '2026-10-31T23:59:59.999Z', '2026-11-01T00:00:00.000Z', '2026-11-01T00:00:00.001Z'];
for (const membership of cases) for (const moment of moments) for (const basePricePaise of [0, 333, 79900]) {
  inputs.push({ basePricePaise, eventClubId: 'club-a', membership, nowMs: at(moment) });
}
// Inputs both sides must refuse.
const now = at('2026-10-10T12:00:00Z');
inputs.push(
  { basePricePaise: -1, eventClubId: 'club-a', membership: null, nowMs: now },
  { basePricePaise: 10.5, eventClubId: 'club-a', membership: null, nowMs: now },
  { basePricePaise: Number.MAX_SAFE_INTEGER + 1, eventClubId: 'club-a', membership: null, nowMs: now },
  { basePricePaise: 100, eventClubId: '', membership: null, nowMs: now },
  { basePricePaise: 100, eventClubId: ' club-a', membership: null, nowMs: now },
  { basePricePaise: 100, eventClubId: 'club-a', membership: member({ discountBps: 10001 }), nowMs: now },
  { basePricePaise: 100, eventClubId: 'club-a', membership: member({ discountBps: -1 }), nowMs: now },
  { basePricePaise: 100, eventClubId: 'club-a', membership: member({ status: 'pending' }), nowMs: now },
  { basePricePaise: 100, eventClubId: 'club-a', membership: member({ clubId: '' }), nowMs: now },
  { basePricePaise: 100, eventClubId: 'club-a', membership: member({ expiresAt: START }), nowMs: now },
  { basePricePaise: 100, eventClubId: 'club-a', membership: member({ startsAt: EXPIRY, expiresAt: START }), nowMs: now },
);

const vectors = inputs.map((input) => {
  // Membership times are also given in milliseconds, which is what the Python port takes.
  const membership = input.membership && { ...input.membership, startsAtMs: at(input.membership.startsAt), expiresAtMs: at(input.membership.expiresAt) };
  try {
    return { input: { ...input, membership }, result: calculateTicketDiscount(input) };
  } catch (error) {
    return { input: { ...input, membership }, error: error.constructor.name };
  }
});

const target = new URL('../tests/fixtures/ticket_discount_vectors.json', import.meta.url);
writeFileSync(target, JSON.stringify({ source: 'web/src/modules/membercore/ticket-discount.ts', vectors }, null, 0) + '\n');
console.log(`${vectors.length} vectors written (${vectors.filter((v) => v.error).length} rejected inputs)`);
