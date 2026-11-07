# zotero-pdffill

**Batch-fill missing PDFs in your Zotero library and attach them back automatically.**

Point it at a Zotero library, and it will find every item without a PDF, try multiple open-access
sources concurrently, verify each download is a real PDF, and write the attachments straight into
Zotero's database.

Built and battle-tested while filling ~500 missing papers in a real research library:
**426 missing → 240**, at ~3 papers/second once the pipeline was made concurrent.

---

## How it works

Two separate concerns, and it matters that they stay separate:

| Step | Needs Zotero closed? |
|---|---|
| Fetching PDFs | **No** — pure HTTP, writes to local `fetched/` |
| Attaching to Zotero | **Yes** — writes directly to `zotero.sqlite` |

The official `zotero-write-endpoint` plugin (port 23119) **cannot import local files** — it only
offers `add-by-identifier`, `create-items`, `item-update`, `delete-items`, `search`, `version`.
So attaching a PDF means writing to the database directly.

---

## Quick start

```bash
cd zotero-pdffill
./pdffill.bat scan                  # list items missing PDFs
./pdffill.bat fast --workers 14     # concurrent fetch (~3 papers/sec)
# quit Zotero from the File menu, not Task Manager
./pdffill.bat attach --backup       # write attachments + verify integrity
./pdffill.bat report                # coverage stats
```

On Windows, `pdffill.bat` is the entry point. On Linux/macOS, call the scripts directly:

```bash
export PDFFILL_ROOT="$PWD"
python scripts/pdffill.py scan
python scripts/pdffill_fast.py --workers 14
python scripts/pdffill.py attach --backup
```

Scope it to one collection:

```bash
./pdffill.bat scan --collection "Ecology Homework"
```

---

## Sources, tried in order

1. **Unpaywall** — cleanest option, official OA copies
2. **arXiv** — direct when the DOI contains `arxiv`
3. **EuropePMC** — biomedical PMC copies
4. **Sci-Hub mirrors** — the workhorse, ~65% hit rate in our runs

### Sci-Hub mirror status (Nov 2026)

| Mirror | Status |
|---|---|
| `sci-hub.box` | ✅ Best coverage — script tries this first |
| `sci-hub.ru` | ✅ Works |
| `www.sci-hub.ren` | ✅ Works |
| `sci-hub.wf` | ⚠️ Often serves "Checking your browser" |
| `scihub.help` | ⚠️ Mostly 404 |
| `sci-hub.se` | ❌ Unreachable |
| `sci-hub.st` | ❌ 403 DDoS-Guard |

If the page says the article is unavailable, **it genuinely isn't indexed** — cycling mirrors
won't help. That's typical for 2024+ papers, small journals, and book chapters.

---

## Publisher direct-link cheatsheet

| Publisher | Direct link | Notes |
|---|---|---|
| Springer/BMC | `link.springer.com/content/pdf/<DOI>.pdf` | **Do not use a proxy** — returns a 3 KB block page |
| MDPI | `mdpi-res.com/d_attachment/<journal>/<journal>-<vol:02d>-<art:05d>/article_deploy/<same>.pdf` | The `www` site is Akamai-blocked; the CDN is not |
| Wiley | `onlinelibrary.wiley.com/doi/pdfdirect/<DOI>` | Recent issues work; pre-2010 mostly 403 |
| ACS | `pubs.acs.org/doi/pdf/<DOI>` | Requires institutional login |
| PNAS | `www.pnas.org/doi/pdf/<DOI>` | |
| Nature | `www.nature.com/articles/<DOI minus 10.1038/>.pdf` | |
| Elsevier | No direct link | Must scrape the md5-tokenised `pdfft` URL from the article page |

### Free official channels

Government and intergovernmental bodies publish PDFs openly. **OECD, WHO, FAO, UNEP, EPA, EFSA,
ECHA** and learned societies all serve documents from a CDN that is **not** behind Cloudflare:

```
oecd.org/content/dam/oecd/en/publications/reports/<year>/<month>/<full-slug>_<hash>/<doi-suffix>.pdf
```

The path can't be constructed (year, month, slug, and hash are all needed), so open the landing
page in a browser once, grab the "Download PDF" href, then let the script fetch it. We got 5/5
this way on OECD guidance documents.

**Commercial book chapters (Elsevier/Springer/CRC) have no such route** — their official site
*is* the paywall.

---

## What attach actually writes

```
items          (itemTypeID=3, 8-char uppercase key, libraryID=1, version=0, synced=0)
itemAttachments(parentItemID, linkMode=0, contentType='application/pdf',
                path='storage:<filename>', syncState=0,
                storageModTime / storageHash may be NULL,
                lastProcessedModificationTime = file mtime)
itemData       (fieldID=1 = title; the value goes into itemDataValues, not a `values` table)
storage/<new-key>/<filename>
```

`attach` checks Zotero is closed, backs up the whole library, runs `PRAGMA wal_checkpoint(TRUNCATE)`,
writes each item in a transaction, then runs `integrity_check` + `foreign_key_check`.

---

## Gotchas (every one of these bit us)

1. **Force-killing Zotero rolls back the library.** We lost an entire night's imports (142 items)
   this way — the DB went from 178 MB to a 23 MB rebuild on next start. **Quit from File → Quit.**
   `attach` refuses to run if it detects Zotero still listening.

2. **Stale WAL blocks writes.** Even after a clean exit, `zotero.sqlite-wal` may linger and make
   the script think Zotero is still up. Always checkpoint first.

3. **The 20-second tool timeout.** PDF endpoints routinely take 15–25 s. Workaround: run the
   `fetch` inside a hidden iframe and return immediately, then collect the downloaded files.

4. **`dsh_browser_download` is a page-context fetch.** Cross-origin redirects
   (Elsevier → `pdf.sciencedirectassets.com`) fail. Navigate to the PDF URL first, *then*
   `fetch(location.href)`.

5. **Cloudflare fingerprints the browser.** Plain `httpx` gets 403 everywhere; institutional IP
   access only works inside a real browser, and each publisher domain needs its own challenge
   solved by a human.

6. **Proxies can hurt.** Springer and MDPI returned 3 KB block pages *with* a proxy and worked
   fine without one. The scripts use `trust_env=False` and no proxy.

7. **Direct DB writes don't build the full-text index.** PDFs open fine, but Zotero's full-text
   search won't find their contents until you rebuild the index.

---

## Documentation

| File | Contents |
|---|---|
| [`references/troubleshooting.md`](references/troubleshooting.md) | Symptom-indexed: 403s, challenge pages, WAL, broken attachments, timeouts |
| [`references/field-data.md`](references/field-data.md) | Hit rates by source, results by publisher, speed benchmarks |
| [`README.zh.md`](README.zh.md) | 中文完整说明 |

---

## Requirements

- Python 3.10+ with `httpx`
- `pdftotext` (Poppler) for PDF verification — optional but strongly recommended
- Zotero (developed against 10.0.7)

Zotero's database is auto-detected: `ZOTERO_DB_PATH` → `extensions.zotero.dataDir` in
`prefs.js` → `~/Zotero/zotero.sqlite`.

## Disclaimer

This tool can retrieve papers from sources whose legality varies by jurisdiction, and it writes
directly to Zotero's database. **Back up your library and use it only for content you are
entitled to access.** The authors take no responsibility for misuse.

## License

MIT — see [LICENSE](LICENSE).
