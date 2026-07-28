/// <reference types="vite/client" />

declare module "virtual:neutron/routes" {
  export const routes: Parameters<
    typeof import("@neutron-build/core/client").registerRoutes
  >[0];
}

interface ImportMetaEnv {
  readonly VITE_OMNI_API_URL?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
