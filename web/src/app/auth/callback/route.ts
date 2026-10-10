import { NextResponse, type NextRequest } from 'next/server';
import { createClient } from '../../../lib/supabase/server';
import { confirmEmail } from '../../../modules/membercore/auth-flow';

export async function GET(request: NextRequest) {
  const client = await createClient();
  const destination = await confirmEmail(client?.auth ?? null, request.nextUrl.searchParams.get('code'), request.nextUrl.searchParams.get('next'));
  // Relative Location avoids trusting request Host/forwarded-host for redirects.
  // Never echo or log the callback code or upstream error descriptions.
  return new NextResponse(null, { status: 303, headers: {
    Location: destination, 'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer'
  } });
}
