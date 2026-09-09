"use client";

import { useState } from "react";
import { apiJson, ApiError, toErrorMessage } from "@/lib/api";
import { normalizeUser } from "@/lib/normalize";
import type { HealthStatus, UserProfile } from "@/lib/types";
import { Icon } from "./icon";

interface AuthScreenProps {
  health: HealthStatus | null;
  healthError?: string;
  onAuthenticated: (user: UserProfile) => void;
}

export function AuthScreen({ health, healthError, onAuthenticated }: AuthScreenProps) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (mode === "register" && password !== confirmPassword) {
      setError("Mật khẩu xác nhận chưa khớp.");
      return;
    }
    if (password.length < 10) {
      setError("Mật khẩu cần ít nhất 10 ký tự.");
      return;
    }
    setBusy(true);
    try {
      const payload: Record<string, string> = { email: email.trim(), password };
      if (mode === "register") payload.name = name.trim();
      const result = await apiJson<unknown>(`/api/auth/${mode}`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      try {
        const me = await apiJson<unknown>("/api/auth/me");
        onAuthenticated(normalizeUser(me, email.trim()));
      } catch (meError) {
        if (mode === "register" && meError instanceof ApiError && meError.status === 401) {
          setMode("login");
          setPassword("");
          setConfirmPassword("");
          setNotice("Tài khoản đã được tạo. Đăng nhập để tiếp tục.");
          return;
        }
        onAuthenticated(normalizeUser(result, email.trim()));
      }
    } catch (authError) {
      setError(toErrorMessage(authError));
    } finally {
      setBusy(false);
    }
  }

  function switchMode(next: "login" | "register") {
    setMode(next);
    setError("");
    setNotice("");
    setPassword("");
    setConfirmPassword("");
  }

  return (
    <main className="auth-shell">
      <section className="auth-story">
        <div className="auth-brand"><span className="brand-mark"><Icon name="trend" /></span>TradeMind</div>
        <div className="story-copy">
          <span className="eyebrow"><Icon name="shield" /> Paper-first trading intelligence</span>
          <h1>Ra quyết định có bằng chứng. Giao dịch có hàng rào.</h1>
          <p>
            TradeMind kết hợp dữ liệu thị trường, mô hình định lượng và tác tử AI trong một quy trình
            mà con người luôn là người phê duyệt cuối cùng.
          </p>
          <div className="trust-grid">
            <article><Icon name="brain" /><b>Decision intelligence</b><span>Điểm số, độ tin cậy và đóng góp từng tín hiệu.</span></article>
            <article><Icon name="shield" /><b>Risk before action</b><span>Veto, position sizing và giới hạn vốn tách khỏi LLM.</span></article>
            <article><Icon name="book" /><b>Evidence attached</b><span>Provenance thị trường và citation tài liệu hiển thị rõ.</span></article>
          </div>
        </div>
        <div className={`auth-health ${health?.ok ? "ok" : "warn"}`}>
          <span className="status-dot" />
          <div>
            <b>{health?.label ?? "Đang kiểm tra hệ thống"}</b>
            <small>{healthError || health?.mode || "Kết nối qua cổng bảo mật cùng origin"}</small>
          </div>
        </div>
      </section>

      <section className="auth-panel">
        <div className="auth-card">
          <div className="mobile-brand"><span className="brand-mark"><Icon name="trend" /></span>TradeMind</div>
          <div className="auth-heading">
            <span>{mode === "login" ? "Chào mừng trở lại" : "Khởi tạo tài khoản"}</span>
            <h2>{mode === "login" ? "Đăng nhập workspace" : "Bắt đầu với paper trading"}</h2>
            <p>{mode === "login" ? "Phiên đăng nhập được giữ trong cookie HttpOnly." : "Live execution không được bật trong hành trình này."}</p>
          </div>
          <div className="auth-tabs" role="tablist" aria-label="Chọn chế độ xác thực">
            <button type="button" role="tab" aria-selected={mode === "login"} className={mode === "login" ? "active" : ""} onClick={() => switchMode("login")}>Đăng nhập</button>
            <button type="button" role="tab" aria-selected={mode === "register"} className={mode === "register" ? "active" : ""} onClick={() => switchMode("register")}>Đăng ký</button>
          </div>
          <form className="auth-form" onSubmit={submit}>
            {mode === "register" && (
              <label>Họ và tên<input autoComplete="name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Nguyễn Minh Anh" required /></label>
            )}
            <label>Email<input type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" required /></label>
            <label>Mật khẩu<input type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Tối thiểu 10 ký tự" minLength={10} required /></label>
            {mode === "register" && (
              <label>Xác nhận mật khẩu<input type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} placeholder="Nhập lại mật khẩu" minLength={10} required /></label>
            )}
            {notice && <div className="form-notice success"><Icon name="check" />{notice}</div>}
            {error && <div className="form-notice error" role="alert"><Icon name="warning" />{error}</div>}
            <button className="primary-button auth-submit" type="submit" disabled={busy}>
              {busy ? <span className="spinner" /> : <Icon name="lock" />}
              {busy ? "Đang xác thực…" : mode === "login" ? "Vào TradeMind" : "Tạo tài khoản"}
            </button>
          </form>
          <p className="auth-footnote"><Icon name="shield" />Access token không được đưa vào JavaScript phía trình duyệt.</p>
        </div>
      </section>
    </main>
  );
}
