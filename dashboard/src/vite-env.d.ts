/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the TradeGuard API. Defaults to "/api" (the dev server proxy). */
  readonly VITE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
