#!/usr/bin/env python3
"""doipdf — resolve a DOI to a PDF file.

Tries multiple open-access sources in order and returns the first verified PDF.
No database, no reference manager, no library required: DOI in, PDF out.

    from doipdf import fetch_pdf
    result = fetch_pdf("10.1016/j.envint.2013.08.023", outdir="pdfs")
    if result.ok:
        print(result.path, result.source, result.size)

Command line:

    python doipdf.py 10.1038/nature12373
    python doipdf.py --outdir pdfs 10.1038/nature12373 10.1126/science.abc1234
    python doipdf.py --list dois.txt --workers 12
    cat dois.txt | python doipdf.py --stdin
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict

import httpx

__version__ = "1.0.0"

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Sci-Hub mirrors, best first. Override with DOIPDF_MIRRORS (comma separated).
DEFAULT_MIRRORS = [
    "https://sci-hub.box",
    "https://www.sci-hub.ren",
    "https://sci-hub.ru",
    "https://sci-hub.wf",
]

# Politeness email for Unpaywall / Crossref. Unpaywall requires one.
MAIL = os.environ.get("DOIPDF_MAIL", "user@example.com")

PDF_META_RE = re.compile(r'citation_pdf_url"\s+content="([^"]+)"', re.I)
PDF_ANY_RE = re.compile(r'(?:data|href)\s*=\s*["\']([^"\']*\.pdf[^"\']*)["\']', re.I)
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

MIN_PDF_BYTES = 20000
MIN_TEXT_CHARS = 200


def mirrors() -> list[str]:
    env = os.environ.get("DOIPDF_MIRRORS")
    if env:
        return [m.strip().rstrip("/") for m in env.split(",") if m.strip()]
    return DEFAULT_MIRRORS


@dataclass
class Result:
    doi: str
    ok: bool
    source: str = ""
    path: str = ""
    size: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------- verification

def looks_like_pdf(path: pathlib.Path) -> bool:
    try:
        return path.open("rb").read(5) == b"%PDF-"
    except OSError:
        return False


def has_extractable_text(path: pathlib.Path, min_chars: int = MIN_TEXT_CHARS) -> bool:
    """Guard against cover-only scans and truncated downloads.

    If pdftotext is missing we accept the file: the %%PDF- magic is still checked.
    """
    try:
        out = subprocess.run(
            ["pdftotext", "-l", "3", str(path), "-"],
            capture_output=True, timeout=45,
        ).stdout
    except FileNotFoundError:
        return True
    except subprocess.TimeoutExpired:
        return False
    return len(out.decode("utf-8", "ignore").strip()) >= min_chars


def verified(path: pathlib.Path) -> bool:
    return looks_like_pdf(path) and has_extractable_text(path)


def _save(dest: pathlib.Path, content: bytes) -> bool:
    if len(content) < MIN_PDF_BYTES or content[:5] != b"%PDF-":
        return False
    dest.write_bytes(content)
    if verified(dest):
        return True
    dest.unlink(missing_ok=True)
    return False


# ----------------------------------------------------------------- sources

def from_unpaywall(c: httpx.Client, doi: str, dest: pathlib.Path) -> str:
    """Official open-access copies. Cleanest source, when it has one."""
    try:
        r = c.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": MAIL}, timeout=12)
        if r.status_code != 200:
            return ""
        for loc in r.json().get("oa_locations") or []:
            url = loc.get("url_for_pdf") or loc.get("url")
            if not url:
                continue
            try:
                if _save(dest, c.get(url, timeout=60).content):
                    return "unpaywall"
            except httpx.HTTPError:
                continue
    except (httpx.HTTPError, ValueError):
        pass
    return ""


def from_arxiv(c: httpx.Client, doi: str, dest: pathlib.Path) -> str:
    if "arxiv" not in doi.lower():
        return ""
    m = re.search(r"(\d{4}\.\d{4,5})", doi)
    if not m:
        return ""
    try:
        if _save(dest, c.get(f"https://arxiv.org/pdf/{m.group(1)}", timeout=60).content):
            return "arxiv"
    except httpx.HTTPError:
        pass
    return ""


def from_europepmc(c: httpx.Client, doi: str, dest: pathlib.Path) -> str:
    """Biomedical literature, PMC mirrors. Note the /webservices/rest path."""
    try:
        r = c.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f"DOI:{doi}", "format": "json", "resultType": "core"},
            timeout=12,
        )
        if r.status_code != 200:
            return ""
        for hit in r.json().get("resultList", {}).get("result", []):
            url = hit.get("pdfUrl")
            if not url and hit.get("pmcid"):
                url = f"https://europepmc.org/articles/{hit['pmcid']}?pdf=render"
            if not url:
                continue
            try:
                if _save(dest, c.get(url, timeout=60).content):
                    return "europepmc"
            except httpx.HTTPError:
                continue
    except (httpx.HTTPError, ValueError):
        pass
    return ""


def _clean_url(raw: str, base: str) -> str:
    """Turn whatever a landing page hands us into a usable absolute URL.

    Sci-Hub mirrors occasionally serve escaped or protocol-relative hrefs
    (``/\\/\\/sci.bban.top\\/pdf\\/...``). Those are junk or third-party
    redirectors, so unescape, reject anything that isn't http(s), and return
    an empty string when it can't be salvaged.
    """
    url = (raw or "").strip()
    if not url:
        return ""
    # Undo JSON-ish / JS string escaping: \/ -> /
    url = url.replace("\\/", "/").replace("\\\\", "\\").replace("\\", "")
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = base.rstrip("/") + url
    if not re.match(r"^https?://[^/\\]+", url, re.I):
        return ""
    return url


def _scihub_one(doi: str, base: str) -> tuple[str, bytes] | None:
    """Hit a single mirror. Returns (source_label, pdf_bytes) or None."""
    landing = f"{base}/{doi}"
    try:
        with httpx.Client(trust_env=False, follow_redirects=True,
                          headers=UA, timeout=httpx.Timeout(15.0, read=90.0)) as c:
            r = c.get(landing)
            if r.status_code != 200:
                return None
            m = PDF_META_RE.search(r.text) or PDF_ANY_RE.search(r.text)
            if not m:
                return None
            url = _clean_url(m.group(1), base)
            if not url:
                return None
            r2 = c.get(url, headers={**UA, "Referer": landing},
                       timeout=httpx.Timeout(15.0, read=120.0))
            if len(r2.content) >= MIN_PDF_BYTES and r2.content[:5] == b"%PDF-":
                return f"scihub:{base.split('//')[-1]}", r2.content
    except Exception:
        return None
    return None


def from_scihub(c: httpx.Client, doi: str, dest: pathlib.Path, quick: bool = False) -> str:
    """Walk Sci-Hub mirrors in order, with a tight per-mirror budget.

    Mirrors are individually flaky: some answer "not found" immediately, some
    hang, and some serve an anti-bot page. A short budget per mirror keeps a
    batch moving instead of blocking on a dead one.
    """
    candidates = mirrors()[:1] if quick else mirrors()
    for base in candidates:
        landing = f"{base}/{doi}"
        try:
            r = c.get(landing, timeout=httpx.Timeout(8.0, read=25.0))
            if r.status_code != 200:
                continue
            m = PDF_META_RE.search(r.text) or PDF_ANY_RE.search(r.text)
            if not m:
                continue
            url = _clean_url(m.group(1), base)
            if not url:
                continue
            r2 = c.get(url, headers={**UA, "Referer": landing},
                       timeout=httpx.Timeout(10.0, read=90.0))
            if _save(dest, r2.content):
                return f"scihub:{base.split('//')[-1]}"
        except Exception:
            # Dead mirror, TLS failure, malformed URL: try the next one.
            continue
    return ""


SOURCES = (from_unpaywall, from_arxiv, from_europepmc, from_scihub)


# ----------------------------------------------------------------- public API

def normalize_doi(raw: str) -> str:
    """Accept bare DOIs and URL forms (doi.org/, dx.doi.org/)."""
    s = (raw or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    return s.strip().rstrip(".,;")


def safe_filename(doi: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", doi) + ".pdf"


def fetch_pdf(
    doi: str,
    outdir: str | pathlib.Path = ".",
    *,
    quick: bool = False,
    timeout: float = 90.0,
) -> Result:
    """Resolve one DOI to a PDF on disk.

    Args:
        doi: bare DOI or doi.org URL.
        outdir: directory to write into (created if missing).
        quick: try only the best Sci-Hub mirror. Much faster over big batches;
            misses articles that only the secondary mirrors have.
        timeout: overall per-request timeout in seconds.

    Returns a Result; check ``.ok`` before using ``.path``.
    """
    doi = normalize_doi(doi)
    if not DOI_RE.match(doi):
        return Result(doi=doi, ok=False, error="not a valid DOI")

    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / safe_filename(doi)

    if dest.exists() and verified(dest):
        return Result(doi=doi, ok=True, source="cached",
                      path=str(dest), size=dest.stat().st_size)

    with httpx.Client(trust_env=False, follow_redirects=True,
                      headers=UA, timeout=timeout) as c:
        for fn in SOURCES:
            try:
                # from_scihub takes the quick flag; the others don't care.
                if fn is from_scihub:
                    source = fn(c, doi, dest, quick=quick)
                else:
                    source = fn(c, doi, dest)
            except Exception:  # one broken source must never abort the batch
                continue
            if source:
                return Result(doi=doi, ok=True, source=source,
                              path=str(dest), size=dest.stat().st_size)

    return Result(doi=doi, ok=False, error="no open source had this paper")


def fetch_many(
    dois,
    outdir: str | pathlib.Path = ".",
    *,
    workers: int = 8,
    quick: bool = True,
) -> list[Result]:
    """Fetch a batch concurrently, preserving input order in the result list.

    Batches default to ``quick=True``: only the best mirror, short budget.
    That is what makes a 400-item run finish in minutes instead of hours.
    Whatever fails gets reported so you can decide whether the slow full
    mirror list is worth another pass (``slow_retry``).
    """
    items = [normalize_doi(d) for d in dois if normalize_doi(d)]
    if not items:
        return []
    results: dict[str, Result] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch_pdf, d, outdir, quick=quick): d for d in items}
        for fut in as_completed(futures):
            doi = futures[fut]
            try:
                results[doi] = fut.result()
            except Exception as e:
                results[doi] = Result(doi=doi, ok=False, error=type(e).__name__)
    return [results[d] for d in items]


def slow_retry(
    dois,
    outdir: str | pathlib.Path = ".",
    *,
    workers: int = 1,
) -> list[Result]:
    """Second pass over failures using the full mirror list.

    Deliberately serial by default: some mirrors take 60-90s to answer, and
    running several of those at once makes a batch look hung. This is for a
    handful of DOIs you actually care about, not for bulk work.
    """
    return fetch_many(dois, outdir, workers=workers, quick=False)


# ----------------------------------------------------------------- CLI

def _read_dois(args) -> list[str]:
    dois: list[str] = []
    if args.stdin:
        dois += [ln.strip() for ln in sys.stdin if ln.strip()]
    if args.list:
        dois += [ln.strip() for ln in pathlib.Path(args.list).read_text(
            encoding="utf-8").splitlines() if ln.strip()]
    dois += args.dois
    deduped, seen = [], set()
    for d in dois:
        d = normalize_doi(d)
        if d and d not in seen:
            seen.add(d)
            deduped.append(d)
    return deduped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="doipdf",
        description="Resolve DOIs to PDF files from open sources.",
    )
    ap.add_argument("dois", nargs="*", help="one or more DOIs")
    ap.add_argument("-o", "--outdir", default=".", help="output directory (default: .)")
    ap.add_argument("-l", "--list", help="file with one DOI per line")
    ap.add_argument("--stdin", action="store_true", help="read DOIs from stdin")
    ap.add_argument("-w", "--workers", type=int, default=8,
                    help="parallel fetches (default: 8)")
    ap.add_argument("--quick", dest="quick", action="store_true", default=None,
                    help="force quick mode: best mirror only "
                         "(automatic for batches)")
    ap.add_argument("--full", dest="quick", action="store_false",
                    help="force the full mirror list")
    ap.add_argument("--retry-slow", action="store_true",
                    help="re-run only this batch's failures against every mirror")
    ap.add_argument("--json", action="store_true", help="emit JSON lines")
    ap.add_argument("--version", action="version", version=f"doipdf {__version__}")
    args = ap.parse_args(argv)

    dois = _read_dois(args)
    if not dois:
        ap.print_help()
        return 2

    started = time.time()

    # One DOI: walk the full mirror list, there is no time pressure.
    # Many DOIs: quick mode by default, so a 400-item batch takes minutes.
    if args.quick is None:
        quick = len(dois) > 1
    else:
        quick = args.quick

    results = fetch_many(dois, args.outdir, workers=args.workers, quick=quick)
    failed = [r.doi for r in results if not r.ok]

    if args.retry_slow and failed:
        print(f"\nquick pass left {len(failed)} unresolved; retrying with every mirror...")
        slow = slow_retry(failed, args.outdir)
        by_doi = {r.doi: r for r in results}
        by_doi.update({r.doi: r for r in slow})
        results = [by_doi[d] for d in dois]
        failed = [r.doi for r in results if not r.ok]
        quick = False          # the run as a whole now includes a full pass

    ok = sum(1 for r in results if r.ok)
    if args.json:
        for r in results:
            print(json.dumps(r.as_dict(), ensure_ascii=False))
    else:
        for r in results:
            if r.ok:
                print(f"OK    {r.doi}  [{r.source}]  {r.size} B  -> {r.path}")
            else:
                print(f"FAIL  {r.doi}  ({r.error})")
        elapsed = time.time() - started
        rate = len(results) / elapsed if elapsed > 0 else 0
        mode = "quick" if quick else "full"
        print(f"\n{ok}/{len(results)} fetched in {elapsed:.1f}s "
              f"({rate:.2f}/s, {mode} mode)")

        if failed and not args.retry_slow:
            if quick:
                print()
                print(f"{len(failed)} not found. Quick mode only checks the best mirror,")
                print("so some of these may exist elsewhere. To try every mirror:")
                print("  re-run with --retry-slow")
            else:
                print()
                print(f"{len(failed)} not found in any open source.")
                print("These need a browser-based route (institutional login or a")
                print("Cloudflare-protected publisher) or a library request.")

    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
