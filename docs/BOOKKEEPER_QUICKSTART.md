# AuditBot — Bookkeeper Quick Start

A one-page reference for sending a client file through the audit. Takes ~2 minutes once you've done it once.

## Before you start

You need:

- **A Microsoft Teams account** on our firm's tenant. The bot is installed — if you don't see **AuditBot** in Chat, ping Taki.
- **Three exports from the client's QuickBooks Online file** for the period you're reviewing:
  1. **Journal Report (Detail)** — Reports → Journal → *Customize* → Report period = the quarter/year you're closing → Export to Excel → save as CSV (or export directly as PDF).
  2. **Balance Sheet** — as of the period-end date. PDF or XLSX.
  3. **Profit and Loss** — for the full period. PDF or XLSX.

Bank statements are optional for now — the bot will run without them and note they're missing.

## The five-step flow

### 1. Open a chat with AuditBot

In Teams, top search bar → type `AuditBot` → click the bot → **Chat**. You get a 1:1 DM.

### 2. Start the audit

Type exactly:

```
new audit <period>
```

Examples:

- `new audit Q3 2026`
- `new audit FY2025`
- `new audit 2026-01-01 to 2026-03-31`

The bot replies with an **Intake Request** card listing the four documents it wants.

### 3. Drop the files

Drag the three files into the chat (or click the paperclip). You can drop them all in one message. For each file, you'll see:

```
Received Ben.K Réno Inc._Journal.csv (classified as journal).
```

Followed by a summary card showing what the bot read (entries, lines, trial balance, top accounts).

### 4. Type `ready`

When you've uploaded everything you're going to upload, type:

```
ready
```

(Alternatives that also work: `done`, `go`, `analyze`, `that's all`.)

### 5. Read the review cards

In ~40 seconds the bot posts five cards back-to-back:

1. **Starting audit** — roll-up of the three docs and key figures
2. **Rollforward (Agent 5)** — balance-sheet ties, retained-earnings snapshot, GST/HST balance, COGS vs. inventory check
3. **Reconciliation (Agent 3)** — duplicate journal entries, missing-account coding, Interac deposit classification
4. **Sales tax (Agent 2)** — GST/HST/QST account structure, net tax position, vendor tax-rate outliers
5. **CPA Review Memo (Opus 4.7 synthesis)** — executive summary, blocking issues, judgment calls, proposed adjusting journal entries, questions for the client

The last card has two buttons:

- **Approve for sign-off** — if the engagement is clean, this marks it delivered.
- **Request changes** — sends the engagement back to you; fix the flagged issues and run it again.

## What the bot flags that a human often misses

- **Missing QST/TVQ account** on a Quebec client whose vendor rates show ~14.975% (5% GST + 9.975% QST). Both portions ending up in "GST/HST Payable" is a return-filing problem.
- **Interac e-Transfer deposits coded as Sales** — receipts required. CRA cannot verify revenue without the underlying sales document.
- **Interac e-Transfer deposits coded as Shareholder Advances** — written confirmation from the owner required. Without it, CRA may reclassify as unreported revenue.
- **COGS with no Inventory account** — closing physical count missing, so period COGS is an estimate.
- **Duplicate journal entries** — same date, account, and amount entered twice.
- **GST/HST Suspense non-zero** — usually indicates an unfiled or unposted return.
- **Vendor implied tax rate off-standard** — suggests partial tax capture on a specific vendor.

## Troubleshooting

**"No active engagement in this channel yet."**
You dropped files before typing `new audit <period>`. Start with the trigger, then upload.

**"An engagement is already active here."**
You tried to start a second audit in the same DM. Finish the first one (click Approve or Request Changes on its memo) before starting another.

**The bot seems unresponsive.**
Teams sometimes holds back messages after backend retries. Wait 1–2 min and re-send. If it's still dead, DM Taki.

**"I only accept file attachments (PDF / XLSX / CSV)."**
The bot saw a file type it doesn't handle. QBO also exports to Google Sheets and HTML — neither are supported. Re-export from QBO as PDF or XLSX.

**The Interac-deposits finding lists a vendor I know is a real shareholder.**
Correct workflow: get a one-paragraph signed statement from the shareholder confirming each Interac transfer was a personal advance to the company, attach it to the engagement's working papers, and move on. The bot flags these because CRA will ask; it isn't saying the coding is wrong.

## What the bot does NOT do (yet)

- It doesn't post back to QBO. All findings are suggestions — you still make the corrections in QBO, then re-run the audit.
- It doesn't reconcile bank statements (yet). If you have them, upload them; they'll land but won't be audited in this version.
- It doesn't check payroll (yet).
- It doesn't look across prior periods (yet). Each engagement is stand-alone.

## Where your files go

Everything lives in a per-engagement SQLite database on the bot's host. **Client data never leaves Microsoft's infrastructure and our firm's Anthropic API key** — no third-party data sharing.
