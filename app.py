from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.loader import validate_ohlcv
from src.train_model.train_forecaster import train_forecaster

st.set_page_config(page_title="TradeMind ML Lab", page_icon="📈", layout="wide")
st.title("TradeMind · ML Lab")
st.caption("Nghiên cứu mô hình: dữ liệu → đặc trưng → huấn luyện → đánh giá")

with st.sidebar:
    st.header("Thiết lập experiment")
    uploaded = st.file_uploader("Tải CSV OHLCV", type=["csv"])
    st.caption("Cột bắt buộc: date, open, high, low, close, volume")
    ticker = st.text_input("Tên mã / dataset", "DEMO").strip().upper() or "DEMO"
    horizon = st.slider("Horizon tối đa (ngày)", 1, 10, 3)
    iterations = st.slider("Số vòng học mỗi model", 10, 180, 40, step=10)
    run_training = st.button("Train và đánh giá", type="primary", disabled=uploaded is None)
    st.divider()
    st.markdown("**Thiết kế an toàn**\n\n- Chỉ dùng CSV bạn tải lên.\n- Không sinh lệnh giao dịch.\n- Chia validation theo thời gian và purge theo horizon.\n- Artifact có manifest và SHA256.")

prices = None
if uploaded is None:
    st.info("Tải CSV OHLCV để bắt đầu. CLI cũng hỗ trợ nguồn giá rõ ràng hoặc file CSV local.")
else:
    try:
        prices = validate_ohlcv(pd.read_csv(BytesIO(uploaded.getvalue())), min_rows=160)
    except Exception as exc:
        st.error(f"CSV OHLCV không hợp lệ: {exc}")
        st.stop()

    left, middle, right = st.columns(3)
    left.metric("Số phiên", f"{len(prices):,}")
    middle.metric("Từ ngày", prices.date.iloc[0].strftime("%Y-%m-%d"))
    right.metric("Đến ngày", prices.date.iloc[-1].strftime("%Y-%m-%d"))
    st.line_chart(prices.set_index("date")["close"], y_label="Giá đóng cửa")
    with st.expander("Xem dữ liệu đã chuẩn hóa"):
        st.dataframe(prices.tail(10), use_container_width=True, hide_index=True)

    if run_training:
        with tempfile.TemporaryDirectory(prefix="trademind-ml-") as temp_dir:
            artifact_path = Path(temp_dir) / "forecast.joblib"
            try:
                with st.spinner("Đang tạo đặc trưng nhân quả và huấn luyện với chronological holdout có purge..."):
                    result = train_forecaster(
                        prices,
                        ticker=ticker,
                        output=artifact_path,
                        max_horizon=horizon,
                        max_iter=iterations,
                        data_source=f"uploaded-csv:{Path(uploaded.name).name}",
                    )
                st.session_state["last_manifest"] = result["manifest"]
                bundle = BytesIO()
                with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.write(artifact_path, "forecast.joblib")
                    archive.write(artifact_path.with_suffix(".joblib.manifest.json"), "forecast.joblib.manifest.json")
                    archive.write(artifact_path.with_suffix(".joblib.sha256"), "forecast.joblib.sha256")
                st.session_state["last_artifact"] = bundle.getvalue()
                st.session_state["last_name"] = f"{ticker}-forecast.joblib"
                st.success("Huấn luyện hoàn tất trên dữ liệu đã tải lên.")
            except Exception as exc:
                st.error(f"Không thể huấn luyện: {exc}")

if "last_manifest" in st.session_state:
    manifest = st.session_state["last_manifest"]
    st.subheader("Kết quả validation")
    st.caption(f"Model: {manifest['model_type']} · Dòng dữ liệu: {manifest['data_rows']:,} · SHA256 dữ liệu: {manifest['data_sha256'][:16]}…")
    rows = []
    for day, values in manifest["metrics"].items():
        rows.append({"Horizon (ngày)": int(day), "MAE log-return": values["mae_log_return"], "RMSE log-return": values["rmse_log_return"], "Coverage 10–90%": values["interval_coverage"], "Directional accuracy": values["directional_accuracy"], "Train rows": values["train_rows"], "Validation rows": values["validation_rows"], "Purge gap": values["purge_gap"]})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.download_button("Tải model artifact", st.session_state["last_artifact"], file_name=st.session_state["last_name"].replace(".joblib", ".zip"), mime="application/zip")
    with st.expander("Manifest đầy đủ"):
        st.json(manifest)

st.divider()
st.caption("Công cụ nghiên cứu ML. Validation lịch sử không đảm bảo hiệu suất tương lai và không phải khuyến nghị đầu tư.")
