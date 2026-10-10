/** @type {import('next').NextConfig} */
const nextConfig = {
  poweredByHeader: false,
  // Callback URLs carry a short-lived authorization code; suppress dev request logs.
  logging: { incomingRequests: false }
};
export default nextConfig;
