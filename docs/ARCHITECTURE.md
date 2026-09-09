# TradeMind v3 architecture

## Mục tiêu thiết kế

TradeMind là một modular monolith local-first cho nghiên cứu và paper trading. Kiến trúc tối ưu cho ba đặc tính:

1. **Evidence first:** mọi signal phải truy ngược được về market snapshot, model/baseline và thời điểm dữ liệu.
2. **Deterministic control:** data-quality gate, risk sizing, veto và approval không do LLM quyết định.
3. **Separated authority:** research, proposal, human approval và paper fill là các bước khác nhau.

Đây chưa phải kiến trúc production multi-node. SQLite và filesystem document store phù hợp cho một instance; Redis chỉ là cache tùy chọn.

## System context

```text
┌─────────────────────────────┐
│ User / analyst              │
└──────────────┬──────────────┘
               │ browser, same-origin REST + SSE
               ▼
┌─────────────────────────────┐
│ Next.js workstation + BFF   │
│ - UI                        │
│ - HttpOnly token cookies    │
│ - refresh rotation proxy    │
└──────────────┬──────────────┘
               │ bearer-authenticated upstream calls
               ▼
┌─────────────────────────────────────────────────────────┐
│ FastAPI modular monolith                                │
│ Auth │ Market │ Agent │ Decision/Risk │ RAG │ Portfolio │
└──────┬─────────┬──────────┬───────────────┬──────────────┘
       │         │          │               │
       ▼         ▼          ▼               ▼
  SQLite     Market/news  Redis or       RAG JSON files
  source of  providers    memory cache   + optional artifacts
  truth
```

External market/news and OpenAI-compatible endpoints are optional dependencies. Khi chúng không khả dụng, research surface có thể dùng fallback được đánh dấu; paper approval fail-closed với dữ liệu đó.

## Thành phần runtime

### Next.js workstation và BFF

`frontend/src/app/api/[...path]/route.ts` là proxy cùng origin:

- Không trả access/refresh token cho JavaScript UI sau login/register.
- Lưu token trong cookie `HttpOnly`, `Secure`, `SameSite=Strict`.
- Gắn bearer token khi gọi FastAPI và thử rotate refresh token sau response `401`.
- Chặn request thay đổi trạng thái khi header `Origin` không cùng origin.
- Forward REST và SSE, đặt response `no-store`.

BFF giảm việc token xuất hiện trong browser code; nó không thay thế TLS, reverse-proxy hardening, CSP review hay CSRF/security testing chuyên sâu.

### FastAPI boundary

`backend/main.py` lắp các router và middleware:

- Auth và portfolio routes nằm tại `backend/api/`.
- Market, intelligence, decision, backtest, document, RAG, copilot và ops routes nằm tại main API.
- `X-Request-ID`, response time, `nosniff` và no-referrer headers được thêm cho mỗi response.
- GZip áp dụng cho response lớn; CORS dùng allow-list từ cấu hình.
- ASGI receive middleware giới hạn request thường ở 2 MiB và document upload ở `MAX_UPLOAD_MB + 256 KiB`, trước multipart parsing.
- OpenAPI/ReDoc bị tắt khi `APP_ENV` là `prod`/`production`.

### Security và identity

`backend/core/security.py` cung cấp:

- Scrypt password hashing với salt ngẫu nhiên.
- JWT representation khóa cứng thuật toán HMAC-SHA256, có issuer, token type, `iat`, `exp`, subject và token id.
- Access token ngắn hạn, refresh token dài hạn.
- Production startup validation bắt buộc auth, JWT secret riêng 32+ byte, bootstrap email và bootstrap token riêng 32+ byte.
- Admin đầu tiên chỉ được tạo khi email khớp cấu hình và request có `X-Bootstrap-Token` đúng; các lần đăng ký sau tuân theo public-registration policy.

`backend/src/store.py` chỉ lưu fingerprint của refresh token; rotate sẽ revoke token cũ. Nếu một token đã rotate/revoke bị replay, cùng transaction revoke mọi refresh session còn active của user và refresh API trả `401`. Access JWT không bị session lookup mỗi request nên vẫn sống tới TTL của nó. Mọi truy vấn portfolio/document nhận `user_id` từ identity đã xác thực, không tin `user_id` do client gửi.

### Storage

SQLite là source of truth cho:

- users và refresh tokens;
- user watchlists;
- paper accounts, positions và orders;
- trade proposals và agent runs;
- account-control events;
- AI usage ledger.

Store bật foreign keys, WAL và migration ledger. Các mutation liên quan approval, cash, position, proposal và order chạy trong một transaction để tránh trạng thái fill nửa chừng.

Safety migration expire các legacy proposal còn `PENDING` nhưng thiếu decision binding, creation idempotency key hoặc quote currency. Chúng không được grandfather vào approval contract mới.

Document RAG được lưu thành JSON riêng theo UUID dưới `DATA_DIR/rag`. Mỗi document mang `owner_id`, filename đã sanitize, parser, OCR flag, SHA-256, thời điểm tạo và chunks. Đây là filesystem store một-node; không có distributed locking/object-store durability.

### Cache

`backend/src/cache.py` ưu tiên Redis khi cấu hình được kết nối. Nếu Redis lỗi lúc khởi động hoặc runtime, hệ thống chuyển sang thread-safe in-memory TTL cache. Per-key locking giảm cache stampede. Cache không chứa ledger giao dịch và không phải source of truth.

## Market data và provenance

`backend/src/intelligence.py` lấy OHLCV từ nguồn ngoài khi có thể, sau đó tính indicators. Nếu provider thất bại, deterministic demo series giúp research UI vẫn hoạt động nhưng metadata ghi rõ `is_demo`, fallback reason và `execution_eligible=false`.

Mỗi snapshot cung cấp metadata đủ để đánh giá:

- `source` và fallback reason;
- `as_of` có timezone;
- `is_demo`;
- freshness/completeness;
- execution eligibility.

`backend/src/market_quality.py` canonicalize snapshot, kiểm tra ticker, giá/volume, candle OHLCV, provenance, timestamp, quote currency, exchange, instrument type và completeness. Execution-quality yêu cầu provider xác nhận `EQUITY`/`ETF`, `tradable=true`, volume dương và không delayed/halted. Kết quả gồm `snapshot_id`, quality score, errors/warnings, freshness và status `PASS`/`FAIL`.

## Decision engine

`backend/src/trading.py` tạo decision có cấu trúc, long-only và proposal-only. Luồng chính:

```text
snapshot ───────► quality assessment ─────────────┐
technicals ─────► contribution (weight 0.40)      │
forecast ───────► contribution (weight 0.35)      ├─► ensemble score
news ───────────► contribution (weight 0.15)      │
anomalies ──────► contribution (weight 0.10) ─────┘
                                                        │
account + risk limits ─► vetoes + sizing ◄──────────────┘
                                                        │
                                                        ▼
                                      BUY / HOLD / REDUCE / OBSERVE
```

Không đủ evidence, severe anomaly, fail quality, currency mismatch, unsafe portfolio marks, kill switch, trading disabled, daily-loss policy hoặc invalid equity có thể thêm veto/chuyển thành `OBSERVE`. BUY sizing lấy minimum capacity từ:

- risk budget / stop distance;
- remaining position limit;
- remaining gross-exposure limit;
- available paper cash;
- optional order-notional cap.

Stop distance ưu tiên mức bảo thủ nhất trong minimum policy stop, ATR stop và realized-volatility stop, sau đó bị giới hạn bởi policy min/max. REDUCE chỉ giảm long position; engine không mở short.

Decision trả `decision_id`, version, timestamp, contribution scores/rationale, vetoes, sizing calculation, evidence, market quality, confidence và `execution_eligible`. Nó luôn trả `execution_performed=false`.

## Agent orchestration

`backend/src/agent.py` có planner heuristic chọn trong năm tool allow-listed:

- `market_data`;
- `technical_indicators`;
- `forecast`;
- `anomalies`;
- `news_sentiment`.

Các tool được chạy song song ngoài async event loop. Kết quả đi qua deterministic decision engine trước khi provider LLM được gọi. Provider chỉ nhận evidence compact và decision để viết phần giải thích; system prompt cấm thay đổi risk gate hoặc tuyên bố đã đặt lệnh.

SSE event contract gồm `plan`, `tool`, `tool_error`, `decision`, `token`, `done` hoặc `error`. Provider response ghi `provider` và `fallback_reason`; usage chỉ được persist một lần tại HTTP streaming boundary.

## Proposal, approval và paper fill

```text
POST /portfolio/proposals
  ├─ load owner-scoped account
  ├─ require displayed decision_id + proposal Idempotency-Key
  ├─ build fresh deterministic decision and compare exact ID
  ├─ persist PENDING proposal + immutable risk policy/currency/expiry
  └─ persist agent run

POST /portfolio/proposals/{id}/approve + Idempotency-Key
  ├─ verify proposal/account owner
  ├─ return existing order when same key + same proposal
  ├─ require PENDING, not expired, executable, no veto
  ├─ require active PAPER account and controls allow trading
  ├─ re-fetch fill quote; for BUY, fetch every current position mark
  ├─ reject demo/fallback/stale/future/delayed/halted/untradable quote
  ├─ require provider-confirmed EQUITY/ETF, positive volume and exact currency match
  ├─ reject price deviation beyond configured limit
  ├─ BUY: atomically revalue portfolio + UTC daily P&L + all risk limits
  ├─ verify actual stop risk, paper cash or position
  └─ atomically update daily risk/cash/position/proposal + insert FILLED PAPER order
```

Proposal creation và approval dùng hai idempotency key riêng. Cùng creation key + decision trả proposal cũ, nhưng cùng key với decision khác bị conflict; approval có semantics tương tự theo proposal/order. Quote fetch xảy ra ngoài database transaction, sau đó store canonical-validate toàn bộ snapshot và đọc lại proposal/account/positions trong transaction trước khi re-check risk và commit mutation.

Daily risk chia theo ngày UTC. Khi bắt đầu ngày mới, opening baseline là `max(current trusted equity, prior trusted latest_equity)` hoặc, nếu chưa có lịch sử, `max(current trusted equity, initial_cash)`. Điều này không tạo profit buffer từ gap tăng trước first mark nhưng vẫn ghi nhận gap giảm. Baseline giữ nguyên trong ngày; daily latest equity/P&L chỉ được cập nhật từ portfolio marks đủ execution quality.

Không có module nào gọi broker. Account currency phải khớp chính xác currency của decision/fill/mọi portfolio mark; không có FX conversion. Một paper order hiện được mô phỏng fill ngay tại quote hợp lệ lúc approval; chưa có bid/ask spread, execution slippage, liquidity/market impact, order book, partial fill, latency model, corporate-action processing hoặc exchange-calendar/session simulator.

## RAG pipeline

```text
PDF/TXT/MD/CSV
   │ sanitize + size/type limits
   ▼
parse text / optional OCR
   │ page-aware chunks + overlap
   ▼
owner-scoped JSON index
   │
   ├─ BM25 rankings
   ├─ TF-IDF n-gram rankings
   └─ optional trained dense retriever
             │
             ▼
      Reciprocal Rank Fusion
             │ relevance threshold + adjacent context
             ▼
       citations / explicit no-answer
             │
             ▼
     LLM or grounded offline synthesis
```

Source content được xem là dữ liệu không đáng tin trong prompt, không phải instruction. Nếu retrieval không vượt relevance threshold, API trả `grounded=false`, `reason=no_relevant_context` và không bịa answer. Ingest đồng thời enforce quota số document và tổng ký tự theo owner từ cấu hình, ngoài giới hạn từng file/chunk.

## Forecast, artifacts và backtest

Forecast runtime ưu tiên artifact tương thích theo ticker. Nếu không có, statistical fallback tạo interval và metrics từ point-in-time EWMA backtest. Artifact training dùng manifest/checksum để tránh nạp nhầm file không phù hợp.

Backtest dùng target exposure long-only. Signal của hàng `t` chỉ áp dụng cho return từ close `t` đến close `t+1`; phí và một mức slippage cấu hình đơn giản tính trên thay đổi exposure. Output có equity curve, benchmark, CAGR, Sharpe, Sortino, max drawdown, win rate, turnover và trade count. Nó vẫn chỉ là historical research: không có FX conversion, bid/ask spread, liquidity/market impact, partial fill, exchange calendar, corporate-action reconciliation, borrow, taxes hoặc survivorship-bias handling đầy đủ.

## Observability

- Health endpoint công bố storage, cache, LLM provider và execution modes.
- Request ID nối HTTP response với log/agent trace.
- Agent trace ghi thời gian tool step và estimated cost.
- Usage ledger ghi model/token/cost khi provider trả usage.
- Mỗi user phải reserve atomically ngân sách AI call/token theo ngày UTC trước khi mở stream; provider giới hạn input chars/output tokens.
- Admin-only ops routes cung cấp recent traces và usage summary.
- Account-control events ghi old/new control values, actor và reason.

Logging hiện là process log và SQLite ledger; chưa phải centralized telemetry hoặc immutable compliance audit.

## Deployment topology

Docker Compose chạy ba service:

- `frontend`: Next.js standalone, non-root;
- `backend`: FastAPI/Uvicorn, non-root, persistent `/data` và read-only training artifacts;
- `redis`: Redis 7 với append-only volume.

Frontend đợi backend healthy; backend đợi Redis healthy. Compose có restart policy, health checks và log rotation. Đây là developer/single-host topology, chưa bao gồm TLS ingress, backups, HA, autoscaling hoặc secret manager. Reverse proxy ngoài Compose phải enforce client-body limit; ASGI limiter không bảo vệ network/worker resources trước khi payload đến application process.

## Extension rules

Khi mở rộng hệ thống, giữ các invariant sau:

1. LLM không được nhận quyền gọi execution tool.
2. Market fallback phải được gắn nhãn và luôn non-executable.
3. Proposal và approval không được gộp thành một API action.
4. Proposal phải bind vào displayed `decision_id`; proposal và approval có idempotency key riêng.
5. Approval luôn đọc lại state, validate fill/portfolio marks và re-check risk trong transaction.
6. Tất cả dữ liệu người dùng phải filter bằng identity server-side.
7. Live broker, nếu có trong tương lai, phải là boundary riêng và không tái sử dụng trực tiếp paper-fill semantics.

Chi tiết vận hành nằm trong [RUNBOOK.md](RUNBOOK.md), còn threat/safety model nằm trong [SAFETY.md](SAFETY.md).
