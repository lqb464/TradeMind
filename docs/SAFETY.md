# TradeMind safety boundary

## Safety statement

TradeMind v3 là hệ thống **research + proposal + human-approved paper execution**. Nó không kết nối broker và không thể tạo live order. Forecast, sentiment, RAG answer, backtest và decision đều có thể sai; người dùng phải tự thẩm định trước mọi quyết định tài chính.

Safety model của project dựa trên defense in depth: một cờ `execution_eligible` không đủ để tạo order. Proposal phải bind vào đúng `decision_id` người dùng đã xem, proposal và approval có hai idempotency key riêng, người dùng phải phê duyệt rõ ràng, và store phải đọc lại state rồi xác nhận các invariant/risk limit trong transaction approval.

## Invariants hiện được enforce

| Invariant | Enforcement |
|---|---|
| Không live trading | Schema account chỉ chấp nhận `PAPER`; codebase không có broker adapter |
| Agent không tự đặt lệnh | Tool registry chỉ chứa research tools; decision luôn `execution_performed=false` |
| Human approval bắt buộc | Chỉ `POST /api/portfolio/proposals/{id}/approve` có thể tạo paper order |
| Proposal bind vào decision | Backend dựng lại decision; `decision_id` phải khớp chính xác trước khi persist |
| Proposal creation idempotent | Key unique theo account; cùng key + decision trả proposal cũ, decision khác bị từ chối |
| Approval idempotent | `Idempotency-Key` unique theo account; retry cùng proposal trả order cũ |
| Demo/fallback không fill | Marker trong metadata/source bị chặn tại approval |
| Quote phải có provenance | Thiếu `source` hoặc timestamp hợp lệ bị từ chối |
| Quote phải đủ mới | Stale hoặc future-dated quá clock skew bị từ chối |
| Instrument phải được provider xác nhận | Chỉ `EQUITY`/`ETF`, `tradable=true`, exchange có tên, volume dương; halted/delayed bị chặn |
| Currency phải khớp tuyệt đối | Decision, proposal, account, fill quote và portfolio marks dùng cùng mã ISO ba ký tự; không FX conversion |
| Proposal phải còn hợp lệ | Chỉ `PENDING`, chưa hết hạn, executable và không veto mới qua |
| Account controls được tôn trọng | Account phải active PAPER, trading enabled và kill switch tắt |
| Không vượt giá proposal quá xa | Approval kiểm tra quote/reference-price deviation |
| BUY dùng mark toàn portfolio | Mọi position hiện có phải có fresh execution-quality mark trước khi tăng exposure |
| Risk được re-check khi BUY approval | Equity, UTC daily loss, position/gross limits, stop risk và cash được tính lại trong transaction |
| Không âm cash/position | BUY kiểm tra paper cash; SELL kiểm tra long position sẵn có |
| Tenant isolation | Account, proposal, order, watchlist, run và document đều resolve theo authenticated user |
| Bootstrap admin có hai điều kiện | Production yêu cầu đúng configured email và `X-Bootstrap-Token` 32+ byte khi chưa có human user |
| Refresh replay containment | Replay token đã rotate/revoke sẽ revoke mọi refresh session còn active của user |

## Trust boundaries

### Market data

Nguồn market/news bên ngoài là dữ liệu không tin cậy. Snapshot phải qua `assess_market_snapshot` trước khi được xem là execution-eligible. Quality contract kiểm tra:

- ticker khớp request;
- giá và volume hợp lệ;
- candle rows có OHLCV nhất quán;
- completeness đạt ngưỡng;
- `as_of` parse được, có timezone, không stale/future quá giới hạn;
- source/provenance tồn tại;
- quote currency là mã ba ký tự;
- exchange tồn tại, instrument type là provider-confirmed `EQUITY` hoặc `ETF` và `tradable=true`;
- upstream không đánh dấu delayed, halted/suspended hoặc execution-ineligible;
- demo/synthetic/fallback markers.

Research endpoint vẫn có thể trả demo snapshot để UI hoạt động khi provider lỗi. Đây là degradation có chủ đích; metadata phải khiến decision chuyển sang `OBSERVE`, quality `FAIL` và approval từ chối.

### LLM và nội dung tài liệu

LLM không phải policy engine. Nó chỉ tổng hợp một decision đã được tạo bằng code deterministic. LLM không thể:

- thêm tool ngoài registry;
- thay đổi contribution, risk sizing hoặc veto đã tính;
- approve proposal;
- ghi cash, position hoặc order;
- bật/tắt kill switch.

News, tài liệu RAG và tool output có thể chứa prompt injection. System prompts coi chỉ dẫn bên trong source là dữ liệu không đáng tin. RAG chỉ gửi những chunk vượt relevance threshold và trả no-answer khi không có evidence đủ liên quan. Đây là giảm thiểu rủi ro, không phải bảo đảm chống mọi prompt injection; output vẫn cần được xem là untrusted text.

### Browser và API

Dashboard dùng same-origin BFF. Token được lưu trong `HttpOnly`, `Secure`, `SameSite=Strict` cookie; mutation khác origin bị từ chối. FastAPI vẫn xác thực bearer token và owner scope ở server-side.

Production cần TLS end-to-end, secure reverse proxy, secret manager, trusted-host/proxy configuration, dependency scanning và penetration testing. Cấu hình cookie/BFF hiện không phải bằng chứng security certification.

ASGI body-limit middleware kiểm tra `Content-Length` khi có và đếm byte thực nhận trước khi multipart parser chạy. Request mutation thông thường bị giới hạn 2 MiB; `/api/documents` dùng `MAX_UPLOAD_MB + 256 KiB` để chừa multipart overhead. Payload vượt giới hạn nhận `413`. Đây là application backstop, không thay thế `client_max_body_size`/request-body limit tại reverse proxy: ingress vẫn phải chặn body lớn trước khi truyền vào Uvicorn.

## Decision và risk gates

Decision engine là long-only. Tùy score và account state, action có thể là `BUY`, `HOLD`, `REDUCE` hoặc `OBSERVE`. Chỉ `BUY`/`REDUCE` có quantity dương, quality pass và không veto mới có thể đặt `execution_eligible=true`.

Các veto hiện bao gồm dữ liệu/evidence không đủ, anomaly nghiêm trọng, currency mismatch, portfolio marks không đủ chất lượng và account-risk conditions như kill switch, trading disabled, daily loss hoặc equity không hợp lệ. BUY size bị giới hạn đồng thời bởi risk budget, stop distance, max position, max gross exposure, cash và optional notional cap.

Risk sizing bảo vệ trước một số lỗi định lượng, nhưng không có FX conversion: account currency phải khớp chính xác quote currency. Paper fill chưa mô phỏng bid/ask spread, execution slippage, liquidity/market impact hoặc partial fill; đồng thời chưa xử lý corporate actions, exchange calendar/session, tax lots, borrow hay counterparty risk. Backtest có mức slippage cấu hình đơn giản nhưng không thay thế execution simulator.

## Proposal binding và idempotency

Client trước tiên đọc một decision và hiển thị `decision_id`, risk policy, evidence, quality và veto. Khi tạo proposal, client phải gửi:

- cùng ticker/account và risk parameters;
- `decision_id` vừa hiển thị;
- một `Idempotency-Key` dành riêng cho mutation tạo proposal.

Backend không tin payload decision từ client. Nó lấy lại account/portfolio marks, dựng lại decision bằng code deterministic và so khớp exact `decision_id`. Nếu data, account, mark, score, action, veto hoặc sizing làm fingerprint thay đổi, request bị từ chối và client phải refresh decision. Proposal persist toàn bộ decision cùng immutable risk policy, quote currency, decision ID, creation key và expiry.

Proposal idempotency key unique theo account. Retry cùng key cho cùng decision trả proposal đã tồn tại với `idempotent_replay=true`; cùng key nhưng decision ID khác trả conflict. Approval dùng một key khác: retry cùng approval key + proposal trả cùng paper order, còn tái sử dụng key cho proposal khác bị từ chối. Hai key không nên được dùng lẫn nhau dù database kiểm soát chúng ở hai mutation boundary riêng.

## Approval boundary

Approval không dùng lại giá proposal một cách mù quáng. API re-fetch quote cho instrument cần fill; với BUY, nó còn fetch mark cho mọi position hiện có. Bên trong một SQLite transaction, store đọc lại proposal/account/positions, canonical-validate các snapshot được cung cấp và kiểm tra lại:

1. authenticated user sở hữu proposal/account;
2. approval idempotency key hợp lệ và chưa dùng cho proposal khác;
3. proposal còn `PENDING` và chưa hết hạn;
4. account active, mode `PAPER`, trading enabled, kill switch off;
5. proposal executable, không veto, side là BUY/SELL và có immutable risk policy hợp lệ;
6. quote có volume dương, provider-confirmed `EQUITY`/`ETF`, `tradable=true`, exchange/provenance/timestamp đầy đủ và không delayed/halted/demo/fallback;
7. ticker khớp, quote không stale/future quá policy và currency khớp account chính xác;
8. giá không vượt `MAX_PRICE_DEVIATION_PCT` so với reference;
9. với BUY, mọi position có mark execution-quality cùng currency; equity, target position và gross exposure được định giá lại;
10. với BUY, UTC daily loss, per-position, gross-exposure và actual stop-risk vẫn nằm trong immutable policy;
11. paper cash đủ cho BUY hoặc long position đủ cho SELL.

Network quote fetch diễn ra trước transaction để không giữ database lock trong khi chờ provider; tuy nhiên không snapshot nào được tin trước khi store validation chạy bên trong approval transaction. Risk re-check, daily baseline/P&L update, cash, position, order và proposal status sau đó được commit hoặc rollback cùng nhau. Order là simulated market fill ngay tại validated quote; không mô phỏng spread, execution slippage, exchange acknowledgement hoặc partial fills.

## Portfolio marks, currency và UTC daily baseline

Account summary cố gắng mark **mọi** position. Mỗi mark được canonical-validate như execution snapshot và currency phải bằng account currency. Nếu bất kỳ quote nào thiếu/không execution-quality/currency mismatch, summary gắn position đó `display-only`, đặt `portfolio_marks_execution_eligible=false` và không cho BUY tăng exposure. Giá average cost có thể được dùng để hiển thị fallback nhưng không được xem là execution-quality mark.

TradeMind không quy đổi currency. Một USD account không thể tạo proposal/fill từ quote VND/EUR, và portfolio chứa mark khác currency không được dùng để tính risk. Muốn hỗ trợ đa tiền tệ cần một FX-rate source có provenance/freshness, conversion ledger và reconciliation riêng; hiện chưa có.

Daily risk dùng ngày UTC, không giả định exchange session. Khi tạo baseline cho một ngày UTC mới, hệ thống lấy `max(current trusted equity, prior trusted latest_equity)`; nếu chưa có ngày trước thì dùng `max(current trusted equity, initial_cash)`. Vì vậy gap tăng trước first mark không tạo “profit buffer”, trong khi gap giảm vẫn được tính là loss. Opening equity sau đó giữ cố định trong ngày. Daily P&L chỉ được cập nhật khi toàn bộ portfolio có trustworthy marks. Trong BUY approval, equity toàn portfolio được mark lại, daily P&L được cập nhật atomically và so với `opening_equity × max_daily_loss_pct`. Đây là session baseline đơn giản; chưa có exchange calendar, holiday/early-close hay timezone-specific trading day.

## Kill switch và trading controls

Mỗi paper account có hai control:

- `trading_enabled=false` chặn approval;
- `kill_switch=true` chặn approval ngay cả khi trading enabled.

Mọi thay đổi control cần `reason` và được lưu thành control event với giá trị cũ/mới. Control không xóa proposal đang pending; chúng làm approval của proposal đó fail cho đến khi policy cho phép trở lại hoặc proposal hết hạn.

Ví dụ tắt trading:

```http
POST /api/portfolio/accounts/{account_id}/controls
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "trading_enabled": false,
  "kill_switch": true,
  "reason": "Manual safety stop during data incident"
}
```

## Authentication và tenant isolation

- Password tối thiểu 10 ký tự và được hash bằng scrypt.
- Access/refresh token có issuer và token type riêng.
- Refresh token được rotate; token cũ đã dùng bị revoke. Nếu token đã rotate/revoke bị replay, transaction revoke mọi refresh token còn active của user rồi trả `401`. Access token đã phát hành không bị tra cứu session mỗi request và vẫn sống tới TTL trừ khi signing secret được rotate.
- First human user là `ADMIN`; ở production request phải khớp cả `BOOTSTRAP_ADMIN_EMAIL` và secret header `X-Bootstrap-Token`. `BOOTSTRAP_ADMIN_TOKEN` phải khác placeholder và dài ít nhất 32 byte.
- Sau bootstrap admin, public registration ở production mặc định tắt; chỉ `ALLOW_PUBLIC_REGISTRATION=true` mới bật self-signup.
- Admin-only ops endpoints không cấp quyền truy cập portfolio/document của user khác.
- Khi `AUTH_REQUIRED=false`, hệ thống dùng một local identity/account cố định; mode này chỉ dành cho single-user development.

SQLite/filesystem isolation là application-level isolation, không phải database row-level security. Không nên chạy cấu hình auth-off hoặc shared filesystem writable bởi user không tin cậy.

## Operational fail-closed behavior

- Production từ chối startup nếu auth bị tắt, JWT secret là mặc định/quá ngắn, thiếu bootstrap admin email, hoặc bootstrap token là placeholder/ngắn hơn 32 byte.
- Market provider lỗi có thể làm research fallback, nhưng không mở đường execution.
- LLM key thiếu/provider lỗi chuyển sang offline synthesis và ghi fallback reason.
- Mỗi user có daily AI call/token budget với reservation nguyên tử trước SSE; input/output của provider cũng bị giới hạn.
- RAG ingest áp quota số document và tổng ký tự theo owner bên cạnh giới hạn từng file.
- Migration decision-binding sẽ expire mọi legacy `PENDING` proposal thiếu `decision_id`, proposal idempotency key hoặc quote currency; proposal cũ đó không thể được grandfather vào approval.
- Redis lỗi chuyển cache về memory; source of truth không bị thay đổi.
- SSE exception trả event lỗi nói rõ không có order được tạo.
- Storage không khả dụng làm health `degraded`; mutation không được xem là thành công.

## Incident response tối thiểu

Khi nghi ngờ dữ liệu, model hoặc session bị compromise:

1. Bật `kill_switch=true` và/hoặc `trading_enabled=false` cho account liên quan.
2. Dừng public ingress nếu sự cố liên quan auth/secrets.
3. Giữ lại SQLite database, application logs, request IDs và control events để điều tra; không chỉnh trực tiếp ledger trước khi sao lưu.
4. Rotate `TRADEMIND_JWT_SECRET` nếu token signing key lộ. Việc này vô hiệu hóa JWT đang tồn tại; đồng thời xử lý refresh-token store theo runbook sự cố của môi trường.
5. Kiểm tra `/api/health`, `/api/ops/traces`, `/api/ops/usage`, order history và agent-run history.
6. Chỉ mở lại controls sau khi provider/provenance và regression tests đạt yêu cầu.

TradeMind chưa có global kill switch đa account, session-revocation endpoint cho admin, centralized SIEM hay automated incident playbook.

## Không được hiểu sai

- `confidence` không phải xác suất có lời.
- Forecast interval không phải cam kết giá mục tiêu.
- Backtest performance không chứng minh future performance.
- RAG citation chỉ chứng minh text được truy xuất, không chứng minh báo cáo đúng hoặc kết luận hoàn chỉnh.
- `PAPER` fill không phản ánh khả năng fill thực tế.
- Currency matching chỉ ngăn cộng/trừ sai đơn vị; nó không thực hiện FX conversion hoặc hedging.
- Health `ok` nghĩa là các dependency cốt lõi phản hồi, không phải mọi dữ liệu/model đều chính xác.

## Điều kiện tối thiểu trước live execution trong tương lai

Live trading không nên được thêm bằng một flag. Một project riêng cần ít nhất broker sandbox/certification, scoped credentials, order state machine, pre-trade limits độc lập, exchange calendar, quote SLA, currency-conversion/reconciliation policy, spread/slippage/partial-fill/cancel handling, corporate-action processing, global kill switch, alerting/on-call, immutable audit, backup/restore drills, security review, legal/compliance review và staged rollout. Cho tới khi các điều kiện đó được xây và kiểm thử, invariant “paper only” phải được giữ nguyên.
