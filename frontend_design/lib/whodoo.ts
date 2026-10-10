export const APP_URL = process.env.NEXT_PUBLIC_WHODOO_APP_URL || '/login'

export type Feature = {
  id: string
  name: string
  category: string
  description: string
}

export const FEATURES: Feature[] = [
  {
    id: 'membercore',
    name: 'MemberCore',
    category: 'Memberships',
    description: 'Profiles, dues, roles, renewals and member discounts in one source of truth.',
  },
  {
    id: 'fairseat',
    name: 'FairSeat',
    category: 'Tickets',
    description: 'Live ticket availability with member pricing and fair, automatic waitlists.',
  },
  {
    id: 'gatesync',
    name: 'GateSync',
    category: 'Attendance',
    description: 'QR check-in at the door with built-in duplicate prevention.',
  },
  {
    id: 'crewpulse',
    name: 'CrewPulse',
    category: 'Volunteers',
    description: 'Coordinate shifts and recognise every volunteer contribution.',
  },
  {
    id: 'trustledger',
    name: 'TrustLedger',
    category: 'Finances',
    description: 'Transparent budgets with clear, auditable approval flows.',
  },
  {
    id: 'snap-to-reimburse',
    name: 'Snap-to-Reimburse',
    category: 'Reimbursements',
    description: 'Snap a receipt. OCR drafts the claim and routes it for approval.',
  },
  {
    id: 'merchflow',
    name: 'MerchFlow',
    category: 'Merchandise',
    description: 'Take merch orders and keep inventory honest across every drop.',
  },
  {
    id: 'crewdoo-connect',
    name: 'CrewDoo Connect',
    category: 'Announcements',
    description: 'Announcements and reminders that reach exactly the right people.',
  },
  {
    id: 'event-autopilot',
    name: 'Event Autopilot',
    category: 'Events',
    description: 'Readiness monitoring that flags what an event still needs before doors open.',
  },
  {
    id: 'syncsphere',
    name: 'SyncSphere',
    category: 'Calendar',
    description: 'A conflict-aware calendar for every club, room and officer.',
  },
  {
    id: 'certidoo',
    name: 'CertiDoo',
    category: 'Certificates',
    description: 'Issue verifiable digital certificates for participation and service.',
  },
  {
    id: 'legacyvault',
    name: 'LegacyVault',
    category: 'Handover',
    description: "Pass knowledge to next year's leaders without starting from zero.",
  },
]

export const MENU_LINKS = [
  { label: 'Memberships', href: '#membercore' },
  { label: 'Events', href: '#event-autopilot' },
  { label: 'Tickets', href: '#fairseat' },
  { label: 'Volunteers', href: '#crewpulse' },
  { label: 'Finances', href: '#trustledger' },
  { label: 'Merchandise', href: '#merchflow' },
  { label: 'Calendar', href: '#syncsphere' },
  { label: 'Certificates', href: '#certidoo' },
  { label: 'Announcements', href: '#crewdoo-connect' },
  { label: 'My account', href: APP_URL },
]
