import 'server-only';
import { isOpaqueCredentialFormat } from './credential-format.ts';
import { parseGateSyncEndpoint, parseRedemptionPayload } from './redemption-contract.ts';
import type { RedemptionPayload } from './scanner-state.ts';

export interface GateSyncRedemptionApi {
  readonly enabled: boolean;
  redeem(credential: string, expectedEventId: string, verifiedOperatorAccessToken: string): Promise<RedemptionPayload | null>;
}

/**
 * Future FastAPI handoff. Disabled unless an approved HTTPS endpoint is set.
 * Call only on a server after verifyAccount/getUser and operator authorization.
 * No endpoint/path is assumed here; raw credentials are sent only in the POST body.
 */
export function createGateSyncRedemptionApi(
  endpointValue: unknown = process.env.GATESYNC_REDEMPTION_URL,
  transport: typeof fetch = fetch,
): GateSyncRedemptionApi {
  const url = parseGateSyncEndpoint(endpointValue);
  if (!url) return { enabled: false, async redeem() { return null; } };
  return {
    enabled: true,
    async redeem(credential, expectedEventId, verifiedOperatorAccessToken) {
      if (!isOpaqueCredentialFormat(credential) || !expectedEventId.trim()
          || !verifiedOperatorAccessToken.trim()) return null;
      try {
        const response = await transport(url, {
          method: 'POST',
          headers: { 'content-type': 'application/json', authorization: `Bearer ${verifiedOperatorAccessToken}` },
          body: JSON.stringify({ credential, expectedEventId }),
          cache: 'no-store',
          redirect: 'error',
        });
        if (!response.ok) return null;
        return parseRedemptionPayload(await response.json());
      } catch { return null; }
    }
  };
}
