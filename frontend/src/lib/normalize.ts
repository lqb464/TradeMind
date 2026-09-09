import type {
  BacktestResult,
  Candle,
  Citation,
  DecisionContribution,
  HealthStatus,
  MarketSnapshot,
  Order,
  Portfolio,
  Position,
  Proposal,
  ProvenanceMode,
  RiskSizing,
  TradeDecision,
  UserProfile,
} from "./types";

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function first(record: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key];
  }
  return undefined;
}

export function stringValue(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

export function numberValue(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function optionalNumber(value: unknown): number | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  const parsed = numberValue(value, Number.NaN);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function optionalBoolean(value: unknown): boolean | undefined {
  if (typeof value === "boolean") return value;
  if (typeof value === "number" && (value === 0 || value === 1)) return value === 1;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["true", "1", "yes", "on"].includes(normalized)) return true;
    if (["false", "0", "no", "off"].includes(normalized)) return false;
  }
  return undefined;
}

function ratio(value: unknown): number {
  const parsed = numberValue(value, 0);
  return parsed > 1 ? Math.min(1, parsed / 100) : Math.max(0, parsed);
}

function modeValue(meta: Record<string, unknown>): ProvenanceMode {
  if (optionalBoolean(meta.is_demo) === true || optionalBoolean(meta.synthetic) === true) return "demo";
  if (optionalBoolean(meta.is_stale) === true || optionalBoolean(meta.stale) === true) return "stale";
  const explicit = stringValue(first(meta, ["mode", "status", "data_mode"])).toLowerCase();
  if (["live", "realtime", "real"].includes(explicit)) return "live";
  if (["stale", "delayed", "cached-stale"].includes(explicit)) return "stale";
  if (["demo", "synthetic", "fallback", "deterministic-demo"].includes(explicit)) return "demo";
  if (["unavailable", "offline", "error"].includes(explicit)) return "unavailable";
  const source = stringValue(meta.source).toLowerCase();
  if (source.includes("demo") || source.includes("synthetic") || source.includes("fallback")) return "demo";
  return source ? "live" : "unknown";
}

export function normalizeUser(payload: unknown, fallbackEmail = ""): UserProfile {
  const root = asRecord(payload);
  const record = asRecord(root.user ?? root.profile ?? root);
  return {
    id: stringValue(first(record, ["id", "user_id"])) || undefined,
    email: stringValue(first(record, ["email", "username"]), fallbackEmail),
    name: stringValue(first(record, ["name", "full_name", "display_name"])) || undefined,
  };
}

function normalizeCandle(value: unknown): Candle | null {
  const row = asRecord(value);
  const date = stringValue(first(row, ["date", "timestamp", "time"]));
  const close = optionalNumber(first(row, ["close", "price", "value"]));
  if (!date || close === undefined) return null;
  const open = optionalNumber(row.open) ?? close;
  return {
    date,
    open,
    high: optionalNumber(row.high) ?? Math.max(open, close),
    low: optionalNumber(row.low) ?? Math.min(open, close),
    close,
    volume: numberValue(row.volume),
    ma20: optionalNumber(first(row, ["ma20", "sma20", "sma_20"])) ?? null,
    ma50: optionalNumber(first(row, ["ma50", "sma50", "sma_50"])) ?? null,
  };
}

export function normalizeMarket(payload: unknown, requestedTicker: string): MarketSnapshot {
  const root = asRecord(payload);
  const quote = asRecord(root.quote ?? root.market ?? root);
  const indicators = asRecord(root.indicators ?? quote.indicators);
  const meta = asRecord(root.meta ?? root.provenance);
  const candles = asArray(first(root, ["candles", "history", "prices"]))
    .map(normalizeCandle)
    .filter((item): item is Candle => item !== null);
  return {
    ticker: stringValue(first(root, ["ticker", "symbol"]), requestedTicker).toUpperCase(),
    currency: stringValue(first(root, ["currency", "quote_currency"]))
      || stringValue(meta.currency)
      || undefined,
    price: numberValue(first(quote, ["price", "close", "last"])),
    change: numberValue(first(quote, ["change", "price_change"])),
    changePct: numberValue(first(quote, ["change_pct", "change_percent", "percent_change"])),
    volume: numberValue(first(quote, ["volume", "last_volume"])),
    candles,
    indicators: {
      rsi: optionalNumber(first(indicators, ["rsi", "rsi_14"])),
      macd: optionalNumber(indicators.macd),
      macdSignal: optionalNumber(first(indicators, ["macd_signal", "signal"])),
      ma20: optionalNumber(first(indicators, ["ma20", "sma20", "sma_20"])),
      ma50: optionalNumber(first(indicators, ["ma50", "sma50", "sma_50"])),
    },
    provenance: {
      mode: modeValue(meta),
      source: stringValue(meta.source, "Không rõ nguồn"),
      asOf: stringValue(first(meta, ["as_of", "timestamp", "updated_at"])) || undefined,
      message: stringValue(first(meta, ["message", "warning", "reason", "fallback_reason"])) || undefined,
    },
  };
}

function contributionList(value: unknown): DecisionContribution[] {
  if (Array.isArray(value)) {
    return value.map((item, index) => {
      const row = asRecord(item);
      return {
        name: stringValue(first(row, ["name", "feature", "factor", "label"]), `Yếu tố ${index + 1}`),
        value: numberValue(first(row, ["weighted_score", "value", "contribution", "raw_score", "score", "impact"])),
        detail: stringValue(first(row, ["rationale", "detail", "reason", "description"])) || undefined,
      };
    });
  }
  const record = asRecord(value);
  return Object.entries(record).map(([name, raw]) => {
    const row = asRecord(raw);
    return {
      name,
      value: typeof raw === "object"
        ? numberValue(first(row, ["weighted_score", "value", "contribution", "raw_score", "score", "impact"]))
        : numberValue(raw),
      detail: stringValue(first(row, ["rationale", "detail", "reason"])) || undefined,
    };
  });
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => {
      const row = asRecord(item);
      return typeof item === "string"
        ? item
        : stringValue(first(row, ["reason", "message", "code", "name"]));
    }).filter(Boolean);
  }
  if (typeof value === "string" && value) return [value];
  return [];
}

export function normalizeDecision(payload: unknown, ticker: string): TradeDecision {
  const root = asRecord(payload);
  const decision = asRecord(root.decision ?? root);
  const risk = asRecord(
    decision.position_size
      ?? decision.sizing
      ?? decision.position_sizing
      ?? root.position_size
      ?? root.risk_sizing
      ?? root.risk,
  );
  const quality = asRecord(decision.market_quality ?? root.market_quality);
  const qualityProvenance = asRecord(quality.provenance);
  const freshness = asRecord(quality.freshness);
  const rootMeta = asRecord(root.meta ?? root.provenance);
  const isDemo = [rootMeta.is_demo, rootMeta.synthetic, qualityProvenance.is_demo,
    qualityProvenance.declared_demo, qualityProvenance.inferred_demo]
    .some((value) => optionalBoolean(value) === true);
  const isStale = [rootMeta.is_stale, rootMeta.stale, freshness.stale]
    .some((value) => optionalBoolean(value) === true);
  const meta = {
    ...qualityProvenance,
    ...rootMeta,
    is_demo: isDemo,
    is_stale: isStale,
  };
  const vetoes = stringList(first(decision, ["vetoes", "veto_reasons", "risk_vetoes"]))
    .concat(stringList(first(root, ["vetoes", "veto_reasons"])));
  const action = stringValue(first(decision, ["action", "signal", "side", "recommendation"]), "HOLD").toUpperCase();
  const executableValue = first(decision, ["execution_eligible", "executable", "can_trade", "approved_by_risk"]);
  const executable = optionalBoolean(executableValue);
  return {
    id: stringValue(first(decision, ["id", "decision_id"])) || undefined,
    ticker: stringValue(first(decision, ["ticker", "symbol"]), ticker).toUpperCase(),
    action,
    score: numberValue(first(decision, ["score", "signal_score", "composite_score"])),
    confidence: ratio(first(decision, ["confidence", "confidence_score", "probability"])),
    thesis: stringValue(first(decision, ["thesis", "rationale", "summary", "reasoning"])) || undefined,
    horizon: stringValue(first(decision, ["horizon", "time_horizon"])) || undefined,
    contributions: contributionList(first(decision, ["contributions", "factors", "factor_contributions", "signals"])),
    vetoes: Array.from(new Set(vetoes)),
    sizing: {
      quantity: optionalNumber(first(risk, ["quantity", "shares", "units"])),
      notional: optionalNumber(first(risk, ["notional", "position_value", "amount"])),
      riskAmount: optionalNumber(first(risk, ["estimated_risk", "risk_amount", "amount_at_risk"])),
      riskPct: optionalNumber(first(risk, ["risk_pct", "risk_percent", "portfolio_risk_pct"])),
      stopLoss: optionalNumber(first(risk, ["stop_loss", "stop_price"])),
      takeProfit: optionalNumber(first(risk, ["take_profit", "target_price"])),
      maxPositionPct: optionalNumber(first(risk, ["max_position_pct", "position_pct"])),
    },
    executable: executable ?? (vetoes.length === 0 && !["HOLD", "OBSERVE", "VETO"].includes(action)),
    provenanceMode: modeValue(meta),
    raw: root,
  };
}

function normalizePosition(value: unknown): Position {
  const row = asRecord(value);
  const quantity = numberValue(first(row, ["quantity", "shares", "units"]));
  const averagePrice = numberValue(first(row, ["average_price", "average_cost", "avg_price", "cost_basis"]));
  const marketPrice = optionalNumber(first(row, ["market_price", "current_price", "price"]));
  const marketValue = optionalNumber(first(row, ["market_value", "value"]))
    ?? (marketPrice === undefined ? undefined : quantity * marketPrice);
  const unrealizedPnl = optionalNumber(first(row, ["unrealized_pnl", "pnl"]))
    ?? (marketPrice === undefined ? undefined : (marketPrice - averagePrice) * quantity);
  const unrealizedPnlPct = optionalNumber(first(row, ["unrealized_pnl_pct", "pnl_pct"]))
    ?? (averagePrice > 0 && marketPrice !== undefined ? ((marketPrice / averagePrice) - 1) * 100 : undefined);
  return {
    ticker: stringValue(first(row, ["ticker", "symbol"]), "—").toUpperCase(),
    quantity,
    averagePrice,
    marketPrice,
    marketValue,
    unrealizedPnl,
    unrealizedPnlPct,
  };
}

export function normalizePortfolio(payload: unknown): Portfolio {
  const root = asRecord(payload);
  const account = asRecord(root.account ?? root.summary ?? root);
  const positions = asArray(root.positions ?? account.positions).map(normalizePosition);
  const cash = numberValue(first(account, ["cash", "cash_balance", "available_cash"]));
  return {
    accountId: stringValue(first(account, ["id", "account_id"])) || undefined,
    currency: stringValue(first(account, ["currency", "account_currency"])) || undefined,
    cash,
    equity: numberValue(first(account, ["equity", "total_equity", "portfolio_value"])),
    buyingPower: optionalNumber(first(account, ["buying_power", "available_funds"])) ?? cash,
    realizedPnl: optionalNumber(first(account, ["realized_pnl", "realized_profit_loss"])),
    unrealizedPnl: optionalNumber(first(account, ["unrealized_pnl", "unrealized_profit_loss"])),
    tradingEnabled: optionalBoolean(first(account, ["trading_enabled", "tradingEnabled"])),
    killSwitch: optionalBoolean(first(account, ["kill_switch", "killSwitch"])),
    executionMode: stringValue(first(account, ["execution_mode", "mode"])) || undefined,
    marksIncludeDemoData: optionalBoolean(first(account, ["marks_include_demo_data", "marksIncludeDemoData"])),
    positions,
  };
}

export function normalizeOrders(payload: unknown): Order[] {
  const root = asRecord(payload);
  return asArray(root.items ?? root.orders ?? payload).map((item, index) => {
    const row = asRecord(item);
    return {
      id: stringValue(first(row, ["id", "order_id"]), `order-${index}`),
      ticker: stringValue(first(row, ["ticker", "symbol"]), "—").toUpperCase(),
      side: stringValue(first(row, ["side", "action"]), "—").toUpperCase(),
      quantity: numberValue(first(row, ["quantity", "shares", "units"])),
      orderType: stringValue(first(row, ["order_type", "type"])) || undefined,
      status: stringValue(row.status, "unknown").toUpperCase(),
      price: optionalNumber(first(row, ["price", "limit_price", "reference_price"])),
      filledPrice: optionalNumber(first(row, ["fill_price", "filled_price", "average_fill_price"])),
      createdAt: stringValue(first(row, ["created_at", "submitted_at", "timestamp"])) || undefined,
      quoteCurrency: stringValue(first(row, ["quote_currency", "currency"])) || undefined,
    };
  });
}

export function normalizeProposals(payload: unknown): Proposal[] {
  const root = asRecord(payload);
  return asArray(root.items ?? root.proposals ?? payload).map((item, index) => {
    const row = asRecord(item);
    const decision = asRecord(row.decision);
    const quantity = optionalNumber(first(row, ["quantity", "shares", "units"]));
    const referencePrice = optionalNumber(first(row, ["reference_price", "price", "entry_price"]));
    return {
      id: stringValue(first(row, ["id", "proposal_id"]), `proposal-${index}`),
      ticker: stringValue(first(row, ["ticker", "symbol"]), stringValue(decision.ticker, "—")).toUpperCase(),
      side: stringValue(first(row, ["side", "action"]), stringValue(decision.action, "HOLD")).toUpperCase(),
      quantity,
      notional: optionalNumber(first(row, ["notional", "amount", "position_value"]))
        ?? (quantity !== undefined && referencePrice !== undefined ? quantity * referencePrice : undefined),
      status: stringValue(row.status, "PENDING").toUpperCase(),
      score: optionalNumber(first(row, ["score", "signal_score"])) ?? optionalNumber(decision.score),
      confidence: optionalNumber(row.confidence) ?? optionalNumber(decision.confidence),
      rationale: stringValue(first(row, ["rationale", "thesis", "reasoning"]))
        || stringValue(first(decision, ["rationale", "thesis", "reasoning"]))
        || undefined,
      createdAt: stringValue(first(row, ["created_at", "timestamp"])) || undefined,
      vetoes: stringList(first(row, ["vetoes", "veto_reasons"])),
      quoteCurrency: stringValue(first(row, ["quote_currency", "currency"])) || undefined,
    };
  });
}

export function normalizeBacktest(payload: unknown, ticker: string): BacktestResult {
  const root = asRecord(payload);
  const metrics = asRecord(root.metrics ?? root.summary ?? root);
  const benchmark = asRecord(root.benchmark);
  const strategy = asRecord(root.strategy);
  const curveRaw = asArray(first(root, ["equity_curve", "curve", "portfolio_values"]));
  const equityCurve = curveRaw.map((item, index) => {
    const row = asRecord(item);
    return {
      date: stringValue(first(row, ["next_date", "date", "timestamp", "time"]), String(index + 1)),
      value: numberValue(first(row, ["equity", "value", "portfolio_value", "balance"])),
    };
  }).filter((point) => Number.isFinite(point.value));
  const winRatePct = optionalNumber(metrics.win_rate_pct)
    ?? (() => {
      const value = optionalNumber(metrics.win_rate);
      return value === undefined ? undefined : Math.abs(value) <= 1 ? value * 100 : value;
    })();
  const firstCurveRow = asRecord(curveRaw[0]);
  const lastCurveRow = asRecord(curveRaw[curveRaw.length - 1]);
  return {
    ticker: stringValue(first(root, ["ticker", "symbol"]), ticker).toUpperCase(),
    totalReturnPct: optionalNumber(first(metrics, ["total_return_pct", "return_pct", "roi_pct"])),
    benchmarkReturnPct: optionalNumber(first(metrics, ["benchmark_return_pct", "buy_hold_return_pct"]))
      ?? optionalNumber(first(benchmark, ["total_return_pct", "return_pct"])),
    sharpeRatio: optionalNumber(first(metrics, ["sharpe_ratio", "sharpe"])),
    maxDrawdownPct: optionalNumber(first(metrics, ["max_drawdown_pct", "max_drawdown"])),
    winRatePct,
    trades: optionalNumber(first(metrics, ["trades", "trade_count", "number_of_trades"])),
    equityCurve,
    method: stringValue(first(root, ["method", "model"])) || stringValue(strategy.name) || undefined,
    from: stringValue(first(root, ["from", "start_date"]))
      || stringValue(first(firstCurveRow, ["date", "timestamp", "time"]))
      || undefined,
    to: stringValue(first(root, ["to", "end_date"]))
      || stringValue(first(lastCurveRow, ["next_date", "date", "timestamp", "time"]))
      || undefined,
  };
}

export function normalizeCitations(value: unknown): Citation[] {
  const root = asRecord(value);
  return asArray(root.sources ?? value).map((item) => {
    const row = asRecord(item);
    return {
      source: stringValue(first(row, ["source", "filename", "title"]), "Tài liệu"),
      page: optionalNumber(row.page) ?? null,
      chunk: optionalNumber(first(row, ["chunk", "position"])) ?? null,
      score: optionalNumber(first(row, ["score", "relevance"])),
      text: stringValue(first(row, ["text", "excerpt", "snippet"])) || undefined,
    };
  });
}

export function normalizeHealth(payload: unknown): HealthStatus {
  const root = asRecord(payload);
  const rawStatus = stringValue(first(root, ["status", "state"]), "unknown").toLowerCase();
  const ok = ["ok", "healthy", "ready", "operational"].includes(rawStatus) || root.ok === true;
  return {
    ok,
    label: ok ? "Hệ thống sẵn sàng" : rawStatus === "unknown" ? "Không rõ trạng thái" : `Trạng thái: ${rawStatus}`,
    mode: stringValue(first(root, ["execution_mode", "mode", "data_mode"])) || undefined,
    details: root,
  };
}

export function eventRecord(value: unknown): Record<string, unknown> {
  return asRecord(value);
}
