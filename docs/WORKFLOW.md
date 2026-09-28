# Computational communication literature workflow

## Bilingual entry (0.4.0)

`comm_search_bilingual` accepts a research question, one explicit Chinese keyword/concept per call and 1–6 explicit English query variants. The calling assistant prepares and discloses those queries; the MCP does not call a hidden translation service. For example:

```json
{"query":"2020年以来算法推荐与政治极化", "chinese_query":"算法推荐", "english_queries":["algorithmic recommendation political polarization", "news feed affective polarization"], "year_start":2020, "max_results":20}
```

CNKI supplies the Chinese lane. Each request uses one keyword/concept, such as 算法推荐. Search 政治极化 in a separate call with languages=["zh"]. Whitespace-joined keywords and Boolean combinations are rejected before network access. Chinese candidates are ranked against the complete research question in query, not just the retrieval keyword. Existing English providers supply the English lane. Each lane is ranked independently with communication priority; equal language ranks alternate Chinese then English. If a lane is empty or unavailable, the other can fill the requested count. The returned count can still fall short. Joint rank is an ordering rule, not a comparable bilingual relevance score or a quality assessment. Full query/source diagnostics and within-language ranking reasons remain attached.

There is **no cross-language deduplication**, including in mixed reranking, download batches and saved selections. English multi-provider deduplication and duplicate checking against the Zotero library remain enabled. When reranking a mixed list directly, supply keywords for both languages; otherwise save the reviewed bilingual search order without reranking.

Chinese navigation starts at https://www.cnki.net/, follows the homepage form, and submits the search-page form if navigation only prefills its keyword. Relevance sorting is selected explicitly. The original Chinese search/parsing/download-button implementations are integrated in the same Python package and called directly within one service. No secondary MCP server, original CNKI Python environment, or `[mcp_servers.cnki]` configuration is used. `COMM_CNKI_BROWSER_PATH` directly selects the existing browser executable. Browser sessions and cookies remain in the service data directory. Search and download share one browser event loop; hidden/transparent/offscreen challenge templates do not count as a user-facing CAPTCHA. Blank verification pages are reported separately. Complete visible verification in the open browser before retrying in the same MCP process.

Chinese journal search retains the existing communication-journal whitelist and a bounded five-page extraction limit. Chinese theses bypass the journal whitelist. It does not provide exhaustive CNKI retrieval or guaranteed 20-paper Chinese recall. The ranking configuration includes Chinese phrase variants and communication venue names; exact venue aliases and lexical coverage remain limitations.

Both languages use the same selection/status/import/retry tools. Chinese bibliographic data retains `language=zh`, Chinese author names and thesis type when applicable; it is not replaced with Crossref metadata. CNKI downloads use the user's existing institutional access. File responses are retained under UUID filenames before browser download handling; PDF identity is checked against DOI or the compact Chinese title on the first page. CAJ and unverified files are retained and reported; they are not attached as verified PDFs. Blob/desktop-client download flows are unsupported. English OA/Sci-Hub policy does not apply to CNKI records.

The integrated browser module avoids upstream download/import cleanup, disables automatic browser installation/update, retains browser artifact directories and license-denial signals, and leaves license enforcement intact. Session directories, receipts and PDFs remain in `COMM_MCP_DATA_DIR/cnki` or the configured data directory; installation archives exclude them.

## Review, select, import

1. `comm_search` searches a single English query. `comm_search_many` searches up to six explicit complementary queries sequentially, retaining query/source diagnostics and deduplicating the combined results. This is candidate discovery, not an exhaustive systematic review.
2. Inspect the results and ranking evidence. Defaults remain relevance 65%, discipline 25%, recency 5%, citations 5%. Communication gets the highest discipline prior; computational social science, computing and multidisciplinary venues remain eligible. Venue classification is reported separately from the strongest topic/discipline signal.
3. `comm_save_selection(papers, query, title)` freezes the supplied order. Each record may include `selection_reason` and an existing `zotero_item_key`; a verified download receipt can be supplied instead of a paper. The same DOI within one language is selected once; Chinese and English entries remain separate. Distinct DOIs remain separate, including possible preprint/published versions that require review.
4. `comm_zotero_probe()` checks availability without displaying an authorization prompt. Obtain explicit user approval before `comm_zotero_authorize(user_approved=true)`.
5. `comm_import_to_zotero(manifest_id, collection_keys, tags, download_pdfs, limit, transport, use_scihub)` imports at most `limit` unfinished entries (1–10). Repeat with the same options for another batch. No collection means the existing personal library root; the tool does not create or guess a collection.
6. `comm_status(manifest_id)` distinguishes metadata verified, attachment verified, pending and needs retry. `comm_retry(manifest_id, limit)` retries only failed or interrupted entries, using the saved options. It never starts an unattempted entry.

The default path uses Zotero Desktop 10's local API. An explicitly selected `web` transport reuses the named Zotero MCP's configured library credentials; it has not been validated against this user's cloud library. Transport and target collections freeze on the first import. Use a new selection to change them.

## Zotero authorization

Zotero 10 local write keys are separate from zotero.org API keys. The native dialog offers Allow, Always Allow, and Deny. This implementation requires a reusable Always Allow grant for multi-step ingestion. A one-time grant is not saved or silently re-requested. Authorization is never triggered by an import or a retry.

Always Allow grants access to all locally editable Zotero libraries, not just one selected collection. This implementation limits each manifest to its recorded target. Revoke the permission in Zotero Settings → Advanced → Clear Write Authorizations. A revoked key or changed server instance stops the operation.

Remembered keys are encrypted with Windows DPAPI for the Windows account running the MCP; they are kept under the configured literature data directory's `authorization` folder. Plaintext keys never enter manifests or tool responses. Running the MCP under another Windows account may require a separate grant. The optional `COMM_ZOTERO_LOCAL_KEY` environment variable is intended for managed noninteractive environments.

References: [Zotero local API](https://www.zotero.org/support/dev/web_api/v3/local_api), [write requests](https://www.zotero.org/support/dev/web_api/v3/write_requests), [attachment uploads](https://www.zotero.org/support/dev/web_api/v3/file_upload).

## Deduplication and verification

- Existing items are matched by exact normalized DOI. Without a DOI, the title, first author's full-name tokens and publication year must match. Ambiguous results or incomplete identity evidence require review.
- Existing bibliographic fields are retained. Specified collections and tags are added; pass `tags=[]` for no added parent-item tags. New items are enriched from Crossref when possible and require authors, year and venue.
- Before creating an item, a deterministic key is saved. Retries reconcile that key and the library before creating anything, including after a timeout that occurred after a successful write.
- Zotero 10.0.3 has a confirmed local-API bug when creating objects with client-supplied keys (`primaryData` is not initialized). Only that specific error activates server-assigned keys, with a stable `dc:relation` reservation marker and write token. Subsequent attempts reconcile the marker before creating an object; successful responses checkpoint the assigned attachment key. New explicit keys use version 0 to require that the object does not already exist.
- PDFs retain the existing first-page DOI/title check. Attachments are deduplicated by file hash and parent item. Different downloaded bytes with identical extracted text can remain as separate PDF copies; this was observed in the integrated CNKI live test. A conflicting existing attachment is not replaced. Local completion additionally reads the file location returned by Zotero and checks the actual stored bytes against the downloaded PDF. Web transport reports server metadata verification only.
- Bibliography and attachment checkpoints are separate. A PDF failure does not undo a successful bibliographic import. The workflow does not update the full-library semantic index.
- Selection revisions, PDFs, receipts, lock files and test artifacts are retained. There is no automatic file deletion. A partial last JSON write can be recovered from an earlier snapshot; deterministic keys support subsequent reconciliation.

## Ranking boundaries

Term variants cover forms such as algorithm/algorithmic, recommendation/recommender and polarization/polarisation. For political-polarization queries, records with an abstract but no political/affective/ideological/partisan/electoral context receive lower relevance and a review flag; records with missing abstracts remain uncertain. This is an inspectable lexical heuristic, not an inference about a study's methods or results.

Complementary query phrases improve recall, but API source limits, missing abstracts, service errors and exact venue aliases still affect coverage. Journal preferences are an editable starting pool, not a finalized user-approved journal list. Selected papers are not reranked during import. DOI-version relationships and a gold-standard recall evaluation remain future work.

## Validation without removing artifacts

Set `PYTHONDONTWRITEBYTECODE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` and `COMM_TEST_ARTIFACTS` to a writable retained-artifact directory. Then run:

```text
python -m pytest -p no:tmpdir -p no:cacheprovider -p tests.retained_tmp tests/test_comm_ingestion.py tests/test_comm_workflow.py tests/test_stabilization_regressions.py tests/test_comm_bilingual.py tests/test_cnki_homepage.py tests/test_unified_cnki_runtime.py -q
```

The tests cover ranked retrieval, PDF validation, interrupted writes, repeated 20-paper ingestion, selective retries, collection/target consistency, DOI-less identity checks, the upload protocol, credential isolation and Windows encryption. These are simulated library writes; they do not establish that a real Zotero import has succeeded. A fresh stdio session validates tool registration separately. Actual library ingestion requires the user's native Zotero authorization and subsequent readback.

