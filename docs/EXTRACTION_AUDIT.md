# Capability extraction audit — TradeMind v3

Tài liệu này ghi lại cách TradeMind kế thừa các ý tưởng có giá trị từ workspace thử nghiệm cũ, đồng thời loại bỏ phần không thuộc trading domain. Đây là provenance về thiết kế, không phải tuyên bố rằng code của các project nguồn được nhập nguyên trạng.

| Project nguồn | Ý tưởng được giữ và hiện thực hóa trong TradeMind | Phần chủ động không mang sang |
|---|---|---|
| AgenThink | SSE agent workflow, typed allow-listed tools, provider abstraction, request trace, rate limit và cache fallback | Chat/tool tổng quát, weather/search, memory đa mục đích và multi-agent demo |
| RAnythinG | Query rewrite, hybrid sparse retrieval, RRF, adjacent-chunk context, citations, tenant-scoped documents và no-answer behavior | Pháp luật prompts/schema, lawyer ticketing, Neo4j/Celery/PostgreSQL deployment |
| dOCRead | PDF text extraction, optional OCR abstraction, parser/`used_ocr` provenance | OCR model training và bundled model weights |
| catbus-ai | Token usage ledger, explicit pricing configuration và JSON-safe provider handling | Canvas LMS, course/module domain schema |
| SketClothes | Cloud-provider → deterministic offline fallback với event contract ổn định | Image generation, ControlNet/GAN, GPU/fashion pipeline |

## Capability được xây thêm ở v3

TradeMind v3 không dừng ở research dashboard. Các boundary sau đã được bổ sung trực tiếp cho trading domain:

- Authentication bằng scrypt password, signed access/refresh JWT, refresh rotation và revocation.
- User-scoped watchlist, paper account, position, proposal, order, agent run và document.
- Data-quality contract có provenance, freshness, completeness, demo/fallback detection và `execution_eligible`.
- Deterministic explainable ensemble với risk veto và position sizing theo account context.
- Proposal tách rời approval; approval idempotent, re-fetch quote và chỉ ghi `PAPER` fill.
- Trading enable/disable, kill switch và control-event history.
- Long-only backtest chống look-ahead cơ bản với transaction cost/slippage.
- RAG retrieval regression gate có negative control.
- Next.js BFF giữ token trong HttpOnly cookies và proxy REST/SSE cùng origin.

## Vị trí implementation

| Boundary | Implementation chính |
|---|---|
| API và HTTP controls | `backend/main.py`, `backend/api/` |
| Auth/security | `backend/core/security.py` |
| Persistence và paper ledger | `backend/src/store.py` |
| Market provenance | `backend/src/intelligence.py`, `backend/src/market_quality.py` |
| Decision/risk/backtest | `backend/src/trading.py` |
| Agent/provider | `backend/src/agent.py`, `backend/src/providers.py` |
| RAG/OCR | `backend/src/rag.py`, `backend/src/ocr.py` |
| Observability/cache | `backend/src/observability.py`, `backend/src/cache.py` |
| UI/BFF | `frontend/src/` |
| Evaluation/training | `evaluation/`, `src/train_model/`, `training/` |

## Quyết định không ghép máy móc

TradeMind không đưa toàn bộ kiến trúc enterprise của các project khác vào chỉ để tăng số lượng thành phần. Redis là cache tùy chọn chứ không phải source of truth; SQLite phù hợp với prototype local-first hiện tại; ingestion chạy đồng bộ ngoài event loop thay vì kéo distributed queue vào khi chưa có tải cần thiết. Dense retriever và LLM là enhancement tùy chọn, không phải dependency để demo chạy.

Các phần chưa được xây gồm live broker/exchange connector, PostgreSQL multi-node, distributed workers, real-time licensed market feed, external immutable audit log và compliance workflow. Việc thiếu những phần này được ghi công khai thay vì mô tả TradeMind là production-ready.
