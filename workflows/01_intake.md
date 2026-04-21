# Workflow 01 — Intake

**Agent:** 1 (Intake & Business Context)
**Phase:** `intake` → advances to `reconciliation` once documents + client profile are saved

## Objective

Open an audit engagement for a specific client + period, collect the four core QBO exports, and capture enough business context for later agents to interpret the data correctly.

## Delivery surface

**Intake happens in a 1:1 Teams DM** between the bookkeeper and `AuditBot` (personal scope). The per-client Teams channel is reserved for the end of the flow — Agent 4's final review memo gets posted there so the CPA can approve it in context with the rest of the client's activity.

Why personal DM for intake (learned the hard way): Teams channels silently strip user-uploaded file attachments from bot activity payloads. Drag-and-drop uploads in channels show up to humans but never reach the bot. In 1:1 DMs, files arrive with `content_type=application/vnd.microsoft.teams.file.download.info` and a SharePoint `downloadUrl` we can fetch directly.

## Required inputs

Sent by the bookkeeper in their 1:1 DM with AuditBot:

1. **Journal Report** (CSV / XLSX / PDF) — full GL detail for the period
2. **Balance Sheet** (XLSX / PDF) — as-of period-end, with prior-period comparative
3. **Profit & Loss** (XLSX / PDF) — for the period, with prior-period comparative
4. **Bank Statements** (PDF) — every bank + credit-card account held during the period

Plus answers to the intake questionnaire (Agent 1, driven by adaptive cards).

## Trigger

Bookkeeper DMs AuditBot (no @mention needed in 1:1):

```
new audit <period>
```

Examples: `new audit Q3 2026`, `new audit FY2025`, `new audit 2026-01-01 to 2026-03-31`.

## Steps (Phase 1 — skeleton, verified working 2026-04-21)

1. Bot recognizes the trigger, checks no engagement is already active for the bookkeeper's 1:1 `conversation_id`.
2. Bot creates a new engagement row in the index DB and a per-engagement SQLite at `.tmp/engagements/<id>/engagement.db`.
3. Bot replies with the **Intake Request** adaptive card listing the four documents.
4. Bookkeeper drags the files into the chat. Bot:
   - downloads each from the Teams download URL to `.tmp/engagements/<id>/<safe-filename>`
   - classifies `doc_type` from filename (`journal`, `balance_sheet`, `pnl`, `bank_statement`, else `unknown`)
   - inserts a row in `documents`
   - acknowledges with "Received `<name>` (classified as `<type>`)"

## Steps (Phase 3 — Agent 1 Q&A, not yet implemented)

5. After all four documents are attached, Agent 1 posts the intake questionnaire card.
6. Agent 1 collects: industry, revenue model, provinces of operation, GST/HST/QST/PST registration status, fiscal year end, typical vendors, prior-period known issues, payroll status, SR&ED or other specialty items.
7. Any answer stored in `client_profile.profile_json`. Each Q/A pair logged to `conversations`.
8. Agent 1 reads any prior-period engagement DB (same `channel_id`, most recent `phase = 'delivered'`) to carry over context.
9. When the profile is complete, Agent 1 advances `phase` from `intake` to `reconciliation` (Agent 3 runs next — the plan runs 3 → 5 → 2).

## Output

- Row in `engagements` / `engagement_index` with `phase = 'reconciliation'`
- Four rows in `documents` (classified, stored locally)
- `client_profile` populated with `profile_json`
- `conversations` log of every intake Q/A

## Error handling

- **Unknown doc type on upload:** accept it, mark `doc_type = 'unknown'`; in Phase 3, Agent 1 asks the bookkeeper to identify it.
- **Duplicate doc type:** later upload wins (keeps both files on disk, latest row is active). Bot notes the replacement.
- **Active engagement already exists:** bot refuses to start a new one in the same channel until the existing engagement is `delivered`.
- **Channel not a Team channel (e.g., 1:1 chat):** bot explains it must be installed in a channel.

## Learnings & edge cases

- **Channel scope cannot receive user-uploaded files (2026-04-21).** Tested in a Teams channel with `scopes=[team, personal]`. Bookkeeper @mentioned the bot with 3 Excel files — Teams forwarded the activity with only the `text/html` mention entity, stripped the file attachments. Moving intake to personal scope solved it instantly on the same manifest.
- **No `MICROSOFT_` prefix on SDK config attrs (2026-04-21).** `ConfigurationServiceClientCredentialFactory` in `botbuilder-integration-aiohttp` reads `APP_ID`, `APP_PASSWORD`, `APP_TYPE`, `APP_TENANTID` via `hasattr` — the `MICROSOFT_`-prefixed attribute names cause a silent fall-through to `app_id=None` and every inbound request returns 401. Env var names can stay prefixed (Microsoft's convention), but the object handed to the auth factory must expose the short names.
- **Teams backoff after repeated 4xx/5xx from the bot endpoint.** After ~8 failed 502s, the Bot Connector stops attempting delivery for several minutes. Sign: new messages don't appear in ngrok at all. Fix: remove + reinstall the app in the Team, or wait.
