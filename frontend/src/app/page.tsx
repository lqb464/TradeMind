"use client";

import { useEffect, useState } from "react";
import { AuthScreen } from "@/components/auth-screen";
import { Icon } from "@/components/icon";
import { Workstation } from "@/components/workstation";
import { apiJson, ApiError, toErrorMessage } from "@/lib/api";
import { normalizeHealth, normalizeUser } from "@/lib/normalize";
import type { HealthStatus, UserProfile } from "@/lib/types";

type SessionState = "checking" | "anonymous" | "authenticated";

export default function Home() {
  const [session, setSession] = useState<SessionState>("checking");
  const [user, setUser] = useState<UserProfile | null>(null);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState("");

  useEffect(() => {
    let active = true;
    async function bootstrap() {
      const [healthResult, userResult] = await Promise.allSettled([
        apiJson<unknown>("/api/health"),
        apiJson<unknown>("/api/auth/me"),
      ]);
      if (!active) return;
      if (healthResult.status === "fulfilled") setHealth(normalizeHealth(healthResult.value));
      else setHealthError(toErrorMessage(healthResult.reason));

      if (userResult.status === "fulfilled") {
        setUser(normalizeUser(userResult.value));
        setSession("authenticated");
      } else {
        if (!(userResult.reason instanceof ApiError && userResult.reason.status === 401)) {
          setHealthError((current) => [current, `Auth: ${toErrorMessage(userResult.reason)}`].filter(Boolean).join(" · "));
        }
        setSession("anonymous");
      }
    }
    void bootstrap();
    return () => { active = false; };
  }, []);

  async function logout() {
    try {
      await apiJson<unknown>("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
    } catch {
      // The BFF clears local HttpOnly cookies even if backend revocation is unavailable.
    } finally {
      setUser(null);
      setSession("anonymous");
    }
  }

  if (session === "checking") {
    return (
      <main className="boot-screen">
        <div className="boot-brand"><span className="brand-mark"><Icon name="trend" /></span><b>TradeMind</b></div>
        <div className="boot-progress"><i /><span>Đang xác minh phiên và kết nối hệ thống…</span></div>
      </main>
    );
  }

  if (session === "anonymous" || !user) {
    return <AuthScreen health={health} healthError={healthError} onAuthenticated={(profile) => { setUser(profile); setSession("authenticated"); }} />;
  }

  return <Workstation user={user} initialHealth={health} onLogout={logout} />;
}
