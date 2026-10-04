/** Shapes of the platform API responses the dashboard uses. */
export type ConversationStatus = "ai" | "waiting_human" | "human" | "resolved";

export interface Conversation {
  id: string;
  channel: "web" | "telegram";
  visitor_id: string;
  status: ConversationStatus;
  customer_name: string | null;
  handoff_reason: string | null;
  handoff_requested_at: string | null;
  staff_read_at: string | null;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_preview: string | null;
  last_message_role: string | null;
  last_message_at: string | null;
  unread: boolean;
}

export interface Citation {
  marker: number;
  chunk_id: string;
  document_title: string;
  metadata: Record<string, unknown>;
  snippet: string;
}

export interface ToolRecord {
  step: number;
  tool: string;
  status: string;
  summary: string;
  arguments?: Record<string, unknown>;
  result?: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant" | "staff";
  content: string;
  citations: Citation[];
  outcome: string | null;
  model: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  timings: Record<string, number>;
  retrieval: { tools?: ToolRecord[] } | null;
  error: string | null;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

export interface Overview {
  timezone: string;
  window_days: number;
  conversations_today: number;
  conversations_week: number;
  daily: { day: string; conversations: number }[];
  outcomes: Record<string, number>;
  handoff_rate: number | null;
  median_first_token_ms: number | null;
  tokens: { prompt: number; completion: number };
  estimated_cost_usd: number;
  recent_gaps: { question: string; asked_at: string; conversation_id: string; message_id: string }[];
}

export interface DocumentRow {
  id: string;
  title: string;
  source_type: string;
  source_uri: string | null;
  status: "pending" | "processing" | "ready" | "failed";
  chunk_count: number;
  size_bytes: number | null;
  error: string | null;
  updated_at: string;
  created_at: string;
}

export interface GapGroup {
  question: string;
  count: number;
  last_asked_at: string;
  examples: string[];
  message_ids: string[];
  conversation_ids: string[];
}

export interface SearchTrace {
  query: string;
  mode: string;
  threshold: number | null;
  top_similarity: number | null;
  strong_keyword_match: boolean;
  relevant: boolean;
  decision: "passed" | "blocked";
  reason: string;
  duration_ms: number;
  embedding_cached: boolean;
  reranker: string | null;
  rerank_applied: boolean;
  results: {
    chunk_id: string;
    document_title: string;
    location: string;
    snippet: string;
    vector_score: number | null;
    keyword_score: number | null;
    fused_score: number | null;
    rerank_score: number | null;
  }[];
}

export interface Trace {
  message_id: string;
  conversation_id: string;
  created_at: string;
  question: string | null;
  reply: string;
  outcome: string | null;
  model: string | null;
  failovers: { model: string; error: string }[];
  fallback: boolean;
  searches: SearchTrace[];
  tools: ToolRecord[];
  citations: Citation[];
  timings_ms: Record<string, number>;
  tokens: { prompt: number | null; completion: number | null };
  error: string | null;
}

export interface Reservation {
  id: string;
  reference: string;
  local_date: string;
  local_time: string;
  party_size: number;
  name: string;
  phone: string;
  notes: string | null;
  status: string;
  conversation_id: string | null;
  created_at: string;
}

export interface Lead {
  id: string;
  name: string;
  contact: string;
  interest: string;
  status: string;
  conversation_id: string | null;
  created_at: string;
}

export interface ApiKey {
  id: string;
  kind: "admin" | "widget";
  prefix: string;
  label: string;
  created_at: string;
  revoked_at: string | null;
}

export interface Delivery {
  id: string;
  event_type: string;
  status: string;
  attempts: number;
  last_status_code: number | null;
  last_error: string | null;
  created_at: string;
  delivered_at: string | null;
}
