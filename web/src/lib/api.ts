const BASE = "";

// Ephemeral session token for protected endpoints (reveal).
// Fetched once on first reveal request and cached in memory.
let _sessionToken: string | null = null;

// Telegram Mini App auth — inject initData header for all requests
// when running inside a Telegram WebApp context.
function _tgHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  try {
    const tg = (window as any).Telegram?.WebApp;
    if (tg?.initData) {
      headers["X-Telegram-Init-Data"] = tg.initData;
    }
  } catch {}
  return headers;
}

function _mergeHeaders(init?: RequestInit): RequestInit {
  const tg = _tgHeaders();
  if (!Object.keys(tg).length) return init ?? {};
  const existing = (init?.headers as Record<string, string>) ?? {};
  return { ...init, headers: { ...tg, ...existing } };
}

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, _mergeHeaders(init));
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

async function getSessionToken(): Promise<string> {
  if (_sessionToken) return _sessionToken;
  const resp = await fetchJSON<{ token: string }>("/api/auth/session-token");
  _sessionToken = resp.token;
  return _sessionToken;
}

// Authenticated fetch helper — attaches session token to all write operations.
async function _authHeaders(extra?: Record<string, string>): Promise<Record<string, string>> {
  const token = await getSessionToken();
  return { Authorization: `Bearer ${token}`, ...extra };
}

export const api = {
  getStatus: () => fetchJSON<StatusResponse>("/api/status"),
  getSessions: (limit = 20, offset = 0) =>
    fetchJSON<PaginatedSessions>(`/api/sessions?limit=${limit}&offset=${offset}`),
  getSessionMessages: (id: string) =>
    fetchJSON<SessionMessagesResponse>(`/api/sessions/${encodeURIComponent(id)}/messages`),
  deleteSession: async (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: await _authHeaders(),
    }),
  getLogs: (params: { file?: string; lines?: number; level?: string; component?: string }) => {
    const qs = new URLSearchParams();
    if (params.file) qs.set("file", params.file);
    if (params.lines) qs.set("lines", String(params.lines));
    if (params.level && params.level !== "ALL") qs.set("level", params.level);
    if (params.component && params.component !== "all") qs.set("component", params.component);
    return fetchJSON<LogsResponse>(`/api/logs?${qs.toString()}`);
  },
  getAnalytics: (days: number) =>
    fetchJSON<AnalyticsResponse>(`/api/analytics/usage?days=${days}`),
  getConfig: () => fetchJSON<Record<string, unknown>>("/api/config"),
  getDefaults: () => fetchJSON<Record<string, unknown>>("/api/config/defaults"),
  getSchema: () => fetchJSON<{ fields: Record<string, unknown>; category_order: string[] }>("/api/config/schema"),
  saveConfig: async (config: Record<string, unknown>) =>
    fetchJSON<{ ok: boolean }>("/api/config", {
      method: "PUT",
      headers: await _authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ config }),
    }),
  getConfigRaw: () => fetchJSON<{ yaml: string }>("/api/config/raw"),
  saveConfigRaw: async (yaml_text: string) =>
    fetchJSON<{ ok: boolean }>("/api/config/raw", {
      method: "PUT",
      headers: await _authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ yaml_text }),
    }),
  getEnvVars: () => fetchJSON<Record<string, EnvVarInfo>>("/api/env"),
  setEnvVar: async (key: string, value: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "PUT",
      headers: await _authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ key, value }),
    }),
  deleteEnvVar: async (key: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "DELETE",
      headers: await _authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ key }),
    }),
  revealEnvVar: async (key: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ key: string; value: string }>("/api/env/reveal", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ key }),
    });
  },

  // Cron jobs
  getCronJobs: () => fetchJSON<CronJob[]>("/api/cron/jobs"),
  createCronJob: async (job: { prompt: string; schedule: string; name?: string; deliver?: string }) =>
    fetchJSON<CronJob>("/api/cron/jobs", {
      method: "POST",
      headers: await _authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(job),
    }),
  pauseCronJob: async (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}/pause`, { method: "POST", headers: await _authHeaders() }),
  resumeCronJob: async (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}/resume`, { method: "POST", headers: await _authHeaders() }),
  triggerCronJob: async (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}/trigger`, { method: "POST", headers: await _authHeaders() }),
  deleteCronJob: async (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${id}`, { method: "DELETE", headers: await _authHeaders() }),

  // Skills & Toolsets
  getSkills: () => fetchJSON<SkillInfo[]>("/api/skills"),
  toggleSkill: async (name: string, enabled: boolean) =>
    fetchJSON<{ ok: boolean }>("/api/skills/toggle", {
      method: "PUT",
      headers: await _authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ name, enabled }),
    }),
  getToolsets: () => fetchJSON<ToolsetInfo[]>("/api/tools/toolsets"),

  // Session search (FTS5)
  searchSessions: (q: string) =>
    fetchJSON<SessionSearchResponse>(`/api/sessions/search?q=${encodeURIComponent(q)}`),

  // OAuth provider management
  getOAuthProviders: () =>
    fetchJSON<OAuthProvidersResponse>("/api/providers/oauth"),
  disconnectOAuthProvider: async (providerId: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ ok: boolean; provider: string }>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}`,
      {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      },
    );
  },
  startOAuthLogin: async (providerId: string) => {
    const token = await getSessionToken();
    return fetchJSON<OAuthStartResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/start`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: "{}",
      },
    );
  },
  submitOAuthCode: async (providerId: string, sessionId: string, code: string) => {
    const token = await getSessionToken();
    return fetchJSON<OAuthSubmitResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/submit`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ session_id: sessionId, code }),
      },
    );
  },
  pollOAuthSession: (providerId: string, sessionId: string) =>
    fetchJSON<OAuthPollResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/poll/${encodeURIComponent(sessionId)}`,
    ),
  cancelOAuthSession: async (sessionId: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ ok: boolean }>(
      `/api/providers/oauth/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      },
    );
  },

  // Chat (streaming handled by ChatPage directly via fetch)
  getModelInfo: () => fetchJSON<ModelInfo>("/api/model-info"),
  executeCommand: async (body: { command: string; args: string }) =>
    fetchJSON<{ output: string; command: string; session_id: string }>("/api/command", {
      method: "POST",
      headers: await _authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    }),

  // System health
  getSystemHealth: () => fetchJSON<SystemHealth>("/api/system-health"),

  // Agents
  getAgents: () => fetchJSON<{ agents: AgentInfo[] }>("/api/agents"),
  getAgent: (name: string) => fetchJSON<AgentInfo>(`/api/agents/${encodeURIComponent(name)}`),
  spawnAgent: async (body: { prompt: string; mode: string; name?: string; worktree?: boolean; model?: string }) =>
    fetchJSON<AgentInfo>("/api/agents", {
      method: "POST",
      headers: await _authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    }),
  killAgent: async (name: string) =>
    fetchJSON<{ status: string; name: string }>(`/api/agents/${encodeURIComponent(name)}`, { method: "DELETE", headers: await _authHeaders() }),
  sendAgentMessage: async (name: string, message: string) =>
    fetchJSON<{ status: string; name: string }>(`/api/agents/${encodeURIComponent(name)}/message`, {
      method: "POST",
      headers: await _authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ message }),
    }),

  // Processes
  getProcesses: () => fetchJSON<{ processes: ProcessInfo[] }>("/api/processes"),

  // Streaming chat via gateway /v1/chat/completions
  streamChat: async function* (
    messages: { role: string; content: string }[],
    opts?: { sessionId?: string; onSessionId?: (id: string) => void },
  ): AsyncGenerator<string> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };

    // Auth: try session token first, fall back to Telegram initData
    try {
      const token = await getSessionToken();
      headers["Authorization"] = `Bearer ${token}`;
    } catch {}
    const tg = _tgHeaders();
    Object.assign(headers, tg);

    if (opts?.sessionId) {
      headers["X-Hermes-Session-Id"] = opts.sessionId;
    }

    const res = await fetch("/v1/chat/completions", {
      method: "POST",
      headers,
      body: JSON.stringify({
        model: "hermes-agent",
        messages,
        stream: true,
      }),
    });

    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status}: ${text}`);
    }

    const reader = res.body?.getReader();
    if (!reader) throw new Error("No response body");

    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      const lines = buf.split("\n");
      buf = lines.pop() || "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        // Custom session event from proxy
        if (trimmed.startsWith("event: session")) continue;
        if (trimmed.startsWith("data: ") && !trimmed.startsWith("data: {")) {
          const payload = trimmed.slice(6);
          if (payload === "[DONE]") return;
          // Non-JSON data line — check if it's a session ID
          if (payload.length > 10 && !payload.startsWith("{")) {
            if (opts?.onSessionId) opts.onSessionId(payload);
            continue;
          }
        }

        if (!trimmed.startsWith("data: ")) continue;
        const payload = trimmed.slice(6);
        if (payload === "[DONE]") return;
        try {
          const chunk = JSON.parse(payload);
          const delta = chunk.choices?.[0]?.delta?.content;
          if (delta) yield delta;
        } catch {}
      }
    }
  },
};

export interface PlatformStatus {
  error_code?: string;
  error_message?: string;
  state: string;
  updated_at: string;
}

export interface StatusResponse {
  active_sessions: number;
  config_path: string;
  config_version: number;
  env_path: string;
  gateway_exit_reason: string | null;
  gateway_pid: number | null;
  gateway_platforms: Record<string, PlatformStatus>;
  gateway_running: boolean;
  gateway_state: string | null;
  gateway_updated_at: string | null;
  hermes_home: string;
  latest_config_version: number;
  release_date: string;
  version: string;
}

export interface SessionInfo {
  id: string;
  source: string | null;
  model: string | null;
  title: string | null;
  started_at: number;
  ended_at: number | null;
  last_active: number;
  is_active: boolean;
  message_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  preview: string | null;
}

export interface PaginatedSessions {
  sessions: SessionInfo[];
  total: number;
  limit: number;
  offset: number;
}

export interface EnvVarInfo {
  is_set: boolean;
  redacted_value: string | null;
  description: string;
  url: string | null;
  category: string;
  is_password: boolean;
  tools: string[];
  advanced: boolean;
}

export interface SessionMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  tool_calls?: Array<{
    id: string;
    function: { name: string; arguments: string };
  }>;
  tool_name?: string;
  tool_call_id?: string;
  timestamp?: number;
}

export interface SessionMessagesResponse {
  session_id: string;
  messages: SessionMessage[];
}

export interface LogsResponse {
  file: string;
  lines: string[];
}

export interface AnalyticsDailyEntry {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
}

export interface AnalyticsModelEntry {
  model: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number;
  sessions: number;
}

export interface AnalyticsResponse {
  daily: AnalyticsDailyEntry[];
  by_model: AnalyticsModelEntry[];
  totals: {
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
  };
}

export interface CronJob {
  id: string;
  name?: string;
  prompt: string;
  schedule: { kind: string; expr: string; display: string };
  schedule_display: string;
  enabled: boolean;
  state: string;
  deliver?: string;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_error?: string | null;
}

export interface SkillInfo {
  name: string;
  description: string;
  category: string;
  enabled: boolean;
}

export interface ToolsetInfo {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
  configured: boolean;
  tools: string[];
}

export interface SessionSearchResult {
  session_id: string;
  snippet: string;
  role: string | null;
  source: string | null;
  model: string | null;
  session_started: number | null;
}

export interface SessionSearchResponse {
  results: SessionSearchResult[];
}

// ── OAuth provider types ────────────────────────────────────────────────

export interface OAuthProviderStatus {
  logged_in: boolean;
  source?: string | null;
  source_label?: string | null;
  token_preview?: string | null;
  expires_at?: string | null;
  has_refresh_token?: boolean;
  last_refresh?: string | null;
  error?: string;
}

export interface OAuthProvider {
  id: string;
  name: string;
  /** "pkce" (browser redirect + paste code), "device_code" (show code + URL),
   *  or "external" (delegated to a separate CLI like Claude Code or Qwen). */
  flow: "pkce" | "device_code" | "external";
  cli_command: string;
  docs_url: string;
  status: OAuthProviderStatus;
}

export interface OAuthProvidersResponse {
  providers: OAuthProvider[];
}

/** Discriminated union — the shape of /start depends on the flow. */
export type OAuthStartResponse =
  | {
      session_id: string;
      flow: "pkce";
      auth_url: string;
      expires_in: number;
    }
  | {
      session_id: string;
      flow: "device_code";
      user_code: string;
      verification_url: string;
      expires_in: number;
      poll_interval: number;
    };

export interface OAuthSubmitResponse {
  ok: boolean;
  status: "approved" | "error";
  message?: string;
}

export interface OAuthPollResponse {
  session_id: string;
  status: "pending" | "approved" | "denied" | "expired" | "error";
  error_message?: string | null;
  expires_at?: number | null;
}

// ── Model info ──────────────────────────────────────────────────────

export interface ModelInfo {
  model: string;
  model_short: string;
  provider: string;
  context_length: number;
}

// ── System health ───────────────────────────────────────────────────

export interface SystemHealth {
  cpu_percent: number;
  memory_percent: number;
  memory_total_gb: number;
  memory_used_gb: number;
  disk_percent: number;
  disk_total_gb: number;
  disk_used_gb: number;
  uptime: number;
  load_avg: number[] | null;
}

// ── Agents ──────────────────────────────────────────────────────────

export interface AgentInfo {
  name: string;
  display_name: string;
  status: string;
  mode: string;
  worktree: boolean;
  model: string;
  uptime?: number;
  prompt?: string;
  output?: string;
}

// ── Process info ────────────────────────────────────────────────────

export interface ProcessInfo {
  pid: number;
  name: string;
  cpu: number;
  mem: number;
  running: boolean;
}
