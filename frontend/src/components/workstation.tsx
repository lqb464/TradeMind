"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiJson, streamSse, toErrorMessage } from "@/lib/api";
import {
  asRecord,
  eventRecord,
  normalizeBacktest,
  normalizeCitations,
  normalizeDecision,
  normalizeHealth,
  normalizeMarket,
  normalizeOrders,
  normalizePortfolio,
  normalizeProposals,
  numberValue,
  stringValue,
} from "@/lib/normalize";
import type {
  BacktestResult,
  Citation,
  HealthStatus,
  MarketSnapshot,
  Order,
  Portfolio,
  Proposal,
  TradeDecision,
  UserProfile,
} from "@/lib/types";
import { EquityChart, PriceChart } from "./charts";
import { Icon, type IconName } from "./icon";

type View = "market" | "portfolio" | "backtest" | "research" | "system";
type ResearchMode = "copilot" | "rag";

interface WorkstationProps {
  user: UserProfile;
  initialHealth: HealthStatus | null;
  onLogout: () => Promise<void>;
}

interface NavItem {
  id: View;
  label: string;
  description: string;
  icon: IconName;
}

const navItems: NavItem[] = [
  { id: "market", label: "Decision desk", description: "Thị trường & tín hiệu", icon: "trend" },
  { id: "portfolio", label: "Paper portfolio", description: "Vị thế & phê duyệt", icon: "briefcase" },
  { id: "backtest", label: "Strategy lab", description: "Kiểm định ngoài mẫu", icon: "activity" },
  { id: "research", label: "Research copilot", description: "SSE & report RAG", icon: "brain" },
  { id: "system", label: "System health", description: "Dịch vụ & usage", icon: "health" },
];

const decimal = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

function formatMoney(value: number | undefined, currency = "USD"): string {
  if (value === undefined || !Number.isFinite(value)) return "—";
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      maximumFractionDigits: currency === "VND" ? 0 : 2,
    }).format(value);
  } catch {
    return `${decimal.format(value)} ${currency}`;
  }
}

function formatNumber(value: number | undefined, suffix = ""): string {
  return value === undefined || !Number.isFinite(value) ? "—" : `${decimal.format(value)}${suffix}`;
}

function formatPercent(value: number | undefined): string {
  return value === undefined || !Number.isFinite(value) ? "—" : `${value >= 0 ? "+" : ""}${decimal.format(value)}%`;
}

function formatDate(value?: string): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("vi-VN", { dateStyle: "medium", timeStyle: "short" }).format(parsed);
}

function actionTone(action: string): string {
  const normalized = action.toUpperCase();
  if (["BUY", "LONG", "BULLISH"].includes(normalized)) return "positive";
  if (["SELL", "SHORT", "BEARISH", "VETO"].includes(normalized)) return "negative";
  return "neutral";
}

function statusTone(status: string): string {
  const normalized = status.toUpperCase();
  if (["FILLED", "APPROVED", "COMPLETED", "EXECUTED"].includes(normalized)) return "positive";
  if (["REJECTED", "CANCELLED", "FAILED", "VETOED"].includes(normalized)) return "negative";
  if (["PENDING", "PROPOSED", "OPEN", "SUBMITTED"].includes(normalized)) return "warning";
  return "neutral";
}

function researchCompletionStatus(
  mode: ResearchMode,
  record: Record<string, unknown>,
  sourceCount: number,
): string {
  if (mode === "rag") {
    const contextRetrieved = record.context_retrieved === true || sourceCount > 0;
    if (!contextRetrieved) return "Hoàn tất · không tìm thấy context liên quan";
    if (record.citation_verified === true) {
      return "Hoàn tất · context và citation đã được xác minh";
    }
    if (record.citation_markers_valid === true) {
      return "Hoàn tất · có context, marker hợp lệ; citation chưa được xác minh";
    }
    return "Hoàn tất · có context; citation marker chưa hợp lệ";
  }

  const decision = asRecord(record.decision);
  const hasEvidence = record.grounded === true
    || (Array.isArray(decision.evidence) && decision.evidence.length > 0);
  if (!hasEvidence) return "Hoàn tất · không có market evidence khả dụng";
  return record.execution_eligible === true
    ? "Hoàn tất · có market evidence, risk gate đạt"
    : "Hoàn tất · có market evidence, risk gate chưa đạt";
}

function EmptyState({ icon, title, message }: { icon: IconName; title: string; message: string }) {
  return (
    <div className="empty-state">
      <span><Icon name={icon} /></span>
      <b>{title}</b>
      <p>{message}</p>
    </div>
  );
}

function InlineError({ title, message, retry }: { title: string; message: string; retry?: () => void }) {
  return (
    <div className="inline-error" role="alert">
      <Icon name="warning" />
      <div><b>{title}</b><span>{message}</span></div>
      {retry && <button type="button" onClick={retry}><Icon name="refresh" />Thử lại</button>}
    </div>
  );
}

function Skeleton({ className = "" }: { className?: string }) {
  return <span className={`skeleton ${className}`} aria-hidden="true" />;
}

export function Workstation({ user, initialHealth, onLogout }: WorkstationProps) {
  const [view, setView] = useState<View>("market");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [tickerInput, setTickerInput] = useState("AAPL");
  const [ticker, setTicker] = useState("AAPL");
  const [market, setMarket] = useState<MarketSnapshot | null>(null);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketError, setMarketError] = useState("");
  const [decision, setDecision] = useState<TradeDecision | null>(null);
  const [decisionLoading, setDecisionLoading] = useState(false);
  const [decisionError, setDecisionError] = useState("");
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [accountLoading, setAccountLoading] = useState(false);
  const [accountErrors, setAccountErrors] = useState<string[]>([]);
  const [backtest, setBacktest] = useState<BacktestResult | null>(null);
  const [backtestLoading, setBacktestLoading] = useState(false);
  const [backtestError, setBacktestError] = useState("");
  const [proposalBusy, setProposalBusy] = useState("");
  const [controlBusy, setControlBusy] = useState(false);
  const [proposalNotice, setProposalNotice] = useState("");
  const [confirmProposal, setConfirmProposal] = useState<{ id: string; action: "approve" | "reject" } | null>(null);
  const [health, setHealth] = useState<HealthStatus | null>(initialHealth);
  const [healthError, setHealthError] = useState("");
  const [opsUsage, setOpsUsage] = useState<Record<string, unknown> | null>(null);
  const [opsTraces, setOpsTraces] = useState<Record<string, unknown>[]>([]);
  const [opsError, setOpsError] = useState("");
  const [researchMode, setResearchMode] = useState<ResearchMode>("copilot");
  const [question, setQuestion] = useState("Phân tích xu hướng, rủi ro và điều kiện vô hiệu hóa luận điểm cho mã này.");
  const [answer, setAnswer] = useState("");
  const [researchStatus, setResearchStatus] = useState("Sẵn sàng");
  const [researchSteps, setResearchSteps] = useState<string[]>([]);
  const [researchError, setResearchError] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [documentId, setDocumentId] = useState("");
  const [documentName, setDocumentName] = useState("");
  const [uploading, setUploading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const approvalKeysRef = useRef<Record<string, string>>({});
  const proposalKeysRef = useRef<Record<string, string>>({});

  const loadMarket = useCallback(async (symbol: string) => {
    const normalized = symbol.trim().toUpperCase();
    if (!/^[A-Z0-9.^-]{1,12}$/.test(normalized)) {
      setMarketError("Mã không hợp lệ. Chỉ dùng chữ, số, dấu chấm, gạch ngang hoặc ^.");
      return;
    }
    setTicker(normalized);
    setTickerInput(normalized);
    setMarketLoading(true);
    setMarketError("");
    setDecision(null);
    setDecisionError("");
    setBacktest(null);
    setBacktestError("");
    try {
      const payload = await apiJson<unknown>(`/api/market/${encodeURIComponent(normalized)}`);
      setMarket(normalizeMarket(payload, normalized));
    } catch (error) {
      setMarket(null);
      setMarketError(toErrorMessage(error));
    } finally {
      setMarketLoading(false);
    }
  }, []);

  const loadAccount = useCallback(async () => {
    setAccountLoading(true);
    setAccountErrors([]);
    const [portfolioResult, orderResult, proposalResult] = await Promise.allSettled([
      apiJson<unknown>("/api/portfolio/account"),
      apiJson<unknown>("/api/portfolio/orders"),
      apiJson<unknown>("/api/portfolio/proposals"),
    ]);
    const errors: string[] = [];
    if (portfolioResult.status === "fulfilled") setPortfolio(normalizePortfolio(portfolioResult.value));
    else { setPortfolio(null); errors.push(`Portfolio: ${toErrorMessage(portfolioResult.reason)}`); }
    if (orderResult.status === "fulfilled") setOrders(normalizeOrders(orderResult.value));
    else { setOrders([]); errors.push(`Orders: ${toErrorMessage(orderResult.reason)}`); }
    if (proposalResult.status === "fulfilled") setProposals(normalizeProposals(proposalResult.value));
    else { setProposals([]); errors.push(`Proposals: ${toErrorMessage(proposalResult.reason)}`); }
    setAccountErrors(errors);
    setAccountLoading(false);
  }, []);

  const loadHealth = useCallback(async () => {
    setHealthError("");
    try {
      setHealth(normalizeHealth(await apiJson<unknown>("/api/health")));
    } catch (error) {
      setHealth(null);
      setHealthError(toErrorMessage(error));
    }
  }, []);

  useEffect(() => {
    void loadMarket("AAPL");
    void loadAccount();
  }, [loadAccount, loadMarket]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (view !== "system") return;
    let active = true;
    void loadHealth();
    Promise.allSettled([
      apiJson<unknown>("/api/ops/usage"),
      apiJson<unknown>("/api/ops/traces?limit=12"),
    ]).then(([usageResult, traceResult]) => {
      if (!active) return;
      const errors: string[] = [];
      if (usageResult.status === "fulfilled") setOpsUsage(asRecord(usageResult.value));
      else errors.push(`Usage: ${toErrorMessage(usageResult.reason)}`);
      if (traceResult.status === "fulfilled") {
        const record = asRecord(traceResult.value);
        const items = Array.isArray(record.items) ? record.items : [];
        setOpsTraces(items.map(asRecord));
      } else errors.push(`Traces: ${toErrorMessage(traceResult.reason)}`);
      setOpsError(errors.join(" · "));
    });
    return () => { active = false; };
  }, [loadHealth, view]);

  async function loadDecision() {
    setDecisionLoading(true);
    setDecisionError("");
    setProposalNotice("");
    try {
      const payload = await apiJson<unknown>(`/api/trade/decision/${encodeURIComponent(ticker)}`);
      setDecision(normalizeDecision(payload, ticker));
    } catch (error) {
      setDecision(null);
      setDecisionError(toErrorMessage(error));
    } finally {
      setDecisionLoading(false);
    }
  }

  async function loadBacktest() {
    setBacktestLoading(true);
    setBacktestError("");
    try {
      const payload = await apiJson<unknown>(`/api/trade/backtest/${encodeURIComponent(ticker)}`);
      setBacktest(normalizeBacktest(payload, ticker));
    } catch (error) {
      setBacktest(null);
      setBacktestError(toErrorMessage(error));
    } finally {
      setBacktestLoading(false);
    }
  }

  function selectView(next: View) {
    setView(next);
    setSidebarOpen(false);
    if (next === "portfolio") void loadAccount();
    if (next === "backtest" && !backtest && !backtestLoading) void loadBacktest();
  }

  async function createProposal() {
    if (!decision?.id) return;
    setProposalBusy("create");
    setProposalNotice("");
    try {
      const key = decision.id;
      proposalKeysRef.current[key] ||= crypto.randomUUID();
      await apiJson<unknown>("/api/portfolio/proposals", {
        method: "POST",
        headers: { "Idempotency-Key": proposalKeysRef.current[key] },
        body: JSON.stringify({ ticker: decision.ticker, decision_id: decision.id }),
      });
      delete proposalKeysRef.current[key];
      setProposalNotice("Đã tạo đề xuất PAPER. Lệnh chưa được thực thi; cần phê duyệt riêng.");
      await loadAccount();
    } catch (error) {
      setProposalNotice(`Không thể tạo đề xuất: ${toErrorMessage(error)}`);
    } finally {
      setProposalBusy("");
    }
  }

  async function resolveProposal(id: string, action: "approve" | "reject") {
    setProposalBusy(id);
    setProposalNotice("");
    try {
      const headers = new Headers();
      if (action === "approve") {
        approvalKeysRef.current[id] ||= crypto.randomUUID();
        headers.set("Idempotency-Key", approvalKeysRef.current[id]);
      }
      await apiJson<unknown>(`/api/portfolio/proposals/${encodeURIComponent(id)}/${action}`, {
        method: "POST",
        headers,
        body: JSON.stringify({}),
      });
      delete approvalKeysRef.current[id];
      setProposalNotice(action === "approve" ? "Đề xuất PAPER đã được phê duyệt." : "Đề xuất đã bị từ chối.");
      setConfirmProposal(null);
      await loadAccount();
    } catch (error) {
      setProposalNotice(`Không thể ${action === "approve" ? "phê duyệt" : "từ chối"}: ${toErrorMessage(error)}`);
    } finally {
      setProposalBusy("");
    }
  }

  async function updatePaperControls(action: "activate-kill" | "clear-kill" | "enable" | "pause") {
    if (!portfolio?.accountId) {
      setProposalNotice("Không thể cập nhật: tài khoản PAPER chưa có định danh hợp lệ.");
      return;
    }
    const payload: Record<string, unknown> = {
      reason: `User requested ${action} from TradeMind portfolio control room`,
    };
    if (action === "activate-kill") payload.kill_switch = true;
    if (action === "clear-kill") payload.kill_switch = false;
    if (action === "enable") payload.trading_enabled = true;
    if (action === "pause") payload.trading_enabled = false;
    setControlBusy(true);
    setProposalNotice("");
    try {
      await apiJson<unknown>(`/api/portfolio/accounts/${encodeURIComponent(portfolio.accountId)}/controls`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const messages = {
        "activate-kill": "Kill switch đã bật. Mọi phê duyệt lệnh PAPER bị khóa.",
        "clear-kill": "Kill switch đã được gỡ; phê duyệt PAPER vẫn đang tạm dừng.",
        enable: "Luồng phê duyệt PAPER đã được bật lại.",
        pause: "Luồng phê duyệt PAPER đã tạm dừng.",
      };
      setProposalNotice(messages[action]);
      await loadAccount();
    } catch (error) {
      setProposalNotice(`Không thể cập nhật kiểm soát: ${toErrorMessage(error)}`);
    } finally {
      setControlBusy(false);
    }
  }

  async function uploadDocument(file: File) {
    setUploading(true);
    setResearchError("");
    setDocumentName(file.name);
    const form = new FormData();
    form.append("file", file);
    try {
      const payload = asRecord(await apiJson<unknown>("/api/documents", { method: "POST", body: form }));
      const id = stringValue(payload.document_id ?? payload.id);
      if (!id) throw new Error("Backend không trả về document_id.");
      setDocumentId(id);
      setResearchStatus(`Đã lập chỉ mục ${numberValue(payload.chunks)} đoạn từ ${file.name}`);
    } catch (error) {
      setDocumentId("");
      setResearchError(toErrorMessage(error));
    } finally {
      setUploading(false);
    }
  }

  async function askResearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (streaming || !question.trim()) return;
    if (researchMode === "rag" && !documentId) {
      setResearchError("Hãy tải và lập chỉ mục một báo cáo trước khi hỏi Report RAG.");
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setStreaming(true);
    setAnswer("");
    setCitations([]);
    setResearchSteps([]);
    setResearchError("");
    setResearchStatus(researchMode === "rag" ? "Đang truy xuất bằng chứng…" : "Agent đang lập luận từ các công cụ…");
    try {
      await streamSse(
        researchMode === "rag" ? "/api/rag/ask" : "/api/copilot/stream",
        { question: question.trim(), ticker, document_id: documentId || undefined },
        controller.signal,
        ({ type, data }) => {
          const record = eventRecord(data);
          if (type === "token" || type === "message") {
            const token = typeof data === "string" ? data : stringValue(record.content ?? record.token ?? record.text);
            if (token) setAnswer((current) => current + token);
          }
          if (type === "status") setResearchStatus(stringValue(record.content ?? record.message, "Đang xử lý…"));
          if (type === "tool") {
            const tool = stringValue(record.name ?? record.tool, "tool");
            setResearchSteps((current) => current.includes(tool) ? current : [...current, tool]);
            setResearchStatus(stringValue(record.content ?? record.message, `Đã chạy ${tool}`));
          }
          if (type === "sources") setCitations(normalizeCitations(record.sources ?? data));
          if (type === "tool_error") {
            const tool = stringValue(record.name ?? record.tool, "tool");
            setResearchSteps((current) => current.includes(tool) ? current : [...current, tool]);
            setResearchStatus(stringValue(record.content ?? record.message, `${tool} không khả dụng; confidence đã giảm`));
          }
          if (type === "error") {
            setResearchError(stringValue(record.content ?? record.message ?? record.detail, "Luồng AI báo lỗi."));
          }
          if (type === "done") {
            const finalSources = normalizeCitations(record.sources ?? []);
            if (researchMode === "rag") setCitations(finalSources);
            setResearchStatus(researchCompletionStatus(researchMode, record, finalSources.length));
          }
        },
      );
    } catch (error) {
      if (!controller.signal.aborted) setResearchError(toErrorMessage(error));
      setResearchStatus(controller.signal.aborted ? "Đã dừng theo yêu cầu" : "Luồng bị gián đoạn");
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setStreaming(false);
    }
  }

  function stopStream() {
    abortRef.current?.abort();
  }

  const selectedNav = navItems.find((item) => item.id === view) ?? navItems[0];
  const pendingProposals = proposals.filter((item) => ["PENDING", "PROPOSED", "AWAITING_APPROVAL"].includes(item.status));
  const canCreateProposal = Boolean(
    decision &&
    decision.id &&
    decision.executable &&
    decision.vetoes.length === 0 &&
    !["HOLD", "VETO"].includes(decision.action) &&
    !["stale", "unavailable"].includes(market?.provenance.mode ?? "unknown"),
  );

  return (
    <main className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebar-brand"><span className="brand-mark"><Icon name="trend" /></span><div><b>TradeMind</b><small>PAPER INTELLIGENCE</small></div></div>
        <nav className="sidebar-nav" aria-label="Điều hướng chính">
          {navItems.map((item) => (
            <button key={item.id} type="button" className={view === item.id ? "active" : ""} onClick={() => selectView(item.id)}>
              <span className="nav-icon"><Icon name={item.icon} /></span>
              <span><b>{item.label}</b><small>{item.description}</small></span>
              {item.id === "portfolio" && pendingProposals.length > 0 && <em>{pendingProposals.length}</em>}
            </button>
          ))}
        </nav>
        <div className="paper-mode-card">
          <span><Icon name="shield" /></span>
          <div><b>PAPER mode</b><small>Không gửi lệnh tiền thật</small></div>
        </div>
        <div className="sidebar-user">
          <span className="avatar">{(user.name || user.email || "U").slice(0, 1).toUpperCase()}</span>
          <div><b>{user.name || "Nhà đầu tư"}</b><small>{user.email}</small></div>
          <button type="button" aria-label="Đăng xuất" title="Đăng xuất" onClick={() => void onLogout()}><Icon name="logout" /></button>
        </div>
      </aside>
      {sidebarOpen && <button className="sidebar-scrim" type="button" aria-label="Đóng menu" onClick={() => setSidebarOpen(false)} />}

      <section className="app-main">
        <header className="topbar">
          <button className="mobile-menu" type="button" aria-label="Mở menu" onClick={() => setSidebarOpen(true)}><Icon name="menu" /></button>
          <div className="page-title"><span>{selectedNav.label}</span><small>{selectedNav.description}</small></div>
          <form className="symbol-search" onSubmit={(event) => { event.preventDefault(); void loadMarket(tickerInput); }}>
            <Icon name="search" />
            <input value={tickerInput} onChange={(event) => setTickerInput(event.target.value.toUpperCase())} aria-label="Tìm mã cổ phiếu" placeholder="AAPL, FPT.VN…" />
            <button type="submit">Phân tích</button>
          </form>
          <div className={`top-health ${health?.ok ? "ok" : "warn"}`} title={healthError || health?.label}>
            <span className="status-dot" />
            <b>{health?.ok ? "API online" : "Kiểm tra API"}</b>
          </div>
        </header>

        <div className="mobile-symbol-row">
          <form className="symbol-search" onSubmit={(event) => { event.preventDefault(); void loadMarket(tickerInput); }}>
            <Icon name="search" /><input value={tickerInput} onChange={(event) => setTickerInput(event.target.value.toUpperCase())} aria-label="Tìm mã cổ phiếu" /><button type="submit">Mở</button>
          </form>
        </div>

        {view === "market" && (
          <MarketView
            ticker={ticker}
            market={market}
            marketLoading={marketLoading}
            marketError={marketError}
            decision={decision}
            decisionLoading={decisionLoading}
            decisionError={decisionError}
            canCreateProposal={canCreateProposal}
            proposalBusy={proposalBusy === "create"}
            proposalNotice={proposalNotice}
            onReloadMarket={() => void loadMarket(ticker)}
            onDecision={() => void loadDecision()}
            onCreateProposal={() => void createProposal()}
            onOpenPortfolio={() => selectView("portfolio")}
          />
        )}

        {view === "portfolio" && (
          <PortfolioView
            portfolio={portfolio}
            orders={orders}
            proposals={proposals}
            loading={accountLoading}
            errors={accountErrors}
            notice={proposalNotice}
            busyId={proposalBusy}
            controlBusy={controlBusy}
            confirmation={confirmProposal}
            onRefresh={() => void loadAccount()}
            onConfirm={setConfirmProposal}
            onCancelConfirm={() => setConfirmProposal(null)}
            onResolve={(id, action) => void resolveProposal(id, action)}
            onControl={(action) => void updatePaperControls(action)}
          />
        )}

        {view === "backtest" && (
          <BacktestView ticker={ticker} result={backtest} loading={backtestLoading} error={backtestError} onRun={() => void loadBacktest()} />
        )}

        {view === "research" && (
          <ResearchView
            ticker={ticker}
            mode={researchMode}
            question={question}
            answer={answer}
            status={researchStatus}
            steps={researchSteps}
            error={researchError}
            citations={citations}
            streaming={streaming}
            documentId={documentId}
            documentName={documentName}
            uploading={uploading}
            onMode={setResearchMode}
            onQuestion={setQuestion}
            onSubmit={askResearch}
            onStop={stopStream}
            onUpload={(file) => void uploadDocument(file)}
          />
        )}

        {view === "system" && (
          <SystemView health={health} healthError={healthError} usage={opsUsage} traces={opsTraces} opsError={opsError} onRefresh={() => { void loadHealth(); setView("market"); setTimeout(() => setView("system"), 0); }} />
        )}
      </section>
    </main>
  );
}

interface MarketViewProps {
  ticker: string;
  market: MarketSnapshot | null;
  marketLoading: boolean;
  marketError: string;
  decision: TradeDecision | null;
  decisionLoading: boolean;
  decisionError: string;
  canCreateProposal: boolean;
  proposalBusy: boolean;
  proposalNotice: string;
  onReloadMarket: () => void;
  onDecision: () => void;
  onCreateProposal: () => void;
  onOpenPortfolio: () => void;
}

function MarketView(props: MarketViewProps) {
  const { market, decision } = props;
  const contributionScale = Math.max(1, ...(decision?.contributions.map((item) => Math.abs(item.value)) ?? [1]));
  const provenance = market?.provenance;
  return (
    <div className="view-content market-view">
      {props.marketError && <InlineError title={`Không tải được ${props.ticker}`} message={props.marketError} retry={props.onReloadMarket} />}
      {provenance && provenance.mode !== "live" && (
        <div className={`provenance-banner ${provenance.mode}`}>
          <Icon name={provenance.mode === "demo" ? "gauge" : "warning"} />
          <div>
            <b>{provenance.mode === "demo" ? "Dữ liệu demo được gắn nhãn" : provenance.mode === "stale" ? "Dữ liệu đã cũ" : "Nguồn dữ liệu chưa được xác minh"}</b>
            <span>{provenance.message || "TradeMind sẽ không trình bày dữ liệu này như dữ liệu thị trường trực tiếp."}</span>
          </div>
        </div>
      )}
      <section className="market-grid">
        <article className="panel market-panel">
          <div className="market-heading">
            <div className="ticker-identity">
              <span className="ticker-logo">{props.ticker.slice(0, 2)}</span>
              <div><small>MÃ ĐANG PHÂN TÍCH</small><h1>{market?.ticker ?? props.ticker}</h1></div>
            </div>
            {props.marketLoading ? <Skeleton className="price-skeleton" /> : market ? (
              <div className="quote-block">
                <strong>{formatMoney(market.price, market.currency)}</strong>
                <span className={market.change >= 0 ? "gain" : "loss"}>{market.change >= 0 ? <Icon name="arrow-up" /> : <Icon name="arrow-down" />}{formatMoney(market.change, market.currency)} · {formatPercent(market.changePct)}</span>
              </div>
            ) : <div className="quote-block unavailable"><strong>—</strong><span>Chưa có dữ liệu</span></div>}
          </div>
          {market ? <PriceChart candles={market.candles} /> : !props.marketLoading && <EmptyState icon="trend" title="Chưa có biểu đồ" message="Kết nối market API hoặc chọn thử một mã khác." />}
          {market && (
            <div className="provenance-row">
              <span className={`provenance-pill ${provenance?.mode}`}><i />{provenance?.mode.toUpperCase()}</span>
              <span>Nguồn: <b>{provenance?.source}</b></span>
              <span>Cập nhật: <b>{formatDate(provenance?.asOf)}</b></span>
            </div>
          )}
        </article>

        <article className="panel decision-panel">
          <div className="panel-heading">
            <div><span className="section-icon"><Icon name="brain" /></span><div><small>AGENT DECISION</small><h2>Trade thesis</h2></div></div>
            <button className="secondary-button" type="button" onClick={props.onDecision} disabled={props.decisionLoading || !market}>
              {props.decisionLoading ? <span className="spinner dark" /> : <Icon name="spark" />}{props.decisionLoading ? "Đang chạy…" : "Chạy agent"}
            </button>
          </div>
          {props.decisionError && <InlineError title="Decision API chưa sẵn sàng" message={props.decisionError} retry={props.onDecision} />}
          {!decision && !props.decisionLoading && !props.decisionError && (
            <EmptyState icon="brain" title="Chưa có quyết định" message="Agent chỉ tạo đề xuất sau khi market data được tải và risk engine đã đánh giá." />
          )}
          {props.decisionLoading && <div className="decision-loading"><Skeleton /><Skeleton /><Skeleton /></div>}
          {decision && (
            <div className="decision-body">
              <div className="decision-score-row">
                <span className={`action-badge ${actionTone(decision.action)}`}>{decision.action}</span>
                <div className="score-ring" style={{ "--score": `${Math.max(0, Math.min(100, decision.confidence * 100)) * 3.6}deg` } as React.CSSProperties}>
                  <span><b>{Math.round(decision.confidence * 100)}%</b><small>tin cậy</small></span>
                </div>
                <div className="score-copy"><small>COMPOSITE SCORE</small><strong>{decision.score >= 0 ? "+" : ""}{formatNumber(decision.score)}</strong><span>{decision.horizon ? `Khung ${decision.horizon}` : "Khung thời gian do backend xác định"}</span></div>
              </div>
              {decision.thesis && <p className="decision-thesis">{decision.thesis}</p>}
              <div className="decision-columns">
                <div>
                  <h3>Đóng góp tín hiệu</h3>
                  {decision.contributions.length ? <div className="contribution-list">{decision.contributions.map((item) => (
                    <div className="contribution" key={item.name} title={item.detail}>
                      <div><span>{item.name.replaceAll("_", " ")}</span><b className={item.value >= 0 ? "gain-text" : "loss-text"}>{item.value >= 0 ? "+" : ""}{formatNumber(item.value)}</b></div>
                      <div className="factor-track"><i className={item.value >= 0 ? "up" : "down"} style={{ width: `${Math.max(4, Math.abs(item.value) / contributionScale * 100)}%` }} /></div>
                    </div>
                  ))}</div> : <p className="muted-copy">Backend chưa trả breakdown đóng góp.</p>}
                </div>
                <div>
                  <h3>Risk gate</h3>
                  {decision.vetoes.length ? <div className="veto-list">{decision.vetoes.map((veto) => <span key={veto}><Icon name="x" />{veto}</span>)}</div> : <div className="risk-pass"><Icon name="check" /><div><b>Không có veto</b><span>Vẫn cần phê duyệt của con người.</span></div></div>}
                </div>
              </div>
              <div className="sizing-grid">
                <div><small>QUANTITY</small><b>{decision.sizing.quantity === undefined ? "—" : integer.format(decision.sizing.quantity)}</b></div>
                <div><small>NOTIONAL</small><b>{formatMoney(decision.sizing.notional, market?.currency)}</b></div>
                <div><small>RISK / PORTFOLIO</small><b>{formatNumber(decision.sizing.riskPct, "%")}</b></div>
                <div><small>STOP LOSS</small><b>{formatMoney(decision.sizing.stopLoss, market?.currency)}</b></div>
                <div><small>TAKE PROFIT</small><b>{formatMoney(decision.sizing.takeProfit, market?.currency)}</b></div>
              </div>
              {decision.provenanceMode === "demo" && <div className="decision-warning"><Icon name="gauge" />Quyết định dùng dữ liệu demo; chỉ phù hợp để thử quy trình PAPER.</div>}
              <div className="proposal-actions">
                <button className="primary-button" type="button" disabled={!props.canCreateProposal || props.proposalBusy} onClick={props.onCreateProposal}>
                  {props.proposalBusy ? <span className="spinner" /> : <Icon name="document" />}{props.proposalBusy ? "Đang tạo…" : "Tạo đề xuất PAPER"}
                </button>
                <button className="text-button" type="button" onClick={props.onOpenPortfolio}>Mở hàng đợi phê duyệt <Icon name="chevron" /></button>
              </div>
              {!props.canCreateProposal && <small className="blocked-reason">Không thể tạo đề xuất khi risk gate veto, hành động là HOLD, hoặc dữ liệu stale/unavailable.</small>}
              {props.proposalNotice && <div className={`form-notice ${props.proposalNotice.startsWith("Không") ? "error" : "success"}`}><Icon name={props.proposalNotice.startsWith("Không") ? "warning" : "check"} />{props.proposalNotice}</div>}
            </div>
          )}
        </article>
      </section>
      <section className="indicator-strip">
        <MetricCard label="RSI · 14" value={market?.indicators.rsi} format={(value) => formatNumber(value)} note="Momentum" />
        <MetricCard label="MACD" value={market?.indicators.macd} format={(value) => formatNumber(value)} note={`Signal ${formatNumber(market?.indicators.macdSignal ?? undefined)}`} />
        <MetricCard label="MA · 20" value={market?.indicators.ma20} format={formatMoney} note="Xu hướng ngắn" />
        <MetricCard label="MA · 50" value={market?.indicators.ma50} format={formatMoney} note="Xu hướng trung" />
        <MetricCard label="VOLUME" value={market?.volume} format={(value) => value === undefined ? "—" : integer.format(value)} note="Phiên gần nhất" />
      </section>
    </div>
  );
}

function MetricCard({ label, value, format, note }: { label: string; value?: number | null; format: (value: number | undefined) => string; note: string }) {
  return <article className="metric-card"><small>{label}</small><b>{format(value ?? undefined)}</b><span>{note}</span></article>;
}

interface PortfolioViewProps {
  portfolio: Portfolio | null;
  orders: Order[];
  proposals: Proposal[];
  loading: boolean;
  errors: string[];
  notice: string;
  busyId: string;
  controlBusy: boolean;
  confirmation: { id: string; action: "approve" | "reject" } | null;
  onRefresh: () => void;
  onConfirm: (value: { id: string; action: "approve" | "reject" }) => void;
  onCancelConfirm: () => void;
  onResolve: (id: string, action: "approve" | "reject") => void;
  onControl: (action: "activate-kill" | "clear-kill" | "enable" | "pause") => void;
}

function PortfolioView(props: PortfolioViewProps) {
  const pending = props.proposals.filter((item) => ["PENDING", "PROPOSED", "AWAITING_APPROVAL"].includes(item.status));
  return (
    <div className="view-content portfolio-view">
      <div className="view-heading"><div><span className="eyebrow">PAPER ACCOUNT</span><h1>Portfolio control room</h1><p>Mọi đề xuất phải đi qua một hành động phê duyệt riêng biệt.</p></div><button className="secondary-button" type="button" onClick={props.onRefresh} disabled={props.loading}><Icon name="refresh" />Làm mới</button></div>
      {props.errors.map((error) => <InlineError key={error} title="Một phần dữ liệu chưa sẵn sàng" message={error} retry={props.onRefresh} />)}
      {props.notice && <div className={`form-notice ${props.notice.startsWith("Không") ? "error" : "success"}`}><Icon name={props.notice.startsWith("Không") ? "warning" : "check"} />{props.notice}</div>}
      <section className="account-summary">
        <SummaryCard label="Total equity" value={props.portfolio ? formatMoney(props.portfolio.equity, props.portfolio.currency) : "—"} note="Paper NAV" icon="briefcase" loading={props.loading} />
        <SummaryCard label="Available cash" value={props.portfolio ? formatMoney(props.portfolio.cash, props.portfolio.currency) : "—"} note="Chưa cam kết" icon="layers" loading={props.loading} />
        <SummaryCard label="Buying power" value={props.portfolio ? formatMoney(props.portfolio.buyingPower, props.portfolio.currency) : "—"} note="Theo risk policy" icon="gauge" loading={props.loading} />
        <SummaryCard label="Unrealized P&L" value={props.portfolio ? formatMoney(props.portfolio.unrealizedPnl, props.portfolio.currency) : "—"} note="Mark-to-market" icon="activity" loading={props.loading} tone={(props.portfolio?.unrealizedPnl ?? 0) >= 0 ? "gain" : "loss"} />
      </section>

      <article className={`panel trading-controls ${props.portfolio?.killSwitch ? "kill-active" : ""}`}>
        <div className="control-copy">
          <span className={`section-icon ${props.portfolio?.killSwitch ? "danger" : "amber"}`}><Icon name={props.portfolio?.killSwitch ? "warning" : "shield"} /></span>
          <div>
            <small>EXECUTION SAFETY · {props.portfolio?.executionMode ?? "PAPER"}</small>
            <h2>{props.portfolio?.killSwitch ? "Kill switch đang khóa" : props.portfolio?.tradingEnabled ? "Phê duyệt PAPER đang mở" : "Phê duyệt PAPER đang tạm dừng"}</h2>
            <p>Agent chỉ tạo đề xuất. Lệnh chỉ được fill sau phê duyệt thủ công, risk gate lần cuối và dữ liệu thị trường hợp lệ.</p>
            {props.portfolio?.marksIncludeDemoData && <span className="demo-mark-warning"><Icon name="warning" />Một số giá mark là dữ liệu demo; chúng không đủ điều kiện fill lệnh.</span>}
          </div>
        </div>
        <div className="control-actions">
          {props.portfolio?.killSwitch ? (
            <button className="secondary-button" type="button" disabled={props.controlBusy} onClick={() => props.onControl("clear-kill")}><Icon name="lock" />Gỡ khóa (vẫn dừng)</button>
          ) : (
            <button className="reject-button" type="button" disabled={props.controlBusy || !props.portfolio?.accountId} onClick={() => props.onControl("activate-kill")}><Icon name="warning" />Bật kill switch</button>
          )}
          {props.portfolio?.tradingEnabled ? (
            <button className="secondary-button" type="button" disabled={props.controlBusy} onClick={() => props.onControl("pause")}><Icon name="lock" />Tạm dừng duyệt</button>
          ) : (
            <button className="approve-button" type="button" disabled={props.controlBusy || props.portfolio?.killSwitch || !props.portfolio?.accountId} onClick={() => props.onControl("enable")}><Icon name="check" />Bật duyệt PAPER</button>
          )}
        </div>
      </article>

      <section className="portfolio-grid">
        <article className="panel table-panel positions-panel">
          <div className="panel-heading simple"><div><span className="section-icon"><Icon name="briefcase" /></span><div><small>OPEN EXPOSURE</small><h2>Vị thế</h2></div></div><span className="count-pill">{props.portfolio?.positions.length ?? 0}</span></div>
          {props.portfolio?.positions.length ? (
            <div className="table-scroll"><table><thead><tr><th>Mã</th><th>SL</th><th>Giá vốn</th><th>Giá TT</th><th>Giá trị</th><th>P&L</th></tr></thead><tbody>{props.portfolio.positions.map((position) => (
              <tr key={position.ticker}><td><b>{position.ticker}</b></td><td>{integer.format(position.quantity)}</td><td>{formatMoney(position.averagePrice, props.portfolio?.currency)}</td><td>{formatMoney(position.marketPrice, props.portfolio?.currency)}</td><td>{formatMoney(position.marketValue, props.portfolio?.currency)}</td><td className={(position.unrealizedPnl ?? 0) >= 0 ? "gain-text" : "loss-text"}><b>{formatMoney(position.unrealizedPnl, props.portfolio?.currency)}</b><small>{formatPercent(position.unrealizedPnlPct)}</small></td></tr>
            ))}</tbody></table></div>
          ) : <EmptyState icon="briefcase" title="Chưa có vị thế" message="Vị thế PAPER sẽ xuất hiện sau khi đề xuất được duyệt và lệnh được fill." />}
        </article>

        <article className="panel approval-panel">
          <div className="panel-heading simple"><div><span className="section-icon amber"><Icon name="shield" /></span><div><small>HUMAN IN THE LOOP</small><h2>Chờ phê duyệt</h2></div></div><span className="count-pill amber">{pending.length}</span></div>
          {pending.length ? <div className="proposal-list">{pending.map((proposal) => (
            <div className="proposal-card" key={proposal.id}>
              <div className="proposal-card-head"><span className={`action-badge small ${actionTone(proposal.side)}`}>{proposal.side}</span><div><b>{proposal.ticker}</b><small>{formatDate(proposal.createdAt)}</small></div><em>{proposal.quantity === undefined ? formatMoney(proposal.notional, proposal.quoteCurrency ?? props.portfolio?.currency) : `${integer.format(proposal.quantity)} cp`}</em></div>
              {proposal.rationale && <p>{proposal.rationale}</p>}
              {proposal.vetoes.length > 0 && <div className="mini-veto"><Icon name="warning" />{proposal.vetoes.join(" · ")}</div>}
              {props.confirmation?.id === proposal.id ? (
                <div className={`approval-confirm ${props.confirmation.action}`}>
                  <b>{props.confirmation.action === "approve" ? "Xác nhận gửi lệnh PAPER?" : "Xác nhận từ chối đề xuất?"}</b>
                  <span>{props.confirmation.action === "approve" ? "Đây là bước phê duyệt của con người. Không có tiền thật được sử dụng." : "Đề xuất sẽ được ghi nhận là rejected trong audit trail."}</span>
                  <div><button type="button" className={props.confirmation.action === "approve" ? "approve-button" : "reject-button"} disabled={props.busyId === proposal.id} onClick={() => props.onResolve(proposal.id, props.confirmation?.action ?? "reject")}>{props.busyId === proposal.id ? "Đang xử lý…" : "Xác nhận"}</button><button type="button" className="text-button" onClick={props.onCancelConfirm}>Quay lại</button></div>
                </div>
              ) : (
                <div className="approval-actions"><button type="button" className="approve-button" onClick={() => props.onConfirm({ id: proposal.id, action: "approve" })}><Icon name="check" />Duyệt PAPER</button><button type="button" className="reject-button" onClick={() => props.onConfirm({ id: proposal.id, action: "reject" })}><Icon name="x" />Từ chối</button></div>
              )}
            </div>
          ))}</div> : <EmptyState icon="shield" title="Hàng đợi trống" message="Không có lệnh nào được tự động thực thi. Tạo proposal từ Decision desk trước." />}
        </article>
      </section>

      <article className="panel table-panel orders-panel">
        <div className="panel-heading simple"><div><span className="section-icon"><Icon name="orders" /></span><div><small>ORDER LEDGER</small><h2>Lịch sử lệnh PAPER</h2></div></div><span className="count-pill">{props.orders.length}</span></div>
        {props.orders.length ? <div className="table-scroll"><table><thead><tr><th>ID</th><th>Mã</th><th>Phía</th><th>SL</th><th>Loại</th><th>Giá</th><th>Trạng thái</th><th>Thời gian</th></tr></thead><tbody>{props.orders.map((order) => (
          <tr key={order.id}><td><code>{order.id.slice(0, 10)}</code></td><td><b>{order.ticker}</b></td><td><span className={`action-badge tiny ${actionTone(order.side)}`}>{order.side}</span></td><td>{integer.format(order.quantity)}</td><td>{order.orderType ?? "—"}</td><td>{formatMoney(order.filledPrice ?? order.price, order.quoteCurrency ?? props.portfolio?.currency)}</td><td><span className={`status-badge ${statusTone(order.status)}`}>{order.status}</span></td><td>{formatDate(order.createdAt)}</td></tr>
        ))}</tbody></table></div> : <EmptyState icon="orders" title="Chưa có lệnh" message="Order ledger sẽ hiển thị kết quả sau bước phê duyệt PAPER." />}
      </article>
    </div>
  );
}

function SummaryCard({ label, value, note, icon, loading, tone }: { label: string; value: string; note: string; icon: IconName; loading: boolean; tone?: string }) {
  return <article className="summary-card"><span><Icon name={icon} /></span><div><small>{label}</small>{loading ? <Skeleton /> : <b className={tone}>{value}</b>}<em>{note}</em></div></article>;
}

function BacktestView({ ticker, result, loading, error, onRun }: { ticker: string; result: BacktestResult | null; loading: boolean; error: string; onRun: () => void }) {
  return (
    <div className="view-content backtest-view">
      <div className="view-heading"><div><span className="eyebrow">WALK-FORWARD EVIDENCE</span><h1>Strategy lab · {ticker}</h1><p>Hiệu suất quá khứ không bảo đảm kết quả tương lai. Metrics chỉ hiển thị khi backend trả dữ liệu.</p></div><button className="primary-button" type="button" onClick={onRun} disabled={loading}>{loading ? <span className="spinner" /> : <Icon name="activity" />}{loading ? "Đang chạy…" : "Chạy backtest"}</button></div>
      {error && <InlineError title="Backtest API chưa sẵn sàng" message={error} retry={onRun} />}
      <section className="backtest-metrics">
        <SummaryCard label="Total return" value={result ? formatPercent(result.totalReturnPct) : "—"} note="Sau chi phí nếu backend hỗ trợ" icon="trend" loading={loading} tone={(result?.totalReturnPct ?? 0) >= 0 ? "gain" : "loss"} />
        <SummaryCard label="Sharpe ratio" value={result ? formatNumber(result.sharpeRatio) : "—"} note="Risk-adjusted" icon="gauge" loading={loading} />
        <SummaryCard label="Max drawdown" value={result ? formatPercent(result.maxDrawdownPct) : "—"} note="Peak-to-trough" icon="arrow-down" loading={loading} tone="loss" />
        <SummaryCard label="Win rate" value={result ? formatPercent(result.winRatePct) : "—"} note={result?.trades === undefined ? "Số lệnh chưa có" : `${result.trades} lệnh`} icon="check" loading={loading} />
      </section>
      <section className="backtest-grid">
        <article className="panel equity-panel">
          <div className="panel-heading simple"><div><span className="section-icon"><Icon name="activity" /></span><div><small>EQUITY CURVE</small><h2>Đường cong vốn</h2></div></div>{result?.method && <span className="method-pill">{result.method}</span>}</div>
          {result ? <EquityChart points={result.equityCurve} /> : !loading && <EmptyState icon="activity" title="Chưa có kết quả" message="Chạy backtest để tải metrics và equity curve từ backend." />}
        </article>
        <article className="panel validation-card">
          <span className="section-icon amber"><Icon name="shield" /></span><h2>Checklist trước khi tin tín hiệu</h2>
          <ul><li><Icon name="check" />Split theo thời gian, không shuffle.</li><li><Icon name="check" />Purging/embargo quanh cửa sổ nhãn.</li><li><Icon name="check" />Transaction cost và slippage thực tế.</li><li><Icon name="check" />So sánh với buy-and-hold.</li><li><Icon name="check" />Không dùng metrics hard-code.</li></ul>
          {result && <div className="backtest-range"><small>Khoảng đánh giá</small><b>{result.from || "—"} → {result.to || "—"}</b><span>Benchmark: {formatPercent(result.benchmarkReturnPct)}</span></div>}
        </article>
      </section>
    </div>
  );
}

interface ResearchViewProps {
  ticker: string;
  mode: ResearchMode;
  question: string;
  answer: string;
  status: string;
  steps: string[];
  error: string;
  citations: Citation[];
  streaming: boolean;
  documentId: string;
  documentName: string;
  uploading: boolean;
  onMode: (mode: ResearchMode) => void;
  onQuestion: (question: string) => void;
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
  onStop: () => void;
  onUpload: (file: File) => void;
}

function ResearchView(props: ResearchViewProps) {
  return (
    <div className="view-content research-view">
      <div className="view-heading"><div><span className="eyebrow">TRACEABLE RESEARCH</span><h1>Copilot workspace · {props.ticker}</h1><p>Token được stream qua BFF; nguồn RAG luôn hiển thị tách biệt với câu trả lời.</p></div><div className={`stream-status ${props.streaming ? "active" : ""}`}><span className="status-dot" />{props.status}</div></div>
      <section className="research-grid">
        <article className="panel research-chat">
          <div className="research-tabs" role="tablist" aria-label="Chế độ nghiên cứu">
            <button type="button" role="tab" aria-selected={props.mode === "copilot"} className={props.mode === "copilot" ? "active" : ""} onClick={() => props.onMode("copilot")}><Icon name="brain" />Market copilot</button>
            <button type="button" role="tab" aria-selected={props.mode === "rag"} className={props.mode === "rag" ? "active" : ""} onClick={() => props.onMode("rag")}><Icon name="document" />Report RAG</button>
          </div>
          {props.mode === "rag" && (
            <label className={`document-upload ${props.documentId ? "ready" : ""}`}>
              <input type="file" accept=".pdf,.txt,.md,.csv" disabled={props.uploading} onChange={(event) => { const file = event.target.files?.[0]; if (file) props.onUpload(file); }} />
              <span><Icon name={props.documentId ? "check" : "upload"} /></span>
              <div><b>{props.uploading ? "Đang lập chỉ mục…" : props.documentName || "Tải báo cáo tài chính"}</b><small>{props.documentId ? `Document ${props.documentId.slice(0, 12)}… sẵn sàng` : "PDF, TXT, MD hoặc CSV · giới hạn do backend áp dụng"}</small></div>
              <em>{props.documentId ? "Đổi tệp" : "Chọn tệp"}</em>
            </label>
          )}
          <div className="conversation">
            {!props.answer && !props.streaming && !props.error && (
              <div className="research-welcome"><span><Icon name="spark" /></span><h2>{props.mode === "rag" ? "Hỏi từ báo cáo, không đoán ngoài nguồn" : "Phân tích đa tín hiệu có trace"}</h2><p>{props.mode === "rag" ? "Tải tài liệu, đặt câu hỏi cụ thể và kiểm tra citation bên cạnh." : "Copilot gọi các công cụ thị trường và stream từng bước đang thực hiện."}</p></div>
            )}
            {props.steps.length > 0 && <div className="tool-steps">{props.steps.map((step) => <span key={step}><Icon name="check" />{step.replaceAll("_", " ")}</span>)}</div>}
            {props.answer && <div className="assistant-answer"><div className="assistant-avatar"><Icon name="brain" /></div><div><small>TRADEMIND · GENERATED ANALYSIS</small><p>{props.answer}</p></div></div>}
            {props.streaming && <div className="thinking-line"><span /><span /><span /> Đang nhận dữ liệu…</div>}
            {props.error && <InlineError title="Research stream gặp lỗi" message={props.error} />}
          </div>
          <form className="research-composer" onSubmit={props.onSubmit}>
            <textarea value={props.question} onChange={(event) => props.onQuestion(event.target.value)} maxLength={2000} placeholder="Đặt câu hỏi có phạm vi và khung thời gian rõ ràng…" />
            <div><span><Icon name="shield" />Không phải khuyến nghị đầu tư</span>{props.streaming ? <button className="stop-button" type="button" onClick={props.onStop}><Icon name="x" />Dừng</button> : <button className="send-button" type="submit"><Icon name="send" />Gửi</button>}</div>
          </form>
        </article>

        <aside className="panel citations-panel">
          <div className="panel-heading simple"><div><span className="section-icon"><Icon name="book" /></span><div><small>EVIDENCE</small><h2>Nguồn trích dẫn</h2></div></div><span className="count-pill">{props.citations.length}</span></div>
          {props.citations.length ? <ol className="citation-list">{props.citations.map((citation, index) => (
            <li key={`${citation.source}-${citation.page}-${citation.chunk}-${index}`}>
              <span>{index + 1}</span><div><b>{citation.source}</b><small>{citation.page ? `Trang ${citation.page}` : "Không rõ trang"}{citation.chunk ? ` · đoạn ${citation.chunk}` : ""}{citation.score !== undefined ? ` · score ${citation.score.toFixed(3)}` : ""}</small>{citation.text && <p>{citation.text}</p>}</div>
            </li>
          ))}</ol> : <EmptyState icon="book" title="Chưa có citation" message={props.mode === "rag" ? "Nguồn sẽ xuất hiện khi retriever tìm thấy bằng chứng." : "Copilot sẽ hiển thị nguồn nếu backend cung cấp."} />}
          <div className="citation-policy"><Icon name="shield" /><div><b>Quy tắc tin cậy</b><span>Nếu câu trả lời không có bằng chứng phù hợp, TradeMind phải nói rõ là chưa đủ dữ liệu.</span></div></div>
        </aside>
      </section>
    </div>
  );
}

function SystemView({ health, healthError, usage, traces, opsError, onRefresh }: { health: HealthStatus | null; healthError: string; usage: Record<string, unknown> | null; traces: Record<string, unknown>[]; opsError: string; onRefresh: () => void }) {
  const calls = numberValue(usage?.calls);
  const inputTokens = numberValue(usage?.input_tokens);
  const outputTokens = numberValue(usage?.output_tokens);
  const cost = numberValue(usage?.estimated_cost_usd);
  return (
    <div className="view-content system-view">
      <div className="view-heading"><div><span className="eyebrow">OPERATIONS</span><h1>System health & AI usage</h1><p>Trạng thái dưới đây đến trực tiếp từ backend; không tự suy diễn khi route unavailable.</p></div><button className="secondary-button" type="button" onClick={onRefresh}><Icon name="refresh" />Làm mới</button></div>
      {(healthError || opsError) && <InlineError title="Một số endpoint vận hành chưa sẵn sàng" message={[healthError, opsError].filter(Boolean).join(" · ")} />}
      <section className="system-grid">
        <article className={`panel health-card ${health?.ok ? "ok" : "warn"}`}>
          <span className="health-orb"><Icon name="health" /></span><small>BACKEND HEALTH</small><h2>{health?.label ?? "Không kết nối được"}</h2><p>{health?.mode ? `Mode: ${health.mode}` : "Backend chưa công bố operating mode."}</p>
          <div className="health-meta"><span><i />BFF same-origin</span><span><Icon name="lock" />Cookie HttpOnly</span></div>
        </article>
        <article className="panel usage-card">
          <div className="panel-heading simple"><div><span className="section-icon"><Icon name="gauge" /></span><div><small>AI LEDGER</small><h2>Usage tổng hợp</h2></div></div></div>
          {usage ? <div className="usage-grid"><div><small>CALLS</small><b>{integer.format(calls)}</b></div><div><small>INPUT TOKENS</small><b>{integer.format(inputTokens)}</b></div><div><small>OUTPUT TOKENS</small><b>{integer.format(outputTokens)}</b></div><div><small>EST. COST</small><b>{formatMoney(cost)}</b></div></div> : <EmptyState icon="gauge" title="Không có usage" message="Ops endpoint chưa trả dữ liệu." />}
        </article>
      </section>
      <article className="panel trace-panel">
        <div className="panel-heading simple"><div><span className="section-icon"><Icon name="activity" /></span><div><small>RECENT ACTIVITY</small><h2>Trace gần đây</h2></div></div><span className="count-pill">{traces.length}</span></div>
        {traces.length ? <div className="trace-list">{traces.map((trace, index) => {
          const events = Array.isArray(trace.events) ? trace.events : [];
          return <div className="trace-row" key={stringValue(trace.request_id, String(index))}><span className="trace-index">{String(index + 1).padStart(2, "0")}</span><div><b>{stringValue(trace.request_id, "Không có request id")}</b><small>{events.length} bước · {formatNumber(numberValue(trace.duration_ms), " ms")}</small></div><em>{formatMoney(numberValue(trace.estimated_cost_usd))}</em></div>;
        })}</div> : <EmptyState icon="activity" title="Chưa có trace" message="Trace sẽ xuất hiện sau khi agent hoặc RAG hoàn tất một request." />}
      </article>
    </div>
  );
}
