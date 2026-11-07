# doipdf

**DOI in, PDF out.** Resolve a DOI to an actual PDF file from open sources — no database, no
reference manager, no library, no Mendeley/Zotero coupling.

```python
from doipdf import fetch_pdf

result = fetch_pdf("10.1038/nature12373", outdir="pdfs")
if result.ok:
    print(result.path, result.source, result.size)
```

```bash
python doipdf.py 10.1038/nature12373
python doipdf.py -o pdfs --workers 12 --list dois.txt
```

---

## Two modes, because batches and one-offs are different problems

| | Single DOI | Batch (2+ DOIs) |
|---|---|---|
| Default mode | **full** — try every mirror | **quick** — best mirror only |
| Typical time | 1–5 min | ~6s per DOI, parallel |
| Use when | you need *this* paper | you need *most* of these papers |

The reasoning: a slow mirror can take 60–90 seconds to answer. That's acceptable when you're
waiting for one paper, and ruinous when it's multiplied across 400.

### Handling the leftovers

A quick batch tells you exactly what it couldn't find, then stops:

```
0/3 fetched in 18.9s (0.16/s, quick mode)

3 not found. Quick mode only checks the best mirror,
so some of these may exist elsewhere. To try every mirror:
  re-run with --retry-slow
```

Then decide: is it worth several minutes per paper to chase these?

```bash
# second pass on just the failures, every mirror, serial
python doipdf.py --retry-slow -o pdfs --list dois.txt

# or force a mode explicitly
python doipdf.py --full 10.1234/abc        # one DOI, be thorough
python doipdf.py --quick 10.1234/abc       # one DOI, just the fast check
```

Slow retry is **serial by default** — several 90-second mirrors at once makes a run look hung.

---

## Sources, tried in order

| Source | Coverage |
|---|---|
| **Unpaywall** | Official open-access copies. Cleanest, when present |
| **arXiv** | Direct when the DOI contains `arxiv` |
| **EuropePMC** | Biomedical, PMC mirrors |
| **Sci-Hub mirrors** | Last resort, widest coverage for paywalled papers |

Override mirrors with `DOIPDF_MIRRORS` (comma-separated), set a contact address with
`DOIPDF_MAIL` (Unpaywall requires one).

### Mirror status (Nov 2026)

| Mirror | Status |
|---|---|
| `sci-hub.box` | ⚠️ Intermittent — 403s and anti-bot pages come and go |
| `www.sci-hub.ren` | ⚠️ Works but takes 60–90s per article |
| `sci-hub.ru` | ⚠️ Often serves a verification page |
| `sci-hub.wf` | ⚠️ Often "Checking your browser" |
| `sci-hub.se` | ❌ Unreachable |
| `sci-hub.st` | ❌ 403 DDoS-Guard |

Mirror health changes weekly. If everything fails, check them by hand before assuming the tool
is broken — and see `references/troubleshooting.md`.

---

## Verification, not just HTTP 200

A 200 response proves nothing: paywalls, challenge pages, and login walls all return 200.

```python
# what counts as success
content[:5] == b"%PDF-"          # real PDF magic
len(content) >= 20000            # not a stub
pdftotext -l 3 → ≥200 chars      # not a cover-only scan
```

Typical impostors we've hit:

| Size | What it is | Cause |
|---|---|---|
| ~3 KB | block page | proxy in use |
| ~50–100 KB | challenge page | Cloudflare |
| ~800 KB | error page | no subscription |
| ~200 KB HTML | login wall | book chapter |

`pdftotext` is optional — without it the tool still checks the magic bytes, just less strictly.

---

## Repository layout

```
doipdf/
├── scripts/doipdf.py        ← the tool. Standalone, no dependencies beyond httpx.
├── zotero/                  ← optional: plug results into a Zotero library
│   ├── pdffill.py           ← scan a library, batch fill, attach back
│   ├── pdffill_fast.py      ← concurrent variant
│   ├── pdffill.bat          ← Windows entry
│   ├── README.md            ← 中文文档
│   └── references/          ← troubleshooting + field data
└── README.md                ← you are here
```

`doipdf` has no awareness of Zotero. The `zotero/` directory is a separate tool that reuses the
same fetching logic but adds library scanning and direct-DB attachment. Ignore it unless you
keep a Zotero library.

```bash
# pure DOI → PDF, no database involved
python scripts/doipdf.py 10.1038/nature12373 -o pdfs/

# optional: fill and attach inside a Zotero library
cd zotero && ./pdffill.bat scan && ./pdffill.bat fast && ./pdffill.bat attach
```

---

## What it deliberately does not do

- No database. Nothing to configure, nothing to migrate.
- No reference manager integration. It writes files; you decide where they go.
- No browser automation. Everything is plain HTTP.
- No credential handling.

If a source needs a browser (Cloudflare-protected publishers like Elsevier, ACS, Wiley), this
tool will not get it — those require a human solving a challenge in a real browser. For that
workflow, see the sibling project `zotero-pdffill`.

---

## Install

```bash
pip install httpx
# optional but recommended, for stricter verification:
#   apt install poppler-utils      /  brew install poppler
```

Python 3.10+. Single file, no other dependencies.

## As a library

```python
from doipdf import fetch_pdf, fetch_many, slow_retry

# one
r = fetch_pdf("10.1038/nature12373", outdir="pdfs")

# many, quick mode
results = fetch_many(dois, outdir="pdfs", workers=12, quick=True)
missing = [r.doi for r in results if not r.ok]

# chase the missing ones, serially
if missing:
    for r in slow_retry(missing, outdir="pdfs"):
        print(r.doi, r.ok, r.source)
```

`Result` has: `doi`, `ok`, `source`, `path`, `size`, `error`.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Some sources here operate in a legal grey area that varies by jurisdiction. Use this only for
content you are entitled to access.
