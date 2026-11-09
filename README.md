# CreditScope AI

### Commercial Credit Underwriting & Financial Risk Intelligence

**Custom financial logic. Explainable credit decisions. ZERO API costs.**

CreditScope AI converts three years of manufacturing financial statements into an underwriting assessment: financial ratios, loan repayment schedules, stress scenarios, a transparent risk rating, and a committee-ready credit memorandum. The interactive **Challenge the Analyst** module answers questions about downside resilience, borrowing capacity, and loan structure using local mathematics.

Every calculation and narrative comes from a custom Python financial engine. No LLM, paid API key, market-data subscription, external financial-data feed, or database is required. The “AI” name reflects expert-system behavior: explicit rules select narratives and recommendations from financial evidence.

> **Portfolio focus:** commercial banking judgment translated into auditable software, with reproducible calculations, documented assumptions, and direct links between financial inputs and credit conclusions.

## Features

| Module | What it demonstrates |
| --- | --- |
| Financial statement analyzer | Editable three-year income statement, balance sheet and cash-flow matrix; financial ratios and reconciliation checks |
| Commercial loan simulator | Monthly/quarterly amortization, I/O with unchanged maturity, and average-draw revolvers with undrawn fees |
| Stress testing | Revenue haircut, EBITDA margin compression and rate shocks; pro forma income statement and scenario comparison |
| Explainable risk assessment | Five-grade deterministic matrix with explicit thresholds, endpoint conventions and weaker-factor grading |
| Credit memorandum | Conditional support, restructuring, decline or deferral narratives across four required sections; Markdown download |
| Challenge the Analyst | A 25% revenue decline test, constrained debt-capacity solver, grace-period analysis and cash-sweep illustration |

## Corporate finance concepts

| Concept | Formula or implementation |
| --- | --- |
| Current ratio | Current assets / current liabilities |
| Quick ratio | (Cash + short-term investments + receivables) / current liabilities |
| Gross margin | (Revenue − COGS) / revenue |
| EBITDA | Revenue − COGS − operating expenses excluding D&A |
| EBITDA margin / net margin | EBITDA / revenue; net income / revenue |
| Gross leverage | Interest-bearing debt / EBITDA; funded scenarios include the proposed loan |
| Debt-to-equity | Interest-bearing debt / book shareholders' equity |
| DSCR | EBITDA / annual cash principal and interest payments |
| Time value of money | Payment = P × r / [1 − (1 + r)^(-n)]; payment = P / n at zero interest |
| Stress sensitivity | Stressed revenue = revenue × (1 − haircut); stressed margin = historical margin − bps / 10,000 |
| Debt capacity | Maximum new principal subject to ≥1.25x DSCR and ≤4.50x gross leverage |
| Loan structuring | Temporary interest-only relief versus later amortization burden; sweeps limited to estimated surplus cash |

The capacity engine uses annuity present value to bracket the answer, then integer-cent bisection against the rounded annual payment schedules across every loan year. It distinguishes maximum new facility size from additional headroom beyond the current proposal.

## Data architecture

```text
Default three-year statements + user edits
                    |
              Input validation
                    |
          Derived financial totals
                    |
     Loan terms -> Decimal amortization schedule
                    |
       Base and custom stress calculations
                    |
         Ratios -> deterministic risk matrix
                    |
      Capacity solver + structural analysis
                    |
       Credit memo + committee responses
                    |
           CSV / Markdown downloads
```

`app.py` contains the canonical DataFrame loader/validator, an immutable Facility specification, shared financial/scenario engines, and the Streamlit presentation layer. Future CSV/Excel loaders must return the same Statement / Line item / Year 1 / Year 2 / Year 3 schema; no engine depends on row order. Pandas holds editable statement matrices and output tables. Python `Decimal` handles cent-rounded loan cash flows. NumPy supports the numerical stack. Plotly renders all charts using one explicit terminal palette. NumPy Financial remains available for independent cross-checks; the engine uses native Decimal amortization formulas.

Inputs live in the active Streamlit session and recalculate on widget changes. Financial edits are not automatically saved to disk or a database. There are no application calls to financial-data APIs or LLMs. Downloads let users retain results. Local execution calculates on the local machine; Community Cloud execution calculates on its hosted Python server.

## Repository structure

```text
creditscope-ai/
├── app.py
├── requirements.txt
├── README.md
├── .gitignore
├── .streamlit/
│   └── config.toml
├── .github/
│   └── workflows/
│       └── tests.yml
├── test_financial_engine.py
├── test_committee_tools.py
├── test_streamlit_app.py
└── sample_credit_memo.md
```

## Run locally

Use **Python 3.12** to match the deployment target. From the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. Dependency installation requires internet access once. No environment variables or secrets are needed.

The defaults model a manufacturer growing from $40M to $50M revenue at an 18% EBITDA margin. Enter full USD amounts. Expenses, capex, principal repayments and dividends are positive outflows. CFO is after existing interest and taxes; COGS and operating expenses exclude separately entered D&A.

## Deploy free on Streamlit Community Cloud

1. Create a public GitHub repository named `creditscope-ai`. Upload the structure above, preserving the dot-directories. Exclude `.venv`, secrets and generated ZIP archives.
2. Sign in to [Streamlit Community Cloud](https://share.streamlit.io/) with GitHub and choose **Create app**.
3. Select your repository, the branch containing the code (for example, `main`), and **`app.py`** as the main file path.
4. In **Advanced settings**, explicitly select **Python 3.12**. Leave Secrets empty.
5. Deploy. Verify the app, then add its assigned `streamlit.app` URL to the GitHub repository's About section.

The root `requirements.txt` declares dependencies and `.streamlit/config.toml` sets the dark terminal green/blue theme. No Dockerfile or system-package file is needed. Python is selected in the deployment settings, not in `requirements.txt`. GitHub stores the code; Community Cloud hosts the running Python application.

[Deployment instructions](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy) · [File organization](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/file-organization)

## Validation

```bash
python -m pip check
python -m unittest -v
```

The 11 tests cover financial reconciliations, amortization, stress propagation, rating boundaries, debt-capacity constraints, recommendation branches, grace-period burdens, sweep availability and live Streamlit interactions. GitHub Actions is configured to run these checks on Python 3.12/Linux after pushes and pull requests. The configured cloud workflow runs after the repository is uploaded; it is not evidence of a completed cloud deployment.

## Underwriting assumptions

- Proposed debt is incremental. Pro forma DSCR adds first-year facility service to Year 3 existing cash debt service. Revolver service is drawn interest plus undrawn fees; only drawn exposure enters leverage, with maturity principal repayment excluded and disclosed. I/O amortizes over the remaining term and must align to payment intervals.
- Rate shocks re-underwrite the new loan at inception; existing obligations remain fixed.
- Revenue haircuts preserve the selected margin after compression. The 10%/20% cash-flow presets reduce EBITDA with unchanged base debt service.
- Ratings use the worse DSCR/leverage grade. The matrix documents endpoints; nonpositive EBITDA and undefined metrics receive conservative grade 5.
- Committee limits of 1.25x DSCR and 4.50x leverage, and the memo's 1.15x critical trigger, are explicit project policies. The sidebar DSCR target is a separate illustrative comparison.
- Negative estimated cash after capex, principal and dividends prompts restructuring even when EBITDA coverage passes. The cash proxy holds existing taxes and working-capital movements fixed.
- The model does not forecast a fully integrated balance sheet or determine collateral realizations, management quality or legal enforceability.

## Cost model

**Financial-data API costs: $0. LLM/token costs: $0. Required subscriptions: none.**

Free Community Cloud hosting is available subject to its resource limits and policies, separately from the engine's zero-API-cost design. The local configuration disables usage telemetry; Community Cloud may override that setting under its platform configuration.

[Community Cloud overview](https://docs.streamlit.io/deploy/streamlit-community-cloud) · [Limits and configuration overrides](https://docs.streamlit.io/deploy/streamlit-community-cloud/status)

## References

- [CFA Institute financial ratios](https://www.cfainstitute.org/sites/default/files/-/media/documents/support/programs/cfa/cfa_program_level_ii_financial_ratio_list.pdf)
- [BDC: EBITDA-based DSCR](https://www.bdc.ca/en/articles-tools/entrepreneur-toolkit/templates-business-guides/glossary/debt-service-coverage-ratio)
- [OCC: repayment capacity and restructuring](https://www.occ.treas.gov/news-issuances/bulletins/2024/bulletin-2024-29.html)

The project grading policy is illustrative, not a universal bank standard or calibrated default probability.
