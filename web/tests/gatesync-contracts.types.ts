import { decideCheckIn } from '../src/modules/gatesync/check-in-decision.ts';
import type { CheckInContext } from '../src/modules/gatesync/check-in-decision.ts';
import type { CredentialResolution } from '../src/modules/gatesync/credential.ts';

declare const context: CheckInContext;
declare const resolution: CredentialResolution;
if (resolution.status !== 'unavailable') decideCheckIn(resolution, context);
// @ts-expect-error storage outages are not ticket eligibility decisions
decideCheckIn({ status: 'unavailable' }, context);
