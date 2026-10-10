'use client';

import { useState, type FormEvent } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { createClient } from '../lib/supabase/client';
import { submitEmailAuth } from '../modules/membercore/auth-flow';

export function AuthForm({ mode, destination }: { mode: 'login' | 'signup'; destination: string }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; message: string } | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const form = event.currentTarget;
    const fields = new FormData(form);
    setPending(true);
    setFeedback(null);
    try {
      const client = createClient();
      const result = await submitEmailAuth(client?.auth ?? null, mode, String(fields.get('email') ?? ''), String(fields.get('password') ?? ''), destination, window.location.origin);
      const passwordInput = form.elements.namedItem('password');
      if (passwordInput instanceof HTMLInputElement) passwordInput.value = '';
      if (result.ok && 'destination' in result) {
        router.replace(result.destination);
        router.refresh();
      } else { setFeedback({ ok: result.ok, message: result.message }); }
    } catch { setFeedback({ ok: false, message: 'Authentication is unavailable. Please try again later.' }); }
    finally { setPending(false); }
  }
  return <form onSubmit={submit} className="space-y-5" aria-busy={pending}>
    <div><label htmlFor="email" className="block text-sm font-medium">Email</label>
      <input id="email" name="email" type="email" autoComplete="email" maxLength={254} required disabled={pending} className="mt-1 w-full rounded border border-slate-300 p-3" /></div>
    <div><label htmlFor="password" className="block text-sm font-medium">Password</label>
      <input id="password" name="password" type="password" autoComplete={mode === 'signup' ? 'new-password' : 'current-password'} minLength={mode === 'signup' ? 8 : 1} maxLength={1024} required disabled={pending} aria-describedby={mode === 'signup' ? 'password-help' : undefined} className="mt-1 w-full rounded border border-slate-300 p-3" />
      {mode === 'signup' && <p id="password-help" className="mt-1 text-sm text-slate-600">At least 8 characters; the project may require a stronger password.</p>}</div>
    <button type="submit" disabled={pending} className="rounded bg-teal-700 px-5 py-3 font-medium text-white disabled:opacity-60">{pending ? 'Please wait…' : mode === 'signup' ? 'Create auth account' : 'Sign in'}</button>
    {feedback && <p role={feedback.ok ? 'status' : 'alert'} className="rounded border border-slate-300 p-3">{feedback.message}</p>}
    <p className="text-sm"><Link className="underline" href={`${mode === 'login' ? '/signup' : '/login'}?next=${encodeURIComponent(destination)}`}>{mode === 'login' ? 'Need an account? Sign up' : 'Already registered? Sign in'}</Link></p>
  </form>;
}
