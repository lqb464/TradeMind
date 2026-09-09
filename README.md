# TradeMind · ML research workspace

TradeMind là project Python-first dành cho workflow ML/AI Engineer: nạp dữ liệu, kiểm tra chất lượng, tạo đặc trưng, huấn luyện, đánh giá theo thời gian và quản lý model artifacts có provenance.

Project hiện tập trung vào hai bài toán có pipeline thật:

- **Financial time-series forecasting:** dự báo log-return theo nhiều horizon bằng các quantile model; feature được tính nhân quả, validation theo thời gian và purge theo horizon.
- **Financial text sentiment:** baseline TF-IDF + Logistic Regression với manifest metric; có thể mở rộng bằng nhóm dependency NLP tùy chọn.

Không có frontend/backend service, tài khoản, paper ledger hay lệnh giao dịch trong luồng ML chính. UI Streamlit chỉ là demo để upload CSV, chạy experiment, xem metric và tải artifact.

## Kiến trúc

```text
CSV / nguồn giá rõ ràng
        ↓
validate OHLCV → causal features → direct-horizon targets
        ↓
chronological holdout + purge → quantile models → metrics
        ↓
.joblib + JSON manifest + SHA256

app.py (Streamlit demo) ───── gọi cùng pipeline Python
scripts/                  ───── CLI train/evaluate
```

## Bắt đầu

Python 3.11 trở lên:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
streamlit run app.py
```

Mở URL Streamlit được in trong terminal, thường là `http://localhost:8501`. Tải CSV có các cột `date, open, high, low, close, volume`, tối thiểu 160 dòng, và nhiều hơn nếu tăng horizon. Chọn horizon và số vòng học, rồi bấm **Train và đánh giá**. App hiển thị MAE/RMSE trên log-return, coverage của khoảng quantile, directional accuracy, purge gap và cho phép tải artifact.

## CLI tái lập được

Huấn luyện từ file local, không gọi mạng:

```powershell
python scripts/train.py --ticker AAPL --input path/to/prices.csv --max-horizon 5 --max-iter 80
```

Lấy giá từ provider được nêu rõ trong CLI:

```powershell
python scripts/train.py --ticker AAPL --period 5y --max-horizon 5
```

Chạy smoke pipeline với dữ liệu tổng hợp chỉ để kiểm tra kỹ thuật:

```powershell
python scripts/train.py --ticker SMOKE --smoke-test --max-horizon 2 --output .tmp/smoke.joblib
```

Dữ liệu tổng hợp được gắn `data_source=deterministic-smoke-test` và không được xem là kết quả nghiên cứu.

## Huấn luyện sentiment

Dataset CSV mặc định cần cột `text,label`:

```powershell
python -m src.train_model.train_sentiment --data path/to/news.csv --output training/outputs/sentiment/model.joblib
```

Để dùng các tiện ích NLP tùy chọn:

```powershell
python -m pip install -e ".[nlp]"
```

## Cấu trúc thư mục

```text
app.py                 Streamlit demo mỏng
src/data/              kiểm tra, tải và chuẩn hóa dữ liệu OHLCV
src/features/          technical features, targets và purged CV
src/models/            backtest kernel độc lập với dịch vụ web
src/train_model/       train forecast/sentiment và artifact contracts
scripts/               CLI cho experiment
data/                  vùng raw/interim/processed và hướng dẫn nguồn dữ liệu
training/outputs/       model artifacts local, không commit
tests/                  kiểm tra feature, CV và training pipeline
docs/                   sơ đồ kiến trúc và ghi chú phương pháp
```

## Model artifact và dữ liệu

Mỗi artifact joblib được lưu cùng JSON manifest và SHA256. Manifest ghi model type, dữ liệu đầu vào, cutoff, feature list, cấu hình validation, metric và checksum. Artifact chỉ nên nạp từ nguồn tin cậy; SHA256 phát hiện hỏng file nhưng không thay chữ ký số.

Không commit dữ liệu vendor, file `.env` hoặc model artifact. Ghi nguồn, license, thời điểm lấy, corporate-action policy và giới hạn point-in-time cho dataset dùng trong báo cáo.

## Kiểm tra chất lượng

```powershell
python -m compileall -q src scripts app.py
python -m pytest
```

Có thể chạy riêng `python -m pytest tests/test_features.py tests/test_purged_cv.py tests/test_training_workspace.py` cho ML core.

## Giới hạn

Đây là workspace nghiên cứu, không phải hệ thống thực thi giao dịch hay lời khuyên đầu tư. Historical validation có thể chịu regime shift, survivorship bias, corporate-action mismatch hoặc lỗi nguồn. Chỉ so sánh model trên dữ liệu có provenance và quy trình evaluation được xác định trước.
