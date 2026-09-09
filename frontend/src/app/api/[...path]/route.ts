import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const ACCESS_COOKIE = "__Host-trademind_access";
const REFRESH_COOKIE = "__Host-trademind_refresh";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

interface RouteContext {
  params: Promise<{ path: string[] }>;
}

interface TokenPayload {
  accessToken: string;
  refreshToken: string;
  expiresIn: number;
  user: unknown;
}

type NodeRequestInit = RequestInit & { duplex?: "half" };
const refreshFlights = new Map<string, Promise<TokenPayload | null>>();
const REFRESH_REPLAY_GRACE_MS = 15_000;

function apiBase(): string {
  const raw = (process.env.API_ORIGIN || "http://127.0.0.1:8000").replace(/\/+$/, "");
  const parsed = new URL(raw);
  if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
    throw new Error("API_ORIGIN must use http or https");
  }
  return raw.endsWith("/api") ? raw : `${raw}/api`;
}

function targetUrl(segments: string[], search: string): string {
  if (!segments.length || segments.some((segment) => !segment || segment === "." || segment === ".." || segment.includes("/"))) {
    throw new Error("Invalid API path");
  }
  return `${apiBase()}/${segments.map(encodeURIComponent).join("/")}${search}`;
}

function isSameOrigin(request: NextRequest): boolean {
  const fetchSite = request.headers.get("sec-fetch-site")?.trim().toLowerCase();
  if (fetchSite && !["same-origin", "none"].includes(fetchSite)) return false;
  if (fetchSite === "same-origin") return true;

  const origin = request.headers.get("origin");
  if (!origin) return true;
  try {
    const host = request.headers.get("host");
    if (!host) return false;
    const expected = new URL(`${request.nextUrl.protocol}//${host}`).origin;
    return new URL(origin).origin === expected;
  } catch {
    return false;
  }
}

function upstreamHeaders(request: NextRequest, accessToken?: string): Headers {
  const headers = new Headers(request.headers);
  for (const name of [
    "authorization",
    "cookie",
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "accept-encoding",
    "x-forwarded-host",
    "x-forwarded-proto",
  ]) headers.delete(name);
  if (accessToken) headers.set("authorization", `Bearer ${accessToken}`);
  headers.set("x-trademind-bff", "1");
  return headers;
}

function requestInit(request: NextRequest, headers: Headers, body: BodyInit | null): NodeRequestInit {
  const init: NodeRequestInit = {
    method: request.method,
    headers,
    body: ["GET", "HEAD"].includes(request.method) ? undefined : body,
    cache: "no-store",
    redirect: "manual",
    signal: request.signal,
  };
  if (init.body) init.duplex = "half";
  return init;
}

function copyResponseHeaders(source: Headers): Headers {
  const output = new Headers();
  for (const name of [
    "content-type",
    "content-disposition",
    "cache-control",
    "etag",
    "last-modified",
    "www-authenticate",
    "x-request-id",
  ]) {
    const value = source.get(name);
    if (value) output.set(name, value);
  }
  output.set("cache-control", "no-store, max-age=0");
  if ((source.get("content-type") || "").includes("text/event-stream")) {
    output.set("content-type", "text/event-stream; charset=utf-8");
    output.set("connection", "keep-alive");
    output.set("x-accel-buffering", "no");
  }
  return output;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function extractTokens(value: unknown): TokenPayload | null {
  const root = asRecord(value);
  const tokenRoot = asRecord(root.tokens ?? root.token ?? root);
  const accessToken = tokenRoot.access_token ?? tokenRoot.accessToken;
  const refreshToken = tokenRoot.refresh_token ?? tokenRoot.refreshToken;
  if (typeof accessToken !== "string" || typeof refreshToken !== "string") return null;
  const rawExpiry = tokenRoot.expires_in ?? tokenRoot.expiresIn;
  const expiresIn = typeof rawExpiry === "number" && Number.isFinite(rawExpiry) ? rawExpiry : 15 * 60;
  return {
    accessToken,
    refreshToken,
    expiresIn: Math.max(60, Math.floor(expiresIn)),
    user: root.user ?? tokenRoot.user ?? null,
  };
}

const cookieOptions = {
  httpOnly: true,
  secure: true,
  sameSite: "strict" as const,
  path: "/",
  priority: "high" as const,
};

function setTokenCookies(response: NextResponse, tokens: TokenPayload): void {
  response.cookies.set(ACCESS_COOKIE, tokens.accessToken, { ...cookieOptions, maxAge: tokens.expiresIn });
  response.cookies.set(REFRESH_COOKIE, tokens.refreshToken, { ...cookieOptions, maxAge: 30 * 24 * 60 * 60 });
}

function clearTokenCookies(response: NextResponse): void {
  response.cookies.set(ACCESS_COOKIE, "", { ...cookieOptions, maxAge: 0 });
  response.cookies.set(REFRESH_COOKIE, "", { ...cookieOptions, maxAge: 0 });
}

function jsonError(status: number, detail: string): NextResponse {
  return NextResponse.json({ detail }, { status, headers: { "cache-control": "no-store" } });
}

async function requestFreshSession(refreshToken: string): Promise<TokenPayload | null> {
  try {
    const response = await fetch(`${apiBase()}/auth/refresh`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json", "x-trademind-bff": "1" },
      body: JSON.stringify({ refresh_token: refreshToken }),
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) return null;
    return extractTokens(await response.json());
  } catch {
    return null;
  }
}

function refreshSession(refreshToken: string): Promise<TokenPayload | null> {
  const active = refreshFlights.get(refreshToken);
  if (active) return active;

  const flight = requestFreshSession(refreshToken);
  refreshFlights.set(refreshToken, flight);
  void flight.then((tokens) => {
    setTimeout(() => {
      if (refreshFlights.get(refreshToken) === flight) refreshFlights.delete(refreshToken);
    }, tokens ? REFRESH_REPLAY_GRACE_MS : 0);
  });
  return flight;
}

async function authResponse(upstream: Response): Promise<NextResponse> {
  const payload: unknown = await upstream.json().catch(() => null);
  if (!upstream.ok) {
    return NextResponse.json(payload ?? { detail: "Authentication failed" }, {
      status: upstream.status,
      headers: copyResponseHeaders(upstream.headers),
    });
  }
  const tokens = extractTokens(payload);
  if (!tokens) return jsonError(502, "Authentication provider returned an invalid token response");
  const response = NextResponse.json(
    { authenticated: true, expires_in: tokens.expiresIn, user: tokens.user },
    { status: upstream.status, headers: { "cache-control": "no-store" } },
  );
  setTokenCookies(response, tokens);
  return response;
}

async function handler(request: NextRequest, context: RouteContext): Promise<NextResponse> {
  if (MUTATING_METHODS.has(request.method) && !isSameOrigin(request)) {
    return jsonError(403, "Cross-origin state change rejected");
  }

  const { path } = await context.params;
  let url: string;
  try {
    url = targetUrl(path, request.nextUrl.search);
  } catch {
    return jsonError(400, "Invalid API route");
  }

  const route = path.join("/");
  const isLogin = route === "auth/login";
  const isRegister = route === "auth/register";
  const isRefresh = route === "auth/refresh";
  const isLogout = route === "auth/logout";
  const cookieStore = await cookies();
  let accessToken = cookieStore.get(ACCESS_COOKIE)?.value;
  const refreshToken = cookieStore.get(REFRESH_COOKIE)?.value;

  if (isLogout && !refreshToken) {
    const response = new NextResponse(null, { status: 204 });
    clearTokenCookies(response);
    return response;
  }

  const replay = !["GET", "HEAD"].includes(request.method) ? request.clone() : null;
  let firstBody: BodyInit | null = ["GET", "HEAD"].includes(request.method) ? null : request.body;
  let firstHeaders = upstreamHeaders(request, accessToken);

  if (isRefresh || isLogout) {
    firstBody = JSON.stringify({ refresh_token: refreshToken || "" });
    firstHeaders.set("content-type", "application/json");
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, requestInit(request, firstHeaders, firstBody));
  } catch {
    return jsonError(502, "TradeMind API is unavailable");
  }

  if (isLogin || isRegister || isRefresh) return authResponse(upstream);

  let rotatedTokens: TokenPayload | null = null;
  if (upstream.status === 401 && refreshToken && !isLogout) {
    rotatedTokens = await refreshSession(refreshToken);
    if (rotatedTokens) {
      accessToken = rotatedTokens.accessToken;
      const retryHeaders = upstreamHeaders(request, accessToken);
      const retryBody = replay ? replay.body : null;
      try {
        upstream = await fetch(url, requestInit(request, retryHeaders, retryBody));
      } catch {
        const response = jsonError(502, "TradeMind API is unavailable after session refresh");
        setTokenCookies(response, rotatedTokens);
        return response;
      }
    }
  }

  if (isLogout) {
    const response = new NextResponse(null, { status: upstream.ok ? 204 : upstream.status });
    clearTokenCookies(response);
    return response;
  }

  const response = new NextResponse(upstream.status === 204 ? null : upstream.body, {
    status: upstream.status,
    headers: copyResponseHeaders(upstream.headers),
  });
  if (rotatedTokens) setTokenCookies(response, rotatedTokens);
  if (upstream.status === 401 && !rotatedTokens) clearTokenCookies(response);
  return response;
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
export const HEAD = handler;
export const OPTIONS = handler;
