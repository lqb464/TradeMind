export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, message: string, payload?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

function errorMessage(payload: unknown, fallback: string): string {
  if (typeof payload === "string" && payload.trim()) return payload;
  if (payload && typeof payload === "object") {
    const value = payload as Record<string, unknown>;
    for (const key of ["detail", "message", "error", "reason"]) {
      if (typeof value[key] === "string" && value[key]) return value[key];
    }
  }
  return fallback;
}

export async function apiJson<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  const response = await fetch(path, {
    ...init,
    headers,
    cache: "no-store",
    credentials: "same-origin",
  });
  const contentType = response.headers.get("content-type") ?? "";
  let payload: unknown;
  if (contentType.includes("application/json")) {
    payload = await response.json().catch(() => null);
  } else {
    payload = await response.text().catch(() => "");
  }
  if (!response.ok) {
    throw new ApiError(
      response.status,
      errorMessage(payload, `Yêu cầu thất bại (${response.status})`),
      payload,
    );
  }
  return payload as T;
}

export interface SseEvent {
  type: string;
  data: unknown;
}

export async function streamSse(
  path: string,
  body: Record<string, unknown>,
  signal: AbortSignal,
  onEvent: (event: SseEvent) => void,
): Promise<void> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify(body),
    cache: "no-store",
    credentials: "same-origin",
    signal,
  });
  if (!response.ok || !response.body) {
    const payload = await response.text().catch(() => "");
    throw new ApiError(
      response.status,
      errorMessage(payload, `Không thể mở luồng dữ liệu (${response.status})`),
      payload,
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventType = "message";
  let dataLines: string[] = [];

  const flush = () => {
    if (!dataLines.length) return;
    const raw = dataLines.join("\n");
    let data: unknown = raw;
    try {
      data = JSON.parse(raw);
    } catch {
      // Plain-text SSE payloads are valid and are intentionally preserved.
    }
    onEvent({ type: eventType, data });
    eventType = "message";
    dataLines = [];
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line) {
        flush();
      } else if (line.startsWith("event:")) {
        eventType = line.slice(6).trim() || "message";
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (done) break;
  }
  if (buffer.startsWith("data:")) dataLines.push(buffer.slice(5).trimStart());
  flush();
}

export function toErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Đã xảy ra lỗi không xác định.";
}
