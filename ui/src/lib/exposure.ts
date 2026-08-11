import { authedSendJson, AuthRequiredError } from "./auth";
import { sendJson, authHeaderIfPresent } from "./api";

export interface ConcentrationFlag {
  ticker: string;
  total_weight: string;
  source_etfs: string[];
}

export interface OverlapPair {
  etf_a: string;
  etf_b: string;
  shared_weight: string;
}

export interface BucketExposure {
  bucket: string;
  allocation: string;
}

export interface TopHolding {
  ticker: string;
  weight: string;
}

export interface ExposureResult {
  concentration: ConcentrationFlag[];
  overlaps: OverlapPair[];
  bucket_exposure: BucketExposure[];
  top_holdings: TopHolding[];
}

export interface PositionInput {
  symbol: string;
  allocation: string;
  bucket?: string;
}

export interface OverlapRequest {
  positions: PositionInput[];
  concentration_threshold?: string;
  overlap_threshold?: string;
}

export async function postOverlap(req: OverlapRequest): Promise<ExposureResult> {
  const token = localStorage.getItem("omni.auth.token");
  if (token) {
    return authedSendJson<ExposureResult>("POST", "/exposure/overlap", req);
  }
  return sendJson<ExposureResult>("POST", "/exposure/overlap", req, authHeaderIfPresent());
}

export { AuthRequiredError };
