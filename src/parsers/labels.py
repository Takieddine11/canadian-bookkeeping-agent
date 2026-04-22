"""Bilingual (English / French) label synonyms for QBO financial statements.

Canadian QBO files are commonly exported in either language, and a Quebec
bookkeeper may also have a file in both within the same engagement (e.g. BS
exported in English, P&L in French). Each constant here is a tuple of
acceptable labels — agents use it via ``FinancialStatement.amount_of_any(*X)``
so lookups work regardless of the language of the underlying export.

Add synonyms here as new QBO variants show up. The tuples are intentionally
small (2–5 entries) — full localization isn't the goal, just covering what
QBO's own French templates use.
"""

from __future__ import annotations

# ---- Balance Sheet top-line totals ----
TOTAL_ASSETS = (
    "Total Assets",
    "Total de l'actif", "Total de l'Actif",
    "Total des actifs",
)
TOTAL_LIABILITIES = (
    "Total Liabilities",
    "Total du passif", "Total du Passif",
    "Total des passifs",
)
TOTAL_EQUITY = (
    "Total Equity",
    "Total des capitaux propres", "Total de l'avoir",
    "Total des capitaux",
)
TOTAL_LIABILITIES_AND_EQUITY = (
    "Total Liabilities and Equity",
    "Total du passif et des capitaux propres",
    "Total du passif et capitaux propres",
    "Total du passif et avoir",
)

# ---- Equity detail ----
RETAINED_EARNINGS = (
    "Retained Earnings",
    "Bénéfices non répartis", "Benefices non repartis",
    "Bénéfices non-répartis",
)
PROFIT_FOR_THE_YEAR = (
    "Profit for the year",
    "Bénéfice de l'exercice", "Benefice de l'exercice",
    "Bénéfice de l'année", "Benefice de l'annee",
    "Résultat de l'exercice",
)
DIVIDENDS = (
    "Dividends",
    "Dividendes",
)

# ---- P&L top-line totals ----
TOTAL_INCOME = (
    "Total Income",
    "Total des revenus", "Total des produits",
    "TOTAL DES REVENUS",
)
TOTAL_COGS = (
    "Total Cost of Goods Sold",
    "Total du coût des marchandises vendues",
    "Total du cout des marchandises vendues",
    "Total des coûts des ventes", "Total des couts des ventes",
)
GROSS_PROFIT = (
    "GROSS PROFIT", "Gross Profit",
    "BÉNÉFICE BRUT", "BENEFICE BRUT", "Bénéfice brut",
    "MARGE BRUTE", "Marge brute",
)
TOTAL_EXPENSES = (
    "Total Expenses",
    "Total des dépenses", "Total des depenses",
    "Total des charges",
)
NET_PROFIT = (
    "PROFIT", "Profit", "Net Income",
    "BÉNÉFICE NET", "BENEFICE NET", "Bénéfice net",
    "RÉSULTAT NET", "RESULTAT NET", "Résultat net",
    "BÉNÉFICE", "BENEFICE",
)

# ---- Sales tax accounts ----
GST_HST_PAYABLE = (
    "GST/HST Payable",
    "TPS/TVH à payer", "TPS/TVH a payer",
    "TPS à payer", "TPS a payer",
    "Taxes de vente à payer", "Taxes de vente a payer",
    "Sales Tax Payable",
)
