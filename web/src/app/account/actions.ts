'use server';

import { redirect } from 'next/navigation';
import { createClient } from '../../lib/supabase/server';
import { logout } from '../../modules/membercore/auth-flow';

export async function signOut(_previous: { message: string }, _formData: FormData): Promise<{ message: string }> {
  const client = await createClient();
  const result = await logout(client?.auth ?? null);
  if (!result.ok) return { message: result.message };
  redirect(result.destination);
}
