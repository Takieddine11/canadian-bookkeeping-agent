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
# QBO's French PDF exports use "Total pour X" for every subtotal including
# the grand totals (e.g. "Total pour Actifs", not "Total des actifs"). So
# we accept both the standard French accounting terminology and QBO's
# "Total pour …" convention.
TOTAL_ASSETS = (
    "Total Assets",
    "Total de l'actif", "Total de l'Actif",
    "Total des actifs",
    "Total pour Actifs",
)
TOTAL_LIABILITIES = (
    "Total Liabilities",
    "Total du passif", "Total du Passif",
    "Total des passifs",
    "Total pour Passifs",
)
TOTAL_EQUITY = (
    "Total Equity",
    "Total des capitaux propres", "Total de l'avoir",
    "Total des capitaux",
    "Total pour Capitaux propres",
)
TOTAL_LIABILITIES_AND_EQUITY = (
    "Total Liabilities and Equity",
    "Total du passif et des capitaux propres",
    "Total du passif et capitaux propres",
    "Total du passif et avoir",
    "Total pour Passifs et capitaux propres",
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
    "Revenu net",            # QBO FR PDF export uses this on the BS equity section
    "Résultat net de l'exercice",
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
    "Total pour Revenu", "Total pour Revenus",
    "Total pour Produits",
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
    "Total pour Charges",
    "Total pour Dépenses", "Total pour Depenses",
    "Total pour Coût des produits vendus",
    "Total pour Cout des produits vendus",
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
    # QBO FR exports wrap GST/HST + QST under a single "Sales Tax Payable"
    # parent; we also accept the section total as the effective payable.
    "Total pour Sales Tax Payable",
    "Total pour Taxes de vente à payer",
    "Total pour Taxes de vente a payer",
)
GST_HST_SUSPENSE = (
    "GST/HST Suspense",
    "TPS/TVH en suspens",
    "Taxes en suspens",
    "Compte d'attente TPS/TVH",
)

# ---- Cash / bank section ----
TOTAL_CASH = (
    "Total Cash and Cash Equivalent",
    "Total Cash and Cash Equivalents",
    "Total de l'encaisse et des équivalents",
    "Total de l'encaisse",
    "Total trésorerie", "Total tresorerie",
    "Total de la trésorerie et équivalents",
)
