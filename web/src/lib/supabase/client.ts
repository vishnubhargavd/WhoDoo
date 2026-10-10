'use client';

import { createBrowserClient } from '@supabase/ssr';
import { getPublicSupabaseConfiguration } from './config';

export function createClient() {
  const config = getPublicSupabaseConfiguration();
  if (config.status !== 'ready') return null;
  return createBrowserClient(config.url, config.publishableKey);
}
