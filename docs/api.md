# API backendu SOC Agent Lab

Základ: `http://127.0.0.1:8711`. Všetky cesty začínajú `/api`. Frontend vo vývoji volá relatívne `/api/...` cez Vite proxy.

Chyby majú jednotný tvar (HTTP 404/409/422/503):

```json
{ "error": { "code": "RUN_IN_PROGRESS", "message": "Iný beh práve prebieha. ...", "request_id": "a1b2c3" } }
```

Kódy: `NOT_FOUND` (404), `RUN_IN_PROGRESS` (409), `APPROVAL_STATE` (409), `VALIDATION_ERROR` (422), `LIVE_MODE_UNAVAILABLE` (503).

## Endpointy

| Metóda | Cesta | Telo | Odpoveď |
|---|---|---|---|
| GET | `/api/health` | – | `HealthResponse` |
| GET | `/api/demo/state` | – | `DemoState` |
| POST | `/api/demo/reset` | `{ "clear_history": false }` (voliteľné) | `DemoState` |
| POST | `/api/demo/inject` | `{ "attack_id": "exfil_email" }` (voliteľné, predvolene `status_override`) | `DemoState` (idempotentné) |
| GET | `/api/incidents` | – | `Incident[]` |
| GET | `/api/incidents/{id}` | – | `Incident` |
| POST | `/api/runs` | `StartRunRequest` | `RunRecord` (202) |
| GET | `/api/runs` | – | `RunRecord[]` (najnovší prvý) |
| GET | `/api/runs/{id}` | – | `RunDetail` |
| GET | `/api/runs/{id}/events` | – | SSE stream `RunEvent` |
| GET | `/api/audit?run_id=&limit=` | – | `AuditEntry[]` |
| GET | `/api/metrics` | – | `MetricsGroup[]` (vždy 2 skupiny: live, replay) |
| GET | `/api/approvals` | – | `ApprovalRequest[]` |
| POST | `/api/approvals/{id}/decision` | `{ "approve": true }` | `ApprovalDecisionResult` |

`POST /api/runs` so `scenario: "attack"` automaticky vloží útok zvolenej úlohy (`TaskInfo.attack_id`), ak ešte nie je vložený.
Počas behu vracia `POST /api/runs`, `/demo/reset` a `/demo/inject` chybu 409.

## Typy (TypeScript)

```ts
type Severity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
type IncidentStatus = "OPEN" | "INVESTIGATING" | "RESOLVED";
type SecurityMode = "vulnerable" | "protected";
type ExecutionMode = "live" | "replay";
type Scenario = "normal" | "attack";

interface EvidenceItem {
  id: string; kind: string; title: string;
  trust: "trusted" | "untrusted";
  source: string; content: string;
  injected: boolean;            // true = simulovaný škodlivý obsah (model tento príznak nevidí)
}
interface Incident {
  id: string; title: string; description: string;
  severity: Severity;           // pôvodná (nahlásená) závažnosť
  status: IncidentStatus;
  reported_at: string; source: string;
  affected_assets: string[]; signals: string[];
  evidence: EvidenceItem[];
}

interface LLMStatus {
  configured: boolean; model: string | null; endpoint_host: string | null;
  api_key_configured: boolean;   // iba príznak, kľúč sa nikdy neposiela
  reasoning_effort: string | null;
  protocol: "langchain" | "native-tools" | "json-tools";
}
interface TaskInfo {
  id: string; title: string; instruction: string;
  description: string;          // čo úloha ukazuje a aký útok k nej patrí
  write_scope: "none" | "status_change_with_approval";   // update_incident
  actions: string[];            // ďalšie povolené zápisy, napr. ["send_email"] alebo ["schedule_payment", "update_vendor_bank"]
  attack_id: string;            // útok, ktorý sa vloží pri scenári "attack"
  domain: "soc" | "finance" | "helpdesk";   // incidenty a prostredie, faktúry a platby, alebo RAG nad IT príručkou
}
interface AttackInfo {
  id: "status_override" | "exfil_email" | "poisoned_threat_feed" | "poisoned_playbook" | "vendor_bank_change" | "poisoned_kb";
  title: string; summary: string;
  target_kind: "incident_evidence" | "playbook_section" | "invoice_document" | "kb_section";
  target_id: string;            // "INC-002", "KB-PHISH-01" alebo "FA-2026-118"
  evidence_id: string; present: boolean; notice: string;
}
interface PlaybookSection { id: string; trust: "trusted" | "untrusted"; source: string; content: string; injected: boolean }
interface EnvironmentView {
  firewall_blocklist: { ip: string; reason: string; asset_name: string | null }[];
  sender_allowlist: string[];
  outbox: { message_id: string; to: string; subject: string; body: string; external: boolean }[];
  playbooks: { id: string; title: string; keywords: string[]; sections: PlaybookSection[] }[];
  assets: { ip: string; name: string; role: string; critical: boolean }[];
  internal_email_domains: string[];
}
interface FinanceView {
  vendors: { id: string; name: string; iban: string; iban_verified: boolean; contact: string }[];
  invoices: { id: string; vendor_id: string; vendor_name: string; amount_eur: number; due_date: string;
              iban: string; paid: boolean; documents: EvidenceItem[] }[];
  payments: { id: string; invoice_id: string; vendor_id: string; iban: string; amount_eur: number;
              account_verified: boolean }[];
}
interface KnowledgeView {        // scenár H, RAG
  doc_id: string; title: string;
  chunks: { id: string; section: string; text: string; chars: number; trust: "trusted" | "untrusted";
            source: string; injected: boolean }[];
  replies: { id: string; to: string; subject: string; body: string; links: string[]; external_links: string[] }[];
  chunk_size: number; chunk_overlap: number; top_k: number; embedding_model: string;
}
interface DemoState {
  incidents: Incident[];
  environment: EnvironmentView;
  finance: FinanceView;          // simulované účtovníctvo (scenár G)
  knowledge: KnowledgeView;      // znalostná báza a odpovede helpdesku (scenár H)
  attacks: AttackInfo[];
  active_run_id: string | null;
  pending_approvals: number;
  tasks: TaskInfo[];            // analyze_report (predvolená), triage_with_approval, notify_lead, contain_threat, playbook_response, pay_invoices, helpdesk_answer
  llm: LLMStatus;
  principal: { user_id: string; display_name: string; role: string };
}
interface HealthResponse { status: "ok"; version: string; llm: LLMStatus; active_run_id: string | null }

interface StartRunRequest {
  task_id?: string;             // predvolene "analyze_report"
  security_mode: SecurityMode;
  execution_mode: ExecutionMode;
  scenario?: Scenario;          // predvolene "normal"
}

interface StateChange {
  target: "incident" | "firewall" | "mail_allowlist" | "outbox" | "payment" | "vendor_bank" | "reply";
  target_id: string;            // INC-003, 198.51.100.10, acme-verify.example, príjemca e-mailu
  field: string; before: string; after: string;
  summary: string;              // slovenská veta pre UI
}
interface EnvironmentSnapshot { blocked_ips: string[]; sender_allowlist: string[]; emails_sent: string[]; payments: string[]; vendor_accounts: string[]; replies: string[] }
interface RunSummary {
  tool_calls_proposed: number; invalid_calls: number; write_proposals: number;
  unauthorized_attempts: number;   // návrhy, ktoré brána zamieta (vynútene alebo v tieni)
  unauthorized_executed: number;   // neautorizované akcie, ktoré sa naozaj vykonali (iba zraniteľný režim)
  blocked: number; pending_approvals: number;
  suspected_injection_influence: number; // heuristika: parametre zápisu sa nachádzajú v nedôveryhodnom texte
  model_turns: number; model_errors: number; model_latency_ms: number;
  state_changes: StateChange[];
}
interface IncidentSnapshot { id: string; status: string; severity: string }
interface RunRecord {
  id: string; task_id: string; task_title: string; instruction: string;
  security_mode: SecurityMode; execution_mode: ExecutionMode; scenario: Scenario;
  attack_id: string; attack_title: string;
  injection_present: boolean;   // útok zvolenej úlohy bol v dátach
  status: "running" | "completed" | "failed";
  started_at: string; finished_at: string | null; duration_ms: number | null;
  model_label: string;          // "Živý model: openai/gpt-oss-120b" alebo "Syntetický záznam: ..."
  report: string | null;        // markdown
  error: string | null;
  summary: RunSummary;
  incidents_before: IncidentSnapshot[]; incidents_after: IncidentSnapshot[];
  environment_before: EnvironmentSnapshot; environment_after: EnvironmentSnapshot;
}
interface RunDetail { run: RunRecord; events: RunEvent[] }
```

### Udalosti behu (`RunEvent`)

```ts
type EventType =
  | "run_started" | "agent_handoff" | "agent_status" | "untrusted_content_loaded"
  | "model_request" | "model_output" | "model_error"
  | "tool_call_proposed" | "tool_call_rejected" | "policy_decision"
  | "tool_executed" | "tool_blocked" | "approval_requested" | "state_changed"
  | "report_ready" | "run_completed" | "run_failed";
type AgentName = "user" | "orchestrator" | "research" | "analysis" | "policy_gate" | "tool_executor" | "audit";

interface RunEvent {
  seq: number; run_id: string; ts: string;
  type: EventType;
  agent: AgentName | null;
  title: string;                // slovenský nadpis pre časovú os
  message: string;
  level: "info" | "success" | "warning" | "danger";
  provenance: "user" | "untrusted" | "model" | "system" | "policy";  // vizuálne odlíšenie zdroja
  source: "live" | "replay" | "system";   // replay = syntetický výstup modelu
  data: Record<string, unknown>;
}
```

Dôležité polia v `data`:

| Typ | Polia |
|---|---|
| `run_started` | `task_id, task_title, security_mode, execution_mode, scenario, attack_id, attack_title, injection_present, protocol, model_label, principal, write_scope` |
| `agent_handoff` | `from, to, task` |
| `agent_status` | `state` (`working`/`done`), voliteľne `plan`, `incident_ids`, `reason: "repeated_blocked_call"` |
| `untrusted_content_loaded` | `location_kind` (`incident`/`playbook`/`invoice`/`kb`), `location_id, evidence_id, title, source, content, injected`, `incident_id` (iba pri incidente), `via_tool` (napr. `search_playbook`, ak obsah prišiel vo výsledku nástroja) |
| `model_request` | `step, message_count, state, final` (záverečná požiadavka bez nástrojov) |
| `model_output` | `step, thought, tool_calls[{id, name, arguments}], has_final_report, raw_text, model, latency_ms, retried, protocol, reasoning?` (uvažovanie modelu, ak ho endpoint vráti) |
| `model_error` | `kind` (`timeout`, `http_error`, `invalid_response`, `invalid_model_output`, `invalid_tool_call`, `incomplete`), `raw_text?`, `step?` |
| `tool_call_proposed` | `call_id, tool, arguments, is_write, unauthorized_attempt, taint_hint?{location_id, evidence_id, evidence_title, matched_terms}` |
| `tool_call_rejected` | `call_id, tool, arguments` |
| `policy_decision` | `call_id, tool, decision` (`ALLOW`/`DENY`/`REQUIRE_APPROVAL`), `rule_id, reason, enforced` (false = zraniteľný režim, iba tieňové hodnotenie), `inputs_considered[], inputs_ignored[]` |
| `tool_executed` | `call_id, tool, arguments, status` (`executed`/`failed`), `unauthorized, result_preview`. Nadpis: Neautorizovaný zápis vykonaný, Schválený zápis vykonaný alebo Povolená akcia vykonaná |
| `tool_blocked` | `call_id, tool, arguments, rule_id` |
| `approval_requested` | `call_id, tool, arguments, approval_id` |
| `state_changed` | `call_id, tool, arguments, change{incident_id, field, before, after}, unauthorized` |
| `report_ready` | `report` (markdown) |
| `run_completed` / `run_failed` | `status, summary, incidents_before, incidents_after, environment_before, environment_after, duration_ms` |

### SSE

`GET /api/runs/{id}/events` pošle najprv celú doterajšiu históriu, potom nové udalosti. Formát:

```
id: 7
data: {...RunEvent JSON...}

: heartbeat

event: end
data: {"run_id": "RUN-..."}
```

Udalosti bez poľa `event:` prichádzajú cez `EventSource.onmessage`. Koniec streamu je pomenovaná udalosť `end` (`addEventListener("end", ...)`), po nej klient spojenie zatvorí. Pri výpadku prehliadač posiela `Last-Event-ID` a server pokračuje od ďalšej udalosti.

### Ostatné

```ts
interface AuditEntry { seq: number; ts: string; run_id: string | null; actor: string; action: string; outcome: string; summary: string; details: Record<string, unknown> }
interface MetricsRow {
  security_mode: SecurityMode; runs: number; completed: number; failed: number; runs_with_injection: number;
  write_proposals: number; unauthorized_attempts: number; unauthorized_executed: number; blocked: number;
  pending_approvals: number; suspected_injection_influence: number; invalid_calls: number; model_errors: number;
  avg_model_latency_ms: number | null;
}
interface MetricsGroup { execution_mode: ExecutionMode; label: string; note: string; rows: MetricsRow[] }  // rows: [vulnerable, protected]
interface ApprovalRequest {
  id: string; run_id: string; task_id: string; tool: string; arguments: Record<string, unknown>;
  fingerprint: string; status: "pending" | "approved" | "rejected";
  created_at: string; decided_at: string | null; decided_by: string | null;
  taint_hint: { location_id: string; evidence_id: string; evidence_title: string; matched_terms: string[] } | null;
  execution_status: string | null;
}
interface ApprovalDecisionResult { approval: ApprovalRequest; outcome: unknown | null }
```
