import { redirect } from 'next/navigation';
import { createClient } from '../../lib/supabase/server';
import { getScannerAccess } from '../../modules/gatesync/scanner-access.ts';
import { MobileScanner } from '../../modules/gatesync/components/mobile-scanner';

export const dynamic = 'force-dynamic';

export default async function CheckInPage() {
  const client = await createClient();
  const access = await getScannerAccess(client?.auth ?? null);
  if (access.status === 'redirect') redirect(access.destination);
  if (access.status === 'unavailable') return <section className="rounded-2xl border border-amber-200 bg-amber-50 p-6">
    <h1 className="text-2xl font-semibold">Scanner unavailable</h1><p role="alert" className="mt-3">{access.message}</p>
  </section>;

  return <div className="space-y-6">
    <div><p className="text-sm font-semibold uppercase tracking-wide text-teal-700">WhoDoo · GateSync</p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">Event check-in</h1>
      <p className="mt-3 max-w-2xl leading-7 text-slate-600">Scan a ticket credential using this device’s camera. Your account is verified, but event-staff permissions and the redemption service are not connected.</p></div>
    <aside role="status" className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm leading-6 text-amber-950">
      Scanning only captures and format-checks a code. It cannot confirm that a ticket exists or change attendance. No successful check-in will be shown without a verified server redemption response.
    </aside>
    <MobileScanner />
  </div>;
}
