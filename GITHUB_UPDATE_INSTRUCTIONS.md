# Update UnderwriteIQ on GitHub — no Terminal required

## The six files in this update

Upload all six files from the extracted update ZIP into the MAIN PAGE of your existing repository, alongside app.py:

1. app.py — REPLACE the existing file.
2. requirements.txt — REPLACE the existing file; adds lxml for local HTML parsing.
3. README.md — REPLACE the existing project description and instructions.
4. edgar_import.py — ADD this new file. The revised app imports it, so it is required.
5. test_edgar_import.py — ADD this new automated test file.
6. GITHUB_UPDATE_INSTRUCTIONS.md — ADD this guide for future reference.

The three runtime-critical files are app.py, requirements.txt and edgar_import.py. Uploading all six is recommended. Keep your existing tests, .streamlit/config.toml, .github/workflows/tests.yml, .gitignore and other project files. No hidden files need changing for this update.

## Exact upload steps

1. Download underwriteiq_edgar_update.zip and double-click it on your Mac to extract it.
2. Open your GitHub repository: https://github.com/hshussain-gif/UnderwriteIQ
3. Make sure you are on the main branch and the repository's main page (the page listing app.py).
4. Click Add file -> Upload files.
5. Open the extracted update folder in Finder. Drag its SIX FILES into GitHub's upload area. Drag the contents, not the ZIP or enclosing folder. Their names must remain unchanged.
6. Enter the commit message: Add reviewed EDGAR 10-K uploads.
7. Choose Commit directly to the main branch and click Commit changes.
8. Confirm app.py, requirements.txt AND edgar_import.py all appear together at the repository's top level.
9. Open Streamlit. The GitHub update should trigger a rebuild because requirements.txt changed. Allow the new library to install.
10. Keep main file path app.py and Python 3.12. If your app already runs Python 3.12, you do not need to delete it or redeploy. If the update does not appear after the rebuild, use Manage app -> Reboot app.
11. Open the Actions tab on GitHub to see the automated checks.

## Using the new upload feature

1. Open UnderwriteIQ and select Upload EDGAR 10-K files.
2. Upload ORIGINAL SEC EDGAR .htm or .html annual filings with embedded Inline XBRL. Start with the latest two 10-K filings for the same USD-reporting US-GAAP non-financial company. Add older filings if needed (maximum four files).
3. Do NOT upload PDFs, screenshots, spreadsheets or a saved EDGAR viewer/search page. Renaming a PDF does not convert it to Inline XBRL.
4. Review the detected company and three full fiscal-year periods.
5. Review the extracted financial matrix and source evidence. Complete missing values manually from the filing. A blank does not mean zero. Never enter zero just to bypass a missing fact.
6. Check reported costs and operating D&A, all short- and long-term debt repayments, cash interest and other financing flows. EBITDA adds total operating D&A back once; no estimate of its COGS allocation is needed.
7. Review reconciliation differences and tick the review confirmation to run the loan/stress/risk/memo engines.
8. Download the audit evidence if you want a record of extracted and manually overridden values.

The required file notice and download instructions also appear inside the application. Correct HTML format enables extraction but cannot guarantee that every company's tags supply all inputs automatically. Custom tags and unusual accounting classifications may need manual review. No API key or secret is required. Processed filings are not uploaded to GitHub.
