import { authedGetJson, authedSendJson } from "./auth";

export type WalletFamily = "evm" | "solana" | "bitcoin";
export type WalletSource = "phantom" | "metamask" | "ledger" | "manual";

export interface WalletAsset {
  symbol: string;
  amount: string;
  kind?: string;
}

export interface WalletBalance {
  assets: WalletAsset[];
  coverage: string;
}

export interface WalletAccount {
  id: string;
  address_family: WalletFamily;
  address: string;
  source: WalletSource;
  label: string;
  discovered_by: "manual" | "browser_extension";
  balance: WalletBalance | null;
  refreshed_at: string | null;
  refresh_error: string | null;
  created_at: string | null;
}

export interface WalletsResponse {
  accounts: WalletAccount[];
  security: {
    read_only: boolean;
    stores_private_keys: boolean;
    stores_seed_phrases: boolean;
  };
}

export interface AddWalletInput {
  address_family: WalletFamily;
  address: string;
  source: WalletSource;
  label: string;
  discovered_by?: "manual" | "browser_extension";
}

export const getWallets = (): Promise<WalletsResponse> =>
  authedGetJson<WalletsResponse>("/wallets");

export const addWallet = (input: AddWalletInput): Promise<WalletAccount> =>
  authedSendJson<WalletAccount>("POST", "/wallets", input);

export const renameWallet = (id: string, label: string): Promise<WalletAccount> =>
  authedSendJson<WalletAccount>("PATCH", `/wallets/${id}`, { label });

export const removeWallet = (id: string): Promise<{ removed: boolean }> =>
  authedSendJson<{ removed: boolean }>("DELETE", `/wallets/${id}`);

export const refreshWallet = (id: string): Promise<WalletAccount> =>
  authedSendJson<WalletAccount>("POST", `/wallets/${id}/refresh`);

export const refreshWallets = (): Promise<{ accounts: WalletAccount[] }> =>
  authedSendJson<{ accounts: WalletAccount[] }>("POST", "/wallets/refresh");

export function shortAddress(address: string): string {
  return address.length <= 16 ? address : `${address.slice(0, 7)}…${address.slice(-6)}`;
}

export function sourceName(source: WalletSource): string {
  return { phantom: "Phantom", metamask: "MetaMask", ledger: "Ledger", manual: "Address" }[source];
}

export function familyName(family: WalletFamily): string {
  return { evm: "Ethereum", solana: "Solana", bitcoin: "Bitcoin" }[family];
}
