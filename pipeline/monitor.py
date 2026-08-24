#!/usr/bin/env python3
"""
Rulebook change monitor.

Checks each clearing house for amended or newly published rulebooks and writes
a status file the register reads. Designed to run unattended in GitHub Actions,
so no machine has to be switched on.

Three detection methods, because the clearing houses signal differently:

  published_dates  the index page prints a last-updated date per document.
                   Read the dates. Most reliable, and names which document moved.
                   (LCH Limited)

  link_manifest    build a manifest of every document link on the page - URL,
                   link text, inferred version - and diff it against last time.
                   Catches additions, removals and retargeted links.
                   (LCH SA, CME index, ICE index pages)

  content_hash     fetch the PDF, normalise, hash. Says "something changed" but
                   not what, so it is the fallback where no date is published.
                   (CME Securities Clearing, ICE Clear Europe)

Usage:  python3 monitor.py sources.json state.json
"""
from __future__ import annotations
import sys, os, json, re, hashlib, io
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

UA = "CCP-Rulebook-Monitor/1.0 (compliance reference tool; contact via repository)"
TIMEOUT = 40
DATE_RE = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2})\b", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(url: str) -> tuple[bytes, dict]:
    """Fetch a URL, returning body and the HTTP metadata worth remembering."""
    with httpx.Client(follow_redirects=True, timeout=TIMEOUT,
                      headers={"User-Agent": UA}) as c:
        r = c.get(url)
        r.raise_for_status()
        meta = {
            "status": r.status_code,
            "etag": r.headers.get("etag", ""),
            "last_modified": r.headers.get("last-modified", ""),
            "content_length": len(r.content),
            "content_type": r.headers.get("content-type", "").split(";")[0],
            "final_url": str(r.url),
        }
        return r.content, meta


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def pdf_marker(body: bytes) -> dict:
    """Version markers from inside a PDF: metadata dates and any date on page 1."""
    out = {}
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=body, filetype="pdf")
        md = doc.metadata or {}
        for k in ("modDate", "creationDate"):
            if md.get(k):
                out[k] = md[k]
        out["pages"] = doc.page_count
        if doc.page_count:
            first = doc[0].get_text()[:1500]
            m = DATE_RE.search(first)
            if m:
                out["cover_date"] = m.group(1)
        doc.close()
    except Exception as e:                       # PDF parsing must never break a run
        out["pdf_error"] = str(e)[:120]
    return out


def collect_links(html: bytes, base: str) -> list[dict]:
    """Every document link on a page, with its text and any date beside it."""
    soup = BeautifulSoup(html, "html.parser")
    seen, links = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        if not re.search(r"\.(pdf|docx?|xlsx?)($|\?)", href, re.I):
            continue
        url = httpx.URL(base).join(href)
        key = str(url)
        if key in seen:
            continue
        seen.add(key)
        text = norm_text(a.get_text())
        # a date printed near the link is the document's version stamp
        context = norm_text(a.parent.get_text() if a.parent else "")
        m = DATE_RE.search(context)
        links.append({
            "url": key,
            "text": text[:160],
            "filename": key.rsplit("/", 1)[-1][:120],
            "date": m.group(1) if m else "",
        })
    links.sort(key=lambda x: x["url"])
    return links


def check_source(src: dict, prev: dict) -> dict:
    """Check one source and describe what, if anything, moved."""
    res = {
        "id": src["id"], "ccp": src["ccp"], "entity": src.get("entity", ""),
        "label": src["label"], "url": src["url"], "method": src["method"],
        "checked_at": now(), "ok": True, "changed": False, "changes": [],
        "error": "",
    }
    if src.get("enabled") is False or src["url"].startswith("TO BE"):
        res.update(ok=False, error="Source not configured", checked_at=now())
        return res

    try:
        body, meta = fetch(src["url"])
    except Exception as e:
        res.update(ok=False, error=f"{type(e).__name__}: {str(e)[:140]}")
        # a fetch failure is NOT "no change" - keep the last known good state
        res["last_good"] = prev.get("last_good") or prev.get("checked_at", "")
        return res

    res["http"] = meta
    method = src["method"]

    if method == "content_hash":
        digest = hashlib.sha256(body).hexdigest()
        res["hash"] = digest
        res["marker"] = pdf_marker(body) if meta["content_type"] == "application/pdf" else {}
        old = prev.get("hash")
        if old and old != digest:
            res["changed"] = True
            detail = "Document content changed"
            oldm, newm = prev.get("marker", {}), res["marker"]
            if newm.get("cover_date") and newm.get("cover_date") != oldm.get("cover_date"):
                detail += f" - cover now reads {newm['cover_date']} (was {oldm.get('cover_date') or 'unknown'})"
            res["changes"].append({"kind": "content", "detail": detail})
        elif not old:
            res["changes"].append({"kind": "baseline", "detail": "First check - baseline recorded"})

    else:  # published_dates and link_manifest both work off the link manifest
        links = collect_links(body, meta["final_url"])
        res["links"] = links
        res["doc_count"] = len(links)
        old_links = {l["url"]: l for l in prev.get("links", [])}
        new_links = {l["url"]: l for l in links}

        if not old_links:
            res["changes"].append({"kind": "baseline",
                                   "detail": f"First check - {len(links)} documents recorded"})
        else:
            for url, l in new_links.items():
                if url not in old_links:
                    res["changed"] = True
                    res["changes"].append({"kind": "added",
                        "detail": f"New document: {l['text'] or l['filename']}", "url": url})
                elif l["date"] and l["date"] != old_links[url]["date"]:
                    res["changed"] = True
                    res["changes"].append({"kind": "updated",
                        "detail": f"{l['text'] or l['filename']} - date changed to {l['date']} "
                                  f"(was {old_links[url]['date'] or 'unknown'})", "url": url})
            for url, l in old_links.items():
                if url not in new_links:
                    res["changed"] = True
                    res["changes"].append({"kind": "removed",
                        "detail": f"Document removed: {l['text'] or l['filename']}", "url": url})

        # a page that suddenly yields far fewer documents means the layout broke,
        # not that the clearing house deleted its rulebook
        old_n = prev.get("doc_count", 0)
        if old_n >= 5 and len(links) < old_n * 0.5:
            res.update(ok=False,
                       error=f"Only {len(links)} documents found, previously {old_n} - page layout may have changed")

    if res["changed"]:
        res["last_change_at"] = res["checked_at"]
    else:
        res["last_change_at"] = prev.get("last_change_at", "")
    return res


def main(sources_path: str, state_path: str) -> int:
    cfg = json.load(open(sources_path))
    prev_state = {}
    if os.path.exists(state_path):
        try:
            prev_state = {s["id"]: s for s in json.load(open(state_path)).get("sources", [])}
        except Exception:
            prev_state = {}

    results = [check_source(s, prev_state.get(s["id"], {})) for s in cfg["sources"]]

    changed = [r for r in results if r["changed"] and
               not any(c["kind"] == "baseline" for c in r["changes"])]
    broken = [r for r in results if not r["ok"]]

    out = {
        "generated_at": now(),
        "summary": {
            "sources": len(results),
            "changed": len(changed),
            "broken": len(broken),
            "healthy": sum(1 for r in results if r["ok"] and not r["changed"]),
        },
        "sources": results,
    }
    with open(state_path, "w") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)

    for r in results:
        flag = "BROKEN " if not r["ok"] else ("CHANGED" if r["changed"] else "ok     ")
        print(f"{flag} {r['label'][:44]:46s} {r.get('error','')}")
        for c in r["changes"]:
            print(f"          - {c['detail']}")
    print(f"\n{len(changed)} changed, {len(broken)} broken, "
          f"{out['summary']['healthy']} unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
