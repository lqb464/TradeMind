# TradeMind v3 operations runbook

Runbook này dành cho local development và single-host evaluation. Nó không biến TradeMind thành production deployment guide; các mục “production-like” chỉ là minimum hygiene trước khi cho người khác truy cập.

## Prerequisites

### Docker path

- Docker Desktop/Engine với Compose v2.
- Khoảng trống đủ cho Python/Node images và Redis volume.

### Local path

- Python 3.11 hoặc 3.12.
- Node.js 20.9+; CI/container dùng Node 22.
- pnpm 11.19 qua Corepack.
- Redis tùy chọn.

## Cấu hình ban đầu

Tạo file cấu hình local:

```bash
cp .env.example .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
```

Ít nhất hãy kiểm tra các biến sau:

```dotenv
APP_ENV=development
AUTH_REQUIRED=true
ALLOW_PUBLIC_REGISTRATION=false
TRADEMIND_JWT_SECRET=replace-with-a-long-random-secret-before-sharing
BOOTSTRAP_ADMIN_EMAIL=your-admin@example.com
BOOTSTRAP_ADMIN_TOKEN=replace-with-an-independent-32-plus-byte-secret
TRADEMIND_DB_PATH=./data/trademind.db
```

Không commit `.env`. Khi `APP_ENV=production`, backend fail startup nếu `AUTH_REQUIRED=false`, JWT secret mặc định/ngắn hơn 32 byte, thiếu bootstrap admin email, hoặc bootstrap token vẫn là placeholder/ngắn hơn 32 byte. JWT secret và bootstrap token phải là hai giá trị độc lập; có thể tạo secret bằng password manager hoặc CSPRNG, ví dụ `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

Sau khi bootstrap user đầu tiên, production mặc định từ chối self-registration. Chỉ đặt `ALLOW_PUBLIC_REGISTRATION=true` khi operator chủ động muốn public signup. Các giới hạn AI/RAG nên được điều chỉnh bằng `MAX_AI_TOKENS_PER_USER_PER_DAY`, `MAX_AI_CALLS_PER_USER_PER_DAY`, `MAX_RAG_DOCUMENTS_PER_USER` và `MAX_RAG_CHARACTERS_PER_USER`; giới hạn mỗi prompt/response dùng `LLM_MAX_INPUT_CHARS` và `LLM_MAX_OUTPUT_TOKENS`.

LLM là tùy chọn. Để chạy hoàn toàn offline:

```dotenv
LLM_PROVIDER=offline
LLM_API_KEY=
```

Để dùng OpenAI-compatible endpoint, cấu hình `LLM_API_KEY`, `LLM_MODEL` và `LLM_BASE_URL`. Risk/approval logic không phụ thuộc LLM.

## Khởi động bằng Docker Compose

```bash
docker compose config --quiet
docker compose up --build
```

Chạy nền:

```bash
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 backend
docker compose logs --tail=100 frontend
```

Endpoints:

- `http://localhost:3000`: workstation;
- `http://localhost:8000/api/health`: health JSON;
- `http://localhost:8000/docs`: OpenAPI khi không chạy production mode.

Dừng services nhưng giữ volumes:

```bash
docker compose down
```

Không dùng `docker compose down -v` trừ khi chủ động muốn xóa toàn bộ SQLite/RAG/Redis data của environment đó.

## Khởi động local

Tạo virtual environment và cài runtime/test dependencies:

```bash
python -m venv .venv
# PowerShell: .\.venv\Scripts\Activate.ps1
# bash/zsh:   source .venv/bin/activate
python -m pip install -r backend/requirements-test.txt
```

Sau khi virtualenv đã được activate, chạy backend từ repository root:

```bash
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000 --env-file .env
```

Nếu không chạy Redis, có thể để `REDIS_URL` trỏ tới Redis không tồn tại; health sẽ cho biết cache đã chuyển sang in-process memory fallback.

Chạy frontend ở terminal khác:

```bash
cd frontend
corepack enable
pnpm install --frozen-lockfile
pnpm dev
```

`API_ORIGIN` mặc định là `http://127.0.0.1:8000`, phù hợp với hai process local. Nếu backend ở host khác, đặt biến này cho process Next.js, chẳng hạn trong `frontend/.env.local`.

## Bootstrap user và account

Mở workstation và đăng ký tài khoản. User đầu tiên được tạo là `ADMIN` và tự có account `Primary Paper`.

Development API có thể nhận cùng request mà không bắt buộc bootstrap header. Với production database chưa có human user, đăng ký admin đầu tiên bắt buộc đúng configured email và header secret:

```bash
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -H "X-Bootstrap-Token: $BOOTSTRAP_ADMIN_TOKEN" \
  -d '{"email":"admin@example.com","password":"change-this-password","name":"TradeMind Admin"}'
```

Response chứa access/refresh token nếu gọi FastAPI trực tiếp. Khi dùng workstation, BFF chuyển token thành HttpOnly cookies và chỉ trả user/session metadata cho UI.

Ở production mode, email đăng ký đầu tiên phải khớp `BOOTSTRAP_ADMIN_EMAIL` và token phải khớp `BOOTSTRAP_ADMIN_TOKEN` bằng constant-time comparison. Không đặt bootstrap token trong frontend bundle. Các user đăng ký sau có role `USER`, nhưng route đăng ký công khai chỉ mở sau bootstrap nếu `ALLOW_PUBLIC_REGISTRATION=true`; bootstrap header không bypass policy này.

Refresh token rotate một lần. Nếu một refresh token đã rotate/revoke bị replay, backend revoke mọi refresh session còn active của user và trả `401`; người dùng phải đăng nhập lại trên tất cả thiết bị. Access token hiện hữu vẫn hết hạn theo access TTL.

## Kiểm tra health

```bash
curl http://localhost:8000/api/health
curl http://localhost:8000/api/config
```

Health payload nên cho biết:

- `status=ok` khi SQLite storage truy cập được;
- `storage=sqlite` và trạng thái cache hiện tại;
- provider LLM hiện dùng;
- `execution.paper_enabled=true`;
- `execution.live_enabled=false`;
- `execution.human_approval_required=true`;
- `execution.demo_data_blocked=true`.

## Workflow paper trading qua API

Các ví dụ sau giả định biến shell `TOKEN`, `ACCOUNT_ID`, `DECISION_ID` và `PROPOSAL_ID` đã được gán từ response trước đó. Dùng hai key khác nhau cho proposal creation và approval.

Lấy account summary:

```bash
curl http://localhost:8000/api/portfolio/account \
  -H "Authorization: Bearer $TOKEN"
```

Tạo decision read-only:

```bash
curl "http://localhost:8000/api/trade/decision/AAPL?account_id=$ACCOUNT_ID" \
  -H "Authorization: Bearer $TOKEN"
```

Copy `decision_id` từ response vào `DECISION_ID`. Tạo proposal riêng biệt với một idempotency key dành cho creation:

```bash
curl -X POST http://localhost:8000/api/portfolio/proposals \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: propose-$DECISION_ID" \
  -d "{\"ticker\":\"AAPL\",\"account_id\":\"$ACCOUNT_ID\",\"decision_id\":\"$DECISION_ID\",\"risk_fraction\":0.01,\"max_position_fraction\":0.10}"
```

Backend dựng lại decision; nếu `decision_id` không còn khớp, refresh decision và dùng key mới thay vì ép tạo proposal cũ. Retry cùng creation key + decision trả proposal hiện có; tái sử dụng key đó với decision khác bị từ chối.

Kiểm tra `status`, `executable`, `vetoes`, `reference_price`, `quote_currency`, `quantity`, immutable risk policy và expiry. Approval là hành động người dùng riêng với một idempotency key khác:

```bash
curl -X POST "http://localhost:8000/api/portfolio/proposals/$PROPOSAL_ID/approve" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: fill-$PROPOSAL_ID" \
  -d '{"note":"Reviewed data provenance and paper risk"}'
```

Nếu data provider đang fallback/demo/delayed/halted, instrument không phải provider-confirmed equity/ETF, volume không dương, currency không khớp, hoặc bất kỳ position nào thiếu fresh execution-quality mark trước BUY, approval phải trả conflict thay vì fill. Đây là hành vi đúng. Gọi lại cùng approval idempotency key cho proposal đã fill trả lại cùng order; không tạo order thứ hai.

Daily risk dùng ngày UTC. Ở first trustworthy mark của ngày mới, opening baseline là giá trị lớn hơn giữa current trusted equity và prior trusted latest equity; nếu chưa có ngày trước thì so với initial cash. Cách này không cấp profit buffer từ gap tăng trước first mark, nhưng vẫn ghi nhận gap giảm là daily loss.

Reject proposal:

```bash
curl -X POST "http://localhost:8000/api/portfolio/proposals/$PROPOSAL_ID/reject" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"note":"Evidence is insufficient"}'
```

## Kill switch và control history

Bật kill switch khi có sự cố dữ liệu hoặc cần ngừng approval:

```bash
curl -X POST "http://localhost:8000/api/portfolio/accounts/$ACCOUNT_ID/controls" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"trading_enabled":false,"kill_switch":true,"reason":"Manual safety stop"}'
```

Đọc audit events:

```bash
curl "http://localhost:8000/api/portfolio/accounts/$ACCOUNT_ID/control-events" \
  -H "Authorization: Bearer $TOKEN"
```

Chỉ mở lại sau khi nguyên nhân được xử lý và tests/evaluation cần thiết đã chạy. Reason nên gắn incident/reference để truy vết.

## RAG ingest và hỏi đáp

Upload report:

```bash
curl -X POST http://localhost:8000/api/documents \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@report.pdf"
```

Hỏi qua SSE:

```bash
curl -N -X POST http://localhost:8000/api/rag/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question":"Rủi ro chính được ban lãnh đạo nêu là gì?"}'
```

Core parser hỗ trợ PDF có text, TXT, MD và CSV. Muốn OCR PDF scan:

```bash
python -m pip install -r backend/requirements-ocr.txt
```

Theo dõi `parser`, `used_ocr`, `sha256`, page/chunk citation và `grounded`. `grounded=false` với `no_relevant_context` là kết quả hợp lệ, không phải lỗi hạ tầng.

Backend chặn request body trong ASGI receive loop trước multipart parse. Request thường tối đa 2 MiB; upload route cho phép `MAX_UPLOAD_MB + 256 KiB` multipart overhead rồi trả `413` nếu vượt. Cấu hình reverse proxy phải có client-body limit tương ứng để request quá lớn bị từ chối trước Uvicorn; application limiter chỉ là lớp backstop.

## Copilot SSE

```bash
curl -N -X POST http://localhost:8000/api/copilot/stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"ticker\":\"AAPL\",\"account_id\":\"$ACCOUNT_ID\",\"question\":\"Phân tích rủi ro và cho tôi một đề xuất paper\"}"
```

Client nên xử lý các event `plan`, `tool`, `tool_error`, `decision`, `token`, `done`, `error`. Chỉ `decision.execution_eligible` chưa đủ để tạo order; vẫn phải tạo proposal rồi gọi approval.

## Verification trước khi merge/deploy

Từ repository root:

```bash
python -m compileall -q backend src evaluation
python -m pytest
python -m evaluation.run_rag_eval
```

Frontend:

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm typecheck
pnpm build
```

Container manifest:

```bash
docker compose config --quiet
docker build -f backend/Dockerfile .
docker build -f frontend/Dockerfile .
```

RAG evaluator phải trả exit code `0` và `passed=true`. Dataset hiện là regression fixture nhỏ; với corpus mới cần bổ sung evaluation set đại diện, không chỉ dựa vào fixture mặc định.

## Training artifacts

Cài stack riêng:

```bash
python -m pip install -r backend/requirements-train.txt
```

Các lệnh chính:

```bash
python -m src.train_model.train_forecaster --ticker AAPL --period 5y
python -m src.train_model.train_sentiment --data training/data/financial_news.csv --mode baseline
python -m src.train_model.finetune_retriever --data training/data/retriever_pairs.jsonl
```

Trước khi mount artifact vào backend:

- kiểm tra manifest/checksum;
- ghi dataset/version/license;
- chạy walk-forward/out-of-time evaluation;
- xem xét leakage và survivorship bias;
- hoàn thiện model card;
- giữ artifact cũ để rollback.

Không train trong backend production process.

## Backup và restore

`DATA_DIR` chứa SQLite và RAG documents; đây là dữ liệu cần backup. Redis cache có thể tái tạo.

Với local instance đơn giản, dừng backend trước khi copy toàn bộ `data/` để SQLite database, WAL và RAG files ở cùng một thời điểm nhất quán. Với Compose, data nằm trong named volume `app_data`; dùng cơ chế backup volume chuẩn của môi trường sau khi dừng backend. Test restore vào một instance tách biệt trước khi coi backup là hợp lệ.

Không sửa trực tiếp rows trong `paper_orders`, `trade_proposals` hoặc `paper_positions`. Nếu cần correction workflow, hãy xây migration/audited operation thay vì chỉnh DB thủ công.

Khi nâng cấp database cũ, migration tự chuyển mọi legacy proposal còn `PENDING` nhưng thiếu `decision_id`, creation idempotency key hoặc `quote_currency` sang `EXPIRED`, ghi `decided_at` và safety note. Đây là fail-closed migration có chủ đích; không phục hồi các proposal này bằng cách sửa DB. Người dùng phải tạo decision/proposal mới theo contract hiện tại.

## Observability

Các kiểm tra cơ bản:

```bash
docker compose ps
docker compose logs --tail=200 backend
docker compose logs --tail=200 frontend
curl http://localhost:8000/api/health
```

Admin có thể đọc:

- `GET /api/ops/traces?limit=20`;
- `GET /api/ops/usage`;
- `GET /api/portfolio/agent-runs` cho run của chính user;
- `GET /api/portfolio/orders` và control events.

Dùng `X-Request-ID` trong response để nối request với backend log và agent trace.

## Troubleshooting

### Health là `degraded`

Kiểm tra quyền ghi và đường dẫn `TRADEMIND_DB_PATH`/`DATA_DIR`, dung lượng ổ đĩa và migration errors trong backend logs. Redis lỗi riêng thường chỉ khiến cache fallback; storage unavailable mới làm overall health degraded.

### Market data luôn là demo/fallback

Kiểm tra DNS/network, upstream throttling và ticker. Xem `meta.source`, `meta.fallback_reason`, `meta.as_of`, `meta.is_demo`. Không cố bỏ execution gate để “sửa”; paper approval bị chặn là đúng.

### Approval trả `409`

Đọc response detail và proposal/account state. Các nguyên nhân bình thường gồm displayed `decision_id` đã đổi, idempotency key đã bind sang decision/proposal khác, proposal không executable/hết hạn/đã xử lý, veto còn tồn tại, kill switch, trading disabled, demo/stale/delayed/halted/untradable quote, volume/instrument gate, currency mismatch, portfolio mark không đủ chất lượng, daily/risk/exposure limit, price deviation, thiếu cash hoặc thiếu position.

### Login được nhưng UI mất session

Kiểm tra browser cookie policy, host/protocol nhất quán và BFF logs. `Secure` cookies cần HTTPS khi truy cập qua hostname từ xa; local browser behavior với `localhost` không nên được dùng làm cấu hình shared deployment. Không gọi frontend bằng một host rồi API bằng host khác nếu muốn same-origin flow.

Nếu logs trả “refresh token replay detected”, coi refresh session có khả năng bị copy/reuse: mọi refresh session active của user đã bị revoke. Yêu cầu đăng nhập lại, kiểm tra thiết bị/log/request ID và rotate signing secret nếu nghi ngờ key compromise rộng hơn.

### SSE bị buffering

Reverse proxy phải tắt buffering cho `text/event-stream`. Backend/BFF đã gửi `X-Accel-Buffering: no` và `Cache-Control: no-cache/no-store`, nhưng proxy ngoài vẫn cần cấu hình tương ứng.

### RAG không trả nguồn

Xác nhận tài liệu thuộc đúng user, file đã ingest thành chunks và câu hỏi có thuật ngữ liên quan. No-answer threshold được thiết kế để tránh đưa context không liên quan. Với PDF scan, cài OCR stack và kiểm tra `used_ocr`.

## Production-like exposure checklist

Trước khi cho bất kỳ người dùng khác truy cập, tối thiểu:

- giữ `AUTH_REQUIRED=true`;
- giữ `ALLOW_PUBLIC_REGISTRATION=false` trừ khi có chủ đích vận hành self-signup;
- dùng JWT secret và bootstrap token ngẫu nhiên, độc lập, 32+ byte; bootstrap admin email chính xác;
- đặt TLS reverse proxy và giới hạn trusted hosts/origins;
- đặt reverse-proxy client-body limit cho request thường và upload, không chỉ dựa vào ASGI limiter;
- giới hạn network access tới backend/Redis;
- dùng persistent encrypted storage và backup/restore test;
- định nghĩa log retention, alerting và incident owner;
- pin/scan images và dependencies;
- chạy toàn bộ tests, RAG gate và container builds;
- giữ live trading unavailable.

Ngay cả khi hoàn tất checklist này, TradeMind vẫn chưa được xem là production-certified nếu chưa có load/security/compliance/model-quality review phù hợp.
