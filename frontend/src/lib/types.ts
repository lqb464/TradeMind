export type ProvenanceMode = "live" | "stale" | "demo" | "unavailable" | "unknown";

export interface UserProfile {
  id?: string;
  email: string;
  name?: string;
}

export interface Candle {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ma20?: number | null;
  ma50?: number | null;
}

export interface MarketSnapshot {
  ticker: string;
  currency?: string;
  price: number;
  change: number;
  changePct: number;
  volume: number;
  candles: Candle[];
  indicators: {
    rsi?: number | null;
    macd?: number | null;
    macdSignal?: number | null;
    ma20?: number | null;
    ma50?: number | null;
  };
  provenance: {
    mode: ProvenanceMode;
    source: string;
    asOf?: string;
    message?: string;
  };
}

export interface DecisionContribution {
  name: string;
  value: number;
  detail?: string;
}

export interface RiskSizing {
  quantity?: number;
  notional?: number;
  riskAmount?: number;
  riskPct?: number;
  stopLoss?: number;
  takeProfit?: number;
  maxPositionPct?: number;
}

export interface TradeDecision {
  id?: string;
  ticker: string;
  action: string;
  score: number;
  confidence: number;
  thesis?: string;
  horizon?: string;
  contributions: DecisionContribution[];
  vetoes: string[];
  sizing: RiskSizing;
  executable: boolean;
  provenanceMode: ProvenanceMode;
  raw: Record<string, unknown>;
}

export interface Position {
  ticker: string;
  quantity: number;
  averagePrice: number;
  marketPrice?: number;
  marketValue?: number;
  unrealizedPnl?: number;
  unrealizedPnlPct?: number;
}

export interface Order {
  id: string;
  ticker: string;
  side: string;
  quantity: number;
  orderType?: string;
  status: string;
  price?: number;
  filledPrice?: number;
  createdAt?: string;
  quoteCurrency?: string;
}

export interface Proposal {
  id: string;
  ticker: string;
  side: string;
  quantity?: number;
  notional?: number;
  status: string;
  score?: number;
  confidence?: number;
  rationale?: string;
  createdAt?: string;
  vetoes: string[];
  quoteCurrency?: string;
}

export interface Portfolio {
  accountId?: string;
  currency?: string;
  cash: number;
  equity: number;
  buyingPower?: number;
  realizedPnl?: number;
  unrealizedPnl?: number;
  tradingEnabled?: boolean;
  killSwitch?: boolean;
  executionMode?: string;
  marksIncludeDemoData?: boolean;
  positions: Position[];
}

export interface EquityPoint {
  date: string;
  value: number;
}

export interface BacktestResult {
  ticker: string;
  totalReturnPct?: number;
  benchmarkReturnPct?: number;
  sharpeRatio?: number;
  maxDrawdownPct?: number;
  winRatePct?: number;
  trades?: number;
  equityCurve: EquityPoint[];
  method?: string;
  from?: string;
  to?: string;
}

export interface Citation {
  source: string;
  page?: number | null;
  chunk?: number | null;
  score?: number;
  text?: string;
}

export interface HealthStatus {
  ok: boolean;
  label: string;
  mode?: string;
  details?: Record<string, unknown>;
}

export interface LoadableError {
  title: string;
  message: string;
  status?: number;
}
