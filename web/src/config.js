export const USE_SAMPLE_DATA = false
// Set VITE_API_BASE_URL (e.g. on Vercel) to the backend's URL, without a trailing slash. Read at build time.
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
export const CLAIM_ID = 'HO-48213'
// Claims the workspace can switch between: Maria (the live claim) and Park (the past claim the rulebook learns from).
export const CLAIMS = [{ id: 'HO-48213', label: 'Maria · HO-48213' }, { id: 'PK-20719', label: 'Park · PK-20719' }]
