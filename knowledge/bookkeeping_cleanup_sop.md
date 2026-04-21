# Bookkeeping Cleanup SOP — Canadian Small Business on QuickBooks Online

**Purpose:** Structured sequence the **Cleanup Coach** agent walks a bookkeeper through before the audit pipeline runs. The audit pipeline assumes the books are clean and the period is closed — this SOP gets them there.

**Shape:** 8 macro-steps. Each step is a checklist the bookkeeper confirms with `done` / `next`. Steps with Canadian-specific compliance implications (CRA, RQ) are non-skippable.

**Sources synthesized (April 2026):**
- Karbon — 5-step cleanup framework (gather → reconcile → review A/R·A/P·payroll·tax → COA cleanup → reports)
- Sal Accounting (Toronto CPA firm) — Canadian-specific compliance items
- QuickBooks Intuit — official 12-step QBO cleanup
- Max Pro Financials — cadence (daily/weekly/monthly/quarterly/annual)

Overlaid with two in-house rules caught in real client work:
- **Interac e-Transfer documentation** (shareholder advances vs. sales)
- **COGS ↔ closing inventory** (service vs. inventory businesses)

---

## Step 1 — Client context

Confirm client facts before any bookkeeping work. These drive every downstream decision (which tax accounts apply, which returns are due, which deductions are available).

- [ ] **Legal entity & BN** — legal name, Business Number, CRA program accounts (RT for GST/HST, RP for payroll, RC for corp tax)
- [ ] **Fiscal year-end** — date; confirm this matches QBO's fiscal-year setting
- [ ] **Provinces of operation** — drives sales-tax rate structure (GST+QST, HST, PST)
- [ ] **Sales tax registrations** — GST/HST (federal), QST (Revenu Québec), PST (BC/MB/SK); filing frequency for each
- [ ] **Payroll program** — is the client an employer? Has a PD7A account? Pays dividends instead?
- [ ] **Industry** — service, retail, construction, professional, etc. Determines whether inventory is expected, whether PST applies, whether CCA classes are unusual
- [ ] **Prior-year status** — was the prior year filed? T2/T1? Are opening balances confirmed?

## Step 2 — Documents from the client

Core "cleanup pack" the bookkeeper requests from the client. Nothing downstream works without these.

- [ ] Bank statements for **every** active bank account, **full period**
- [ ] Credit-card statements for **every** active card, full period
- [ ] Loan/line-of-credit statements (if any)
- [ ] Expense receipts — digital copies organized by month, or shoebox (categorize during cleanup)
- [ ] Vendor invoices / bills
- [ ] Customer invoices issued
- [ ] Payroll source documents (if applicable) — pay stubs, T4s if mid-year
- [ ] Prior-year returns and filed GST/HST/QST returns
- [ ] CRA / Revenu Québec correspondence (assessments, notices, penalty letters)
- [ ] Material contracts (landlord, key vendors, major customers)

## Step 3 — Bank & credit-card reconciliation

Every bank and CC account reconciled to the statement for every month in the period. Uncleared timing differences explicitly identified and dated. **CRA relies on bank-reconciled books for audit defence.**

- [ ] Every bank account shows a green ✓ reconciliation status in QBO for every month
- [ ] Every credit-card account reconciled
- [ ] Timing differences (uncleared cheques, deposits in transit) listed with dates
- [ ] No transactions in "Undeposited Funds" older than 30 days
- [ ] Bank feeds connected and current

## Step 4 — Uncategorized → categorized

All transactions posted to a real GL account. `Uncategorized Income`, `Uncategorized Expense`, and `Ask My Accountant` should be **empty** at cleanup end.

- [ ] Zero balance in Uncategorized Income
- [ ] Zero balance in Uncategorized Expense
- [ ] Zero balance in "Ask My Accountant"
- [ ] No transactions in "Opening Balance Equity" other than deliberate opening entries
- [ ] **Interac e-Transfer classification**: every Interac deposit has either a sales receipt (for revenue) or a written shareholder statement (for loan) attached in QBO or filed in the working papers. Missing documentation here is the #1 cause of CRA reassessment on small files.

## Step 5 — Chart of accounts cleanup

Duplicates merged, unused accounts archived, consistent naming. The chart shouldn't carry legacy accounts from prior bookkeepers that no one uses anymore.

- [ ] Duplicate accounts merged
- [ ] Unused accounts archived (not deleted — preserves history)
- [ ] Account names follow a consistent convention (e.g. "Telephone & Internet" not "phone")
- [ ] Parent/sub-account hierarchy makes sense (e.g. Vehicle Expense:Gas, Vehicle Expense:Insurance)
- [ ] Tax-liability accounts match filings — typically one consolidated "GST/HST Payable" (QBO handles GST/QST split via tax codes in the Tax Center, **not** via separate GL accounts)

## Step 6 — Sales-tax compliance

CRA (GST/HST) and Revenu Québec (QST) are the highest-risk areas for small files.

- [ ] **Tax Center verified**: registered for the right taxes, correct filing frequency, Quebec businesses have both GST and QST active
- [ ] Tax codes applied correctly on purchases and sales (Quebec purchases use the combined GST+QST code, not GST-only)
- [ ] ITC (Input Tax Credits) claimed on eligible expenses
- [ ] Meals & entertainment ITC at 50% (CRA rule)
- [ ] Prior GST/HST and QST returns filed; any "Suspense" balance traced to a filed return
- [ ] Net tax on the return reconciles to the BS GST/HST Payable balance

## Step 7 — A/R, A/P, payroll review

- [ ] **A/R**: open invoices aged (30/60/90/+). Stale receivables >90 days flagged for write-off or collection
- [ ] Customer deposits held separately from revenue if service not yet delivered
- [ ] **A/P**: open bills current, vendor credits applied
- [ ] Subcontractors paid >$500/yr identified for T4A at year-end
- [ ] **Payroll (if applicable)**: CPP (5.95%), EI (1.63% employee / 2.28% employer), federal + provincial tax withheld correctly
- [ ] Monthly PD7A remittances filed on time
- [ ] T4 amounts for each employee will tie to payroll register at year-end (Feb 28 deadline)

## Step 8 — Year-end closing adjustments

These are the entries that turn "in-progress" books into "ready for CPA review".

- [ ] **Inventory / COGS** — if the business carries inventory: physical closing count on period-end date, Inventory asset adjusted to actual, COGS trued up. (Service businesses with no inventory: explicitly document this is correct.)
- [ ] **Shareholder loans** — cleaned up; personal-use transactions reclassed; opening balance agrees with last filed T2
- [ ] **Dividends** — if declared, booked as DR Retained Earnings / CR Dividends Payable, T5 slip issued in the calendar year paid
- [ ] **Accruals** — accrued salaries, accrued professional fees, accrued interest on loans
- [ ] **Prepaid expenses** — identified and amortized (insurance, subscriptions, annual fees)
- [ ] **Deferred revenue** — customer prepayments for services not yet delivered moved out of Sales
- [ ] **CCA (depreciation)** — fixed-asset additions assigned CCA classes, depreciation booked per CRA tables
- [ ] **Retained-earnings rollforward** — prior RE + current profit − dividends = current RE (must tie)

## Gate — ready for audit

Cleanup is complete when:

1. Every step above is `done` (or explicitly not applicable, documented why)
2. BS balances — Total Assets = Total Liabilities + Equity
3. P&L profit equals BS "Profit for the year"
4. All prior-period GST/HST/QST returns filed
5. The period is locked in QBO ("Close the books" setting with a password)

Export Journal (CSV), Balance Sheet (PDF/XLSX), Profit & Loss (PDF/XLSX) for the period, then type `new audit <period>` and drop them in the DM.

---

## Notes for the Cleanup Coach agent

- **Ask, don't accuse.** Bookkeeping files are messy by nature. Frame every prompt as "please confirm" not "this is wrong".
- **Skippable steps.** If the client is a service business with no inventory, Step 8's inventory item is N/A — record that, don't block on it.
- **Non-skippable items** (will refuse to advance to audit mode without resolution):
  - Step 3: every bank account reconciled for every month
  - Step 4: zero balance in Uncategorized Income/Expense and Ask My Accountant
  - Step 6: prior sales-tax returns filed (or explicit deferral with reason)
  - Gate: BS balances and profit ties
- **Industry-specific additions** (future "Company Intelligence" tool will layer these on):
  - Construction: holdback receivables, WIP, subcontractor liability ins.
  - Restaurant: tip pooling, liquor licence fees, food inventory monthly
  - Professional services: WIP vs. billed, trust accounts if lawyer/realtor
  - E-commerce: inventory across FBA/Shopify, multi-jurisdiction sales tax
