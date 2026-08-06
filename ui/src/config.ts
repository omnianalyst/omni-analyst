export const API_BASE_URL =
  (import.meta.env.VITE_OMNI_API_URL ?? "").trim() ||
  (import.meta.env.PROD ? "" : "http://localhost:8000");
