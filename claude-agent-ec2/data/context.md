# Vylo — workspace context

This file is the entry point for any session working inside `~/Documents/code/vylo/`.
It is NOT a git repo itself — each subfolder below is its own independently cloned
repository. **When a new repo is added to this folder, add a section for it here**
(purpose, tech stack, key modules, how it's deployed, its ELK index) so this stays
the single map of "what lives in Vylo."

Business context: **Vylo** is WheelsEye's voice/conversation-intelligence product line
(product label `voice`, k8s label `business=vylo`). Everything in this folder serves
that product, across several repos owned by different parts of the stack (STT
pipeline, the voiceLog Java backend, the AI/chat layer, client apps, and marketing).

---

## Jira

**Project key: `VOIC`** (name: *Voice Telematics*). File all bugs/tasks for anything
in this folder under `VOIC`, not a generic/shared project. Example:
[VOIC-250](https://wheelseye.atlassian.net/browse/VOIC-250) — stt-api-worker
crash-loop from a duplicate S3 cleanup task (filed 2026-10-21), useful as a model
for how an RCA writeup should look in this project (alert context, root cause
chain with file/line references, supporting metrics, suggested fix).

### Every PR needs a linked Jira ID in the title, or CI rejects it

**Format: `<JIRA-ID> | <title>`** — e.g. `VOIC-254 | Fix notification claim race in
UserNotificationService`. The ID goes first, then a space-pipe-space, then the
normal descriptive title. This applies to **every PR in every repo in this
folder** (`voiceLog`, `we-gitops-k8s`, `stt-platform`, etc.) — CI checks for it and
blocks approval on a PR without one, so **file or identify the Jira ticket before
opening the PR**, not after:
1. If no ticket exists yet for the change, create one in `VOIC` first (see above).
2. Put that ticket's key at the start of the PR title in this format.
3. Only then open the PR.

This was learned the hard way — [voiceLog#639](https://github.com/WeyeTech/voiceLog/pull/639)
and [we-gitops-k8s#7091](https://github.com/WeyeTech/we-gitops-k8s/pull/7091) were both opened
*without* this prefix (before the convention was written down here) and need their titles
fixed to match, or CI will reject them.

### Every PR also needs exactly one of three GitHub labels

`bug`, `feature`, or `tech-debt` (the label is literally named `tech-debt` on GitHub, even
though it's usually said out loud as "tech dev") — check `gh label list` on the specific repo
first, since this isn't necessarily the same label set everywhere in this folder. Pick based on
what the change actually is, not how it was found:
- **`bug`** — fixes something that was behaving wrong (a real defect, a crash, incorrect output).
- **`feature`** — adds new user-facing capability.
- **`tech-debt`** — everything else that isn't product-facing: config/threshold tuning,
  observability/tooling improvements, refactors, dependency bumps. [voiceLog#639](https://github.com/WeyeTech/voiceLog/pull/639)
  (adding `contextTags=traceId` so Sentry links to ELK) is `tech-debt` — nothing was broken
  before it, and no user-facing behavior changed; it's purely a debugging-capability improvement.

---

## Infrastructure quick reference

- **Kubernetes namespace:** `vylo` (everything in this folder's repos deploys here;
  `istio-injection=enabled`). Relevant pods as of 2026-10-21: `dhwani-engine`,
  `dhwani-kendra`, `rahasya-gateway`, `signal-x-console`, `stt-api-worker`,
  `stt-speaker-tagging-worker`, `stt-worker`, `voice-agentic-chat`,
  `voice-ai-pipline-worker`, `voicelog-admin`, `voicelog-heimdall`.
- **Primary RDS:** `voicelog-tenant` (`db.t4g.small`, Postgres 18, ap-south-1,
  `DbiResourceId: db-DPFH3P4QP6ZY34OKGBJ7LPJEZI`). Shared by far more than its name
  suggests — see "Who actually connects to `voicelog-tenant`" below. 2GiB RAM,
  `max_connections` ≈ 225 (unmodified RDS default formula). Performance Insights is
  enabled on it; CloudWatch Logs export is **not** enabled (no Postgres server logs
  available via CloudWatch Logs Insights — Performance Insights + CloudWatch metrics
  are the only visibility).
- **Grafana dashboard pattern:** `http://grafana.prod-we.com/d/v03IottSk/backend-7c-application-monitoring?...&var-Namespace=vylo&var-App_name=<app>&var-pod=<pod>`
- **AWS account/region:** `489284938287`, `ap-south-1`.
- **Secrets:** Vault + Vault Secrets Operator (VSO) issue dynamic DB credentials for
  the STT pipeline services (role names like `v-stt-api-w-<timestamp>`,
  `v-stt-worker-<timestamp>`). `voicelog-heimdall` resolves tenant DB credentials via
  **AWS Secrets Manager** instead (`TenantSecretResolver`/`TenantGuard` in
  `multiverse`), not Vault.
- **CDC:** Debezium connectors replicate out of `voicelog-tenant` under users
  `dbz_stt_pipeline`, `dbz_vylo_tenant_004`, `dbz_langgraph_askvylo` — each holds a
  long-lived logical replication slot/connection, independent of any app pool.

### Who actually connects to `voicelog-tenant`

Confirmed logical databases seen on this one RDS instance (via Performance Insights'
`db` dimension, last 7 days — there may be more dormant ones not listed):

| DB name | Owner | Notes |
|---|---|---|
| `stt_pipeline` | `stt-platform` (all 3 workers) | single shared DB, not per-tenant |
| `vylo_te000`, `vylo_te001`, `vylo_tenant_002`, `vylo_tenant_003`, `vylo_tenant_004`, `vylo_tenant_006` | `voiceLog`/`heimdall` via `multiverse` | one DB **per tenant**, routed dynamically — see below |
| `langgraph_askvylo` | `AudioVerse_Claude/apps/vylo_chat` (= `voice-agentic-chat` pod) | control-plane DB (threads/checkpoints); despite local docker-compose comments implying isolation, in prod it lives on this same instance |
| `rdsadmin` | AWS-managed | — |

As of 2026-10-21: **6 tenants provisioned, 1 actively used**, the rest idle/negligible
traffic (still each hold `minimumIdle=2` HikariCP connections permanently once
touched by a pod — see `voiceLog`'s multiverse section below).

---

## Repos you do NOT have access to (read this first)

Three repos in this map are shared with other teams and are **deliberately not cloned on this
box and not reachable with the agent's GitHub token**: `we-gitops-k8s`, `aggregator-nginx-conf`
and `signal-x-console`. They are described below so you understand how Vylo fits together, but
you cannot read, change or open a PR on them.

When a task needs a change there (a deploy value or image tag in `we-gitops-k8s`, a route in
`aggregator-nginx-conf`, a UI change in `signal-x-console`):
1. Do not try to clone, fetch or push them, and do not work around the missing access.
2. Finish the part that lives in your repos, and write down exactly what the other repo needs
   (file, setting, old and new value, and why).
3. Tell the person who asked, and ask them to make that change or hand it to the owning team.

---

## Repos in this folder

### `stt-platform` — the STT pipeline (Python)
Four Kafka-connected workers behind one FastAPI gateway, sharing a common library
(`core/stt_core`). Full architecture in `stt-platform/CLAUDE.md` — read that before
touching this repo, it's extensive (job lifecycles, speaker tagging internals,
scheduling, DB schema).

- **`workers/api`** (pod `stt-api-worker`) — FastAPI gateway: STT jobs, speakers,
  speaker-tagging jobs. Runs `uvicorn --workers 2` (2 processes/pod).
- **`workers/stt`** (pod `stt-worker`) — pull-based claim loop against Postgres,
  calls STT providers (Sarvam, ElevenLabs), encrypts + uploads transcripts to S3.
- **`workers/speaker_tagging`** (pod `stt-speaker-tagging-worker`) — Kafka consumer,
  pyannote WeSpeaker embeddings + Qdrant search to map diarized speakers.
- **`workers/vad`** — disabled, not deployed (kept in source for `#79`-style revival).
- **DB:** owns the `stt_pipeline` Postgres DB on `voicelog-tenant`. Pool config is
  `core/stt_core/config/settings.py` (`pool_size=20, max_overflow=10` default) —
  **`speaker_tagging`'s bootstrap calls `get_async_engine()` without passing these,
  so it silently runs on the function's own defaults (5/10) instead** — a real,
  easy-to-miss divergence if you're ever reasoning about this service's DB load.
- **Known issue:** VOIC-250 — a background S3 cleanup task
  (`workers/api/app/tasks/cleanup.py`) runs once per uvicorn worker with no
  singleton guard, double-processes the same batch, and holds an idle-in-transaction
  DB connection for the whole burst, blocking the liveness probe long enough to get
  SIGKILLed. Recurring, not yet fixed as of 2026-10-21.

### `voiceLog` — the Java backend (`git@github.com:WeyeTech/voiceLog.git`)
Java 21 / Spring Boot 3.5.6, Maven multi-module. Full detail in `voiceLog/CLAUDE.md`
(it's large — multi-tenancy, fix-word, folders, commerce/billing, Apple IAP, etc.).

| Module | Type | Port/path | Pod | Purpose |
|---|---|---|---|---|
| `core` | library | — | — | shared auth, Redis, S3, Kafka config, exceptions, DTOs |
| `web` | Spring Boot app | 8080 `/web` | `voicelog-web` | mobile/device-facing APIs |
| `admin` | Spring Boot app | 8080 `/admin` | `voicelog-admin` | dashboard backend: recordings, analytics, call tagging |
| `heimdall` | Spring Boot app | 8080 `/ums` | `voicelog-heimdall` | identity: employees, roles, scopes, devices, JWT issuance |
| `multiverse` | library | — | — | multi-tenant DB routing (Postgres, MongoDB, ClickHouse) |
| `stub-server` | Spring Boot app | — | — | Spring Cloud Contract stub server |

**`multiverse`'s per-tenant Postgres routing is the thing to understand before
reasoning about `voicelog-tenant` load:**
`TenantRoutingDataSource` (`AbstractRoutingDataSource`) picks a per-tenant
`HikariDataSource` from `DataSourceCache`, keyed `tenantId:service:dbEngine`, created
on first use by `TenantDataSourceProvider` with **`minimumIdle=2, maximumPoolSize=5`
per tenant**. Only `heimdall` wires this with `DbEngine.POSTGRESQL` (so it's the only
module hitting `voicelog-tenant` this way — `admin`'s copy of the pattern is for
ClickHouse, a different DB). **No eviction path was found anywhere in the repo**
outside `DataSourceCache.evict()`/`evictAll()` themselves — nothing calls them — so
once a `heimdall` pod serves a request for a tenant, that tenant's 2-5 connections
stay open for the pod's entire lifetime, growing with tenant count, never shrinking.
Tenant codes: `TE000`–`TE006`+ (`tenantConfig/*.yml` per module; `default.yml` is the
fallback, not a tenant).

### `AudioVerse_Claude` — AI/ingestion layer (Python monorepo, Poetry workspaces)
`apps/*` are deployable services, `libs/*` shared packages (path deps).

- **`apps/vylo_chat`** (pod `voice-agentic-chat`) — "AskVylo" chat agent (LangGraph).
  Single uvicorn process (no `--workers` flag). Postgres pool is
  `psycopg_pool.AsyncConnectionPool`, `DB_POOL_MIN=2`/`DB_POOL_MAX=10`
  (`store/pool.py`, `config.py`) — this is the `langgraph_askvylo` DB on
  `voicelog-tenant` (see infra note above re: it not actually being isolated from
  prod despite local docker-compose comments). Reads tenant business data only
  through an MCP tool layer (`agent/tools/mcp_pool.py`), never a direct tenant DB
  connection.
- **`apps/vylo_chat_ui`** — the web frontend for AskVylo (what `vylo_gpt` ports to
  Flutter — see below).
- **`apps/ai_work_flow`** — the LangGraph pipeline that writes `ai_classification`,
  `ai_summary`, `call_processing_history` etc. into voiceLog's MongoDB collections
  (see voiceLog's CLAUDE.md "Key MongoDB Collections" table) and mirrors to
  `ai_summary_dump`.
  pod: `voice-ai-pipline-worker`.
- **`apps/agent_extraction`, `apps/audio_processing`, `apps/embarding`,
  `apps/modeltester`, `apps/preprocess-worker`, `apps/scheduler`,
  `apps/summarization-service`, `apps/tagging-service`** — supporting
  pipeline/worker apps; not yet individually mapped to k8s pod names in this doc —
  check `kustomize-controller`/deployment manifests if you need to trace one.
  `apps/scheduler` owns **all** time-triggered work across this repo (one-shot jobs
  + k8s CronJob, never a bolted-on loop in a Kafka worker — see
  `AudioVerse_Claude/CLAUDE.md`).
- **`libs/master_postgres`** — `heimdall`'s `/api/v1/master/templates/{code}` HTTP
  client (confusingly named "multiverse" in its docstring — this is unrelated to
  `voiceLog`'s `multiverse` module; it's just calling heimdall's HTTP API for
  template/master-data lookups, not a DB connection).

### `vylo_gpt` — Flutter package (Dart only)
Ports `apps/vylo_chat_ui` (the AskVylo web chat) into a reusable Flutter package for
embedding in native Vylo mobile apps. First consumer: Myelo Individuals
(`companion_app`). No backend/DB of its own — pure client, calls the same API
`vylo_chat_ui` does (`VyloChatApiClient`, Dio + SSE).

### `VyloVoiceDesktop` — Electron desktop client (macOS/Windows)
`vylovoice-desktop`. See `VyloVoiceDesktop/CLAUDE.md` + `docs/agent/INDEX.md` for the
full feature index — that repo enforces a **worktree-only edit policy** (a PreToolUse
hook denies edits on the main checkout; always work in a git worktree there). No
server-side component — calls the voiceLog/STT APIs.

### `signal-x-console` — "VoiceIQ Dashboard" (Next.js 15, JavaScript)
Front-end for call-recording analytics, AI summaries, team management. Served under
`basePath: '/console'`. Redux Toolkit for auth state (multi-tenant). No direct DB
access — calls `voicelog-admin`'s APIs. Pod: `signal-x-console`.
*No corresponding ELK index found as of 2026-10-21* — check Grafana/browser-side
logging instead, or confirm with the team if one exists under a different name.

### `vylo-one-landing-page` — marketing site (Vite + React + TS)
Deployed at **https://vylo.one**. Pod/ELK index: `landing-page`. Fix policy (per
Jitesh, 2026-07-16): pure engineering fixes (perf, response-time trade-offs) — just
implement; anything touching product/pricing/copy/SEO strategy — post as a PR
comment, don't implement directly.

### `we-gitops-k8s` — the Flux GitOps repo (`git@github.com:WeyeTech/we-gitops-k8s.git`)
**Not Vylo-specific — this is WheelsEye's whole-company GitOps repo**, cloned here
for convenience because it's where every Vylo service's actual deployed config
lives (resource requests/limits, probes, HPA, image tags) — the source of truth
that live `kubectl`/pod specs are rendered from. FluxCD reconciles it continuously;
**the deployed image tag in `release.yaml` is the fastest way to confirm whether a
local checkout of this repo (or of any app repo) is actually what's running in
prod**, since Flux auto-commits image-tag bumps on every deploy (`{"$imagepolicy":
...}` comments mark those fields).

Layout: `app/<env>/<service>/` holds three files per service — `values.yaml` (a
ConfigMap with the actual Helm values: resources, probes, HPA, affinity, secrets),
`release.yaml` (the Flux `HelmRelease`, pins the chart version + image tag),
`helmRepo.yaml` (the `HelmRepository` source). Vylo's services live under
`app/prod/voicelog-web/`, `app/prod/voicelog-heimdall/`, etc. — same split across
namespaces as the live cluster (`voicelog-web` in `horizontal`, `voicelog-heimdall`
in `vylo` — see the k8s namespace note above).

**This repo has many long-lived branches** (other teams' in-flight work) — when
starting a change here, always branch fresh off `origin/master`
(`git fetch && git checkout -b <branch> origin/master`), never off whatever branch
happened to be checked out, and switch back to that original branch when done so
the next session isn't surprised by an unrelated branch switch.

Example: [PR #7091](https://github.com/WeyeTech/we-gitops-k8s/pull/7091) bumped
`voicelog-web`/`voicelog-heimdall`'s `startupProbe.failureThreshold` after
boot-time data (pulled from their ELK indices) showed the old budget sat below
their p90 boot time — a model for how a GitOps change here should be justified
(real measured data in the PR description, not a guessed number).

### `aggregator-nginx-conf` — the public edge / API gateway (`git@github.com:WeyeTech/aggregator-nginx-conf.git`)
**Not Kubernetes at all — a standalone nginx fleet, deployed by Ansible
playbooks onto EC2** (`playbook-{dev,stage,prod}.yml` + `hosts.yml`), separate from
both `we-gitops-k8s`/Flux and the in-cluster Istio gateways. This is the actual
internet-facing hop: a request to `voice.wheelseye.com` or `mcp.vylo.one` hits this
nginx first, which then proxies to the Istio-fronted internal hostnames
(`<service>.prod-we.com`) that `we-gitops-k8s`'s `virtualService.host` entries
publish — i.e. this repo's `proxy_pass` targets and that repo's VirtualServices
must agree, and a routing change usually needs both.

Layout: `production/`, `staging/` (with `conf-slot-1.d`/`conf-slot-2.d` — a
blue/green pair), `development/`, each with an `nginx.conf` + `conf.d/*.conf` per
virtual host. **Not Vylo-specific** — `conf.d/` holds every WheelsEye product's
vhost (`thor.conf`, `odin.conf`, `matrix.conf`, etc.), Vylo's are just the ones
named `voicelog-web.conf`, `vylo.conf`, `vylo-deeplink.conf`, `mcp-vylo.conf`.

Two Vylo-relevant vhosts in `production/conf.d/`:
- **`voicelog-web.conf`** (`voice.wheelseye.com`) — the main Vylo Business entry
  point. Routes `/web/` → `voicelog-web`, `/admin/` → `voicelog-admin`, `/ums/` →
  `voicelog-heimdall`, `/console/` → `signal-x-console`, `/ask-api/` →
  `voice-agentic-chat` (AskVylo, with `proxy_buffering off` + 3600s timeouts for
  streaming), and the ElevenLabs STT webhook (`= /stt/v1/webhooks/elevenlabs`) →
  `stt-api-worker` directly. Also hard-blocks `~* (^|/)internal(/|$)` (voiceLog's
  service-to-service surface, must never be internet-reachable) and `/actuator`.
- **`mcp-vylo.conf`** (`mcp.vylo.one`) — the dedicated MCP connector host for
  **Individuals** (tenant `TE004`), separate from `voice.wheelseye.com` on purpose:
  OAuth discovery (`/oauth/authorize`, `/.well-known/oauth-authorization-server`,
  etc.) happens before any session/tenant context exists, so the product has to be
  encoded in which *host* was hit, not in a path or header a client could spoof.
  Routes to `voicelog-admin` (MCP endpoint, resource metadata) and
  `voicelog-heimdall` (OAuth authorize/token/register), both with a `/individual`
  suffix injected by the rewrite to tell heimdall/admin which product's identity
  flow to run.

**Known gotcha preserved in the file's own comments, worth knowing before touching
either vhost:** `proxy_pass` with a nginx *variable* host and no URI part passes the
request URI through unchanged — so any path rewrite for that target must use
`rewrite ... break`, it cannot be expressed on the `proxy_pass` line. Several OAuth
`.well-known` routes exist specifically to work around RFC 8414's "insert the
well-known segment before the issuer's path" rule, which breaks naively for an
issuer like `.../ums` that has a path component.

**No ELK index for this fleet's access/error logs** (checked 2026-10-22 — no
`nginx`/`aggregator`/`gateway`-named index exists, and `app-api-gateway-service-*`
is an unrelated Kubernetes service in `saas-gps`, not this). The repo's own
playbooks (`playbook-{dev,stage,prod}.yml`) only install and configure nginx —
**no task in this repo provisions a log shipper** (no filebeat/fluentd/CloudWatch
agent task anywhere), so these logs most likely only live locally on the EC2
instances' `/var/log/nginx/` unless something outside this repo's scope ships them
elsewhere. If you need to debug something at this layer (routing, 5xx from the
edge itself, a misfiring `rewrite`), that means SSH to the host or a separate log
source — not a `wheelseye-elasticsearch-mcp` query like everything else in this
folder.

---

## ELK / log indices

Cluster is **shared across all of WheelsEye**, not just Vylo — don't assume every
index belongs to this product. Pattern is `app-<service-name>-YYYY.MM.DD` (new index
per day). Confirmed indices for this folder's services (as of 2026-10-21):

| Service (pod) | Index pattern |
|---|---|
| `voicelog-admin` | `app-voicelog-admin-*` |
| `voicelog-heimdall` | `app-voicelog-heimdall-*` |
| `voicelog-web` | `app-voicelog-web-*` |
| `stt-api-worker` | `app-stt-api-worker-*` |
| `stt-worker` | `app-stt-worker-*` |
| `stt-speaker-tagging-worker` | `app-stt-speaker-tagging-worker-*` |
| `voice-ai-pipline-worker` | `app-voice-ai-pipline-worker-*` |
| `voice-agentic-chat` | `app-voice-agentic-chat-*` |
| `vylo-one-landing-page` | `app-landing-page-*` |

**No matching index found** for `dhwani-engine`, `dhwani-kendra`, `rahasya-gateway`,
`signal-x-console` as of 2026-10-21 — either they log elsewhere (Loki/CloudWatch) or
under a name not yet identified. Confirm before assuming log coverage for these.

Use the `wheelseye-elasticsearch-mcp` tools (`list_indices`, `search_documents`,
`query_logs`) against these patterns; always scope queries to `app-<service>-*`
rather than querying across the whole cluster.

### `app-kubernetes-event-*` — Kubernetes Events, shipped long-term (check this FIRST for any pod-restart/eviction alert)

The cluster already runs a `kubernetes-event-exporter` (pod
`devops-kube-event-exporter-...`, namespace `devops`) that ships **every Kubernetes
Event** (Scheduled, Pulling, Started, Unhealthy, Killing, Evicted, etc.) into this
index, indefinitely — unlike the live `kubectl get events`/k8s API, which only
retains ~1h and is usually already gone by the time an alert gets investigated.
**This is the single most useful index for root-causing `Warning_PodRestart` /
`Warning_Memory` alerts** — it reconstructs the full lifecycle of a since-deleted
pod (scheduled → probe failures → killed/evicted, with exact reasons) when the live
API has nothing left to show.

Query shape (via `elasticsearch_mcp-search_documents`):
```json
{"query": {"match_phrase": {"pod_name": "devops-kube-event-exporter-..."}}}
```
Wait — the exporter's own pod writes these as its *own* log lines (JSON-encoded),
so **search the `log` field for the target pod name**, not `pod_name` (that field
is always the exporter's pod):
```json
{"query": {"match_phrase": {"log": "<target-pod-name>"}}}
```
Each hit's `log` field is a JSON blob with `msg`, `reason`, `involvedObject`, `time`
— `reason` is the fast way to tell real causes apart: `Unhealthy` (probe failure,
check `msg` for "Startup"/"Readiness"/"Liveness"), `Killing` (probe-triggered or
node-drain-triggered kill — check the preceding `Unhealthy`/`Evicted` for which),
`Evicted` with `msg: "... Underutilized"` or `"... Forceful Termination"` (Karpenter
node consolidation — cross-check by querying `kube_pod_info{node="<node>"}` in
Prometheus for a co-located `karpenter-termination-waiter` pod, which only runs on
a node actively being drained).

**Also cross-check `kube_pod_container_status_last_terminated_reason` in
Prometheus** for the same pod — `OOMKilled` vs `Error` is the fastest way to rule
OOM in or out before reading any logs at all (a true OOM kill is an immediate
SIGKILL from the kernel with no graceful shutdown; a probe-triggered kill sends
SIGTERM first and *can* still show a clean app-level shutdown sequence in its own
logs — don't mistake a graceful shutdown for proof it wasn't a probe kill).

---

## Sentry

Self-hosted at `sentry.wheelseye.in`, org slug `wheelseye`. The `wheelseye-sentry-selfhosted`
MCP tools (`list_sentry_issues`, `get_sentry_issue`, etc.) take a **project slug**, not a
pod/service name — use the mapping below.

**Only `voiceLog`'s three Spring Boot apps are actually wired to Sentry.** Confirmed by
grepping the whole folder for `sentry_sdk`/`io.sentry`/`SENTRY_DSN`: `stt-platform`,
`AudioVerse_Claude`, `signal-x-console`, and `vylo-one-landing-page` have **no Sentry SDK
integration at all** — errors from those services are only visible via ELK/Grafana/Loki,
not Sentry. Don't `list_sentry_issues` against a guessed slug for them; it won't exist.

| Pod | Sentry project slug | Project ID | Confirmed via |
|---|---|---|---|
| `voicelog-web` | `voicelog-web` | 125 | `web/src/main/resources/sentry.properties` DSN (`.../125`), matches API `id: "125"` |
| `voicelog-admin` | `voicelog-admin` | 131 | `admin/src/main/resources/sentry.properties` DSN (`.../131`), matches API `id: "131"` |
| `voicelog-heimdall` | `voicelog-heimdall` | 132 | `heimdall/src/main/resources/sentry.properties` DSN (`.../132`), matches API `id: "132"` |

All three report `environment=prod`, team `sentry`, via `io.sentry:sentry-logback` (pulled
in by `voiceLog/core`'s `pom.xml`). `voiceLog/core` and `voiceLog/multiverse` are libraries
with no `sentry.properties` of their own — they report under whichever app embeds them
(`web`/`admin`/`heimdall`).

**If a service outside `voiceLog` ever needs Sentry**, it must be added explicitly (SDK
dependency + DSN config) — nothing here auto-reports. When that happens, add its slug to
the table above the same way it was derived here: read its `sentry.properties`/SDK init
code for the DSN's trailing project ID, then confirm via
`GET /api/0/projects/wheelseye/<slug>/` that the ID matches, rather than guessing a slug
from the pod name — e.g. `stt-api-worker`, `voice-agentic-chat`, `signal-x-console`, and
`landing-page` all 404 as Sentry slugs today even though they're real pods/ELK indices.

---

## Multi-tenancy model (cross-repo)

Tenant codes follow `TE0NN` (e.g. `TE000`, `TE004`, `TE007`). Two **different**
multi-tenancy mechanisms exist across this folder and should not be conflated:

1. **`voiceLog`/`multiverse`** — one physical RDS/Mongo/ClickHouse instance, one
   logical database **per tenant**, resolved per-request via `TenantContext`
   (thread-local, populated by `TenantFilter`). This is what `heimdall` and
   `admin`'s Mongo/ClickHouse configs use.
2. **`stt-platform`** — tenant data lives inside **one shared** `stt_pipeline`
   database/schema (not routed per-tenant at the DB level); tenant scoping happens
   in application queries, not connection routing.

When investigating anything connection-count or load related, always check which of
these two models the service in question uses — they do not scale the same way (one
scales with pod replicas × fixed pool size; the other scales with tenant count ×
per-tenant pool size, and the per-tenant one does not currently evict).

---

## Maintenance note for this file

When a new repo is cloned into `~/Documents/code/vylo/`:
1. Add a `### <repo-name>` section above with: purpose, tech stack, key
   modules/pods, and its DB(s)/connection model if any.
2. Add its ELK index pattern to the table above (or note that none was found).
3. If it talks to `voicelog-tenant` or any other shared DB, add it to the "Who
   actually connects" table.
4. Confirm it also files issues under Jira project `VOIC` unless told otherwise.
