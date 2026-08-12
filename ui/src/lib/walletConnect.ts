import type { WalletFamily } from "./wallets";

interface RequestProvider {
  request(args: { method: string; params?: unknown[] }): Promise<unknown>;
}

interface Eip6963Detail {
  info: { name: string; rdns: string };
  provider: RequestProvider;
}

interface PhantomSolanaProvider {
  isPhantom?: boolean;
  connect(): Promise<{ publicKey: { toString(): string } }>;
}

type BrowserWalletWindow = Window & {
  ethereum?: RequestProvider;
  phantom?: {
    solana?: PhantomSolanaProvider;
    ethereum?: RequestProvider;
  };
};

export interface DiscoveredAddress {
  family: WalletFamily;
  address: string;
}

function addresses(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

async function findMetaMask(): Promise<RequestProvider | null> {
  const browser = window as BrowserWalletWindow;
  const announced: Eip6963Detail[] = [];
  const listener = (event: Event) => {
    const detail = (event as CustomEvent<Eip6963Detail>).detail;
    if (detail?.provider) announced.push(detail);
  };
  window.addEventListener("eip6963:announceProvider", listener);
  window.dispatchEvent(new Event("eip6963:requestProvider"));
  await new Promise((resolve) => window.setTimeout(resolve, 100));
  window.removeEventListener("eip6963:announceProvider", listener);
  return announced.find((item) => item.info.rdns.toLowerCase().includes("metamask"))?.provider
    ?? browser.ethereum
    ?? null;
}

export async function connectMetaMask(): Promise<DiscoveredAddress[]> {
  const provider = await findMetaMask();
  if (!provider) throw new Error("MetaMask was not found in this browser.");
  const accounts = addresses(await provider.request({ method: "eth_requestAccounts" }));
  if (!accounts.length) throw new Error("MetaMask did not share an account.");
  return accounts.map((address) => ({ family: "evm", address }));
}

export async function connectPhantom(): Promise<DiscoveredAddress[]> {
  const phantom = (window as BrowserWalletWindow).phantom;
  if (!phantom?.solana && !phantom?.ethereum) {
    throw new Error("Phantom was not found in this browser.");
  }
  const found: DiscoveredAddress[] = [];
  if (phantom.solana) {
    const result = await phantom.solana.connect();
    found.push({ family: "solana", address: result.publicKey.toString() });
  }
  if (phantom.ethereum) {
    const accounts = addresses(await phantom.ethereum.request({ method: "eth_requestAccounts" }));
    found.push(...accounts.map((address): DiscoveredAddress => ({ family: "evm", address })));
  }
  return found;
}
