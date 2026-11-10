# Veeva HTML format fix — update the existing repository

Your Veeva 2024, 2025 and 2026 downloads are reformatted investor-relations HTML reports. They have concept-labelled financial tables but no original Inline XBRL fact tags. The original importer rejected them. This update adds a conservative adapter for this supported table format, while retaining original Inline XBRL support.

## What to upload

Upload all FIVE files from underwriteiq_veeva_fix.zip into the main page of the existing UnderwriteIQ GitHub repository:

- app.py — replace existing; updated format notice, detected source format, and reviewed corrections.
- edgar_import.py — replace existing; rendered-table adapter and source provenance.
- test_edgar_import.py — replace existing; new regression tests.
- README.md — replace existing; updated format documentation.
- GITHUB_UPDATE_INSTRUCTIONS.md — replace existing; this guide.

requirements.txt, the Python 3.12 setting and your hidden folders do not need changing. Keep all your other files and tests.

## Exact steps

1. Download the fix ZIP and double-click it to extract it.
2. Go to https://github.com/hshussain-gif/UnderwriteIQ and remain on the main branch.
3. Select Add file -> Upload files.
4. Drag the FIVE extracted files into GitHub, alongside app.py. Upload the contents, not the enclosing folder or ZIP.
5. Commit directly to main with the message Support Veeva rendered 10-K HTML.
6. Wait for Streamlit to update, then refresh the app. If the older version persists, use Manage app -> Reboot app.
7. Select Upload EDGAR 10-K files and upload the SAME three Veeva HTML files again. No redownload is needed.

The revised importer was checked against those three supplied files and correctly selected fiscal years ended January 31, 2024, 2025 and 2026. It identifies the rendered format and keeps segment revenue separate from company-wide totals.

## Important review step

This fixes the file-format rejection. Some debt, cash-interest, inventory or cash-flow inputs can still be blank because these rendered primary tables do not report every required input. Review the source before entering values, including true zeros. A missing tag does not establish zero. Do not enter zeros just to bypass review. Dependent rows recalculate after explicit manual corrections; the app shows the final reviewed matrix used by the engine. Footnote-level automation is not claimed for this rendered format.
