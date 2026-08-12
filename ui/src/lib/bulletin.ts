import { authedGetJson, authedSendJson } from "./auth";

export type BulletinKind = "note" | "link";

export interface BulletinItem {
  id: string;
  kind: BulletinKind;
  title: string;
  body: string | null;
  url: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface BulletinInput {
  kind: BulletinKind;
  title: string;
  body?: string | null;
  url?: string | null;
}

export const getBulletin = (): Promise<{ items: BulletinItem[]; limit: number }> =>
  authedGetJson("/bulletin");

export const addBulletinItem = (input: BulletinInput): Promise<BulletinItem> =>
  authedSendJson("POST", "/bulletin", input);

export const updateBulletinItem = (id: string, input: BulletinInput): Promise<BulletinItem> =>
  authedSendJson("PATCH", `/bulletin/${id}`, input);

export const removeBulletinItem = (id: string): Promise<{ removed: boolean }> =>
  authedSendJson("DELETE", `/bulletin/${id}`);
