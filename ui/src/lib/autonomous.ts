import { authHeaderIfPresent, request } from "./api";

export interface RegimeValue {
  cycle_phase: string;
  risk_regime: string;
  inflation_regime: string;
  inflation_yoy: number;
  recession_probability: number;
  recession_assessment: string;
  policy_stance: string;
  yield_curve_inverted: boolean;
  yield_curve_spread: number | null;
  sahm_triggered: boolean;
  sahm_indicator: number | null;
  lei_negative: boolean;
  lei_change_6m: number | null;
  output_gap: number;
}

export interface RegimeResponse {
  value: RegimeValue;
  event_date: string | null;
  knowledge_date: string | null;
}

export interface SectorScore {
  rs_percentile: number;
  trend: string;
  macro_alignment: string;
  cycle_phase: string | null;
  return_window: number;
  etf_symbol: string;
}

export interface SectorEntry {
  symbol: string;
  name: string;
  score: SectorScore;
}

export const getRegime = (): Promise<RegimeResponse> =>
  request<RegimeResponse>("/autonomous/regime");

export const getSectors = (): Promise<SectorEntry[]> =>
  request<SectorEntry[]>("/autonomous/sectors", authHeaderIfPresent());
