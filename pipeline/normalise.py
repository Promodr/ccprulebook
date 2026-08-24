#!/usr/bin/env python3
"""
Normalise the 7 CCP rulebook spreadsheets into one canonical schema.

No LLM. Pure structural work: column mapping, stable IDs, glossary extraction.
Prior classifications from the earlier tagging pass are PRESERVED (prefixed
`prior_`) rather than dropped -- they are used as a disagreement cross-check
against the new tagging, which is a free quality signal.

Usage:  python3 normalise.py <source_excel_dir> <output_dir>
"""
import sys, os, json, glob, hashlib, re
from datetime import datetime, timezone
import pandas as pd

# book_code -> (ccp, display name, hierarchy cols in order, text col, summary col,
#               y/n col, classification col, nature col)
BOOKS = {
    "CME_SC": dict(
        file="CMESC.xlsx", ccp="CME", name="CME Security Clearing Rules",
        hier=["Chapter Number and Name", "Rule Number and Name", "Sub-rule"],
        text="Sub-rule Text", summary="Plain-English Summary",
        yn="Member Obligation (Y/N)", cls="Classification", nature="Nature of Obligation"),
    "ICE_EU": dict(
        file="ICE ClearEurope.xlsx", ccp="ICE_CLEAR_EUROPE", name="ICE Clear Europe Rules",
        hier=["Part Title", "Rule Title", "Sub-rule Name"],
        text="Sub-rule Text", summary="Plain English Summary",
        yn="Member Obligation (Y/N)", cls="Classification", nature="Nature of Obligation"),
    "ICE_CC": dict(
        file="ICE_ClearCredit.xlsx", ccp="ICE_CLEAR_CREDIT", name="ICE Clear Credit Rules",
        hier=["Chapter Title", "Rule Title", "Sub-rule Name"],
        text="Sub-rule Text", summary="Plain English Summary",
        yn="Member Obligation (Y/N)", cls="Classification", nature="Nature of Obligation"),
    "ICE_US": dict(
        file="ICE_ClearUS.xlsx", ccp="ICE_CLEAR_US", name="ICE Clear US Rules",
        hier=["Part", "Rule", "Rule Title", "Subrule"],
        text="Provision Text", summary="Plain English Summary",
        yn="Member Obligation (Y/N)", cls="Classification", nature="Nature of Obligation"),
    "LCH_LTD_DEF": dict(
        file="LCH_Limited-DefaultRules.xlsx", ccp="LCH_LTD", name="LCH Limited Default Rules",
        hier=["Chapter Number and Name", "Rule Number and Name", "Sub-rule"],
        text="Sub-rule Text", summary="Plain English Summary",
        yn="Member Obligation (Y/N)", cls="Classification", nature="Nature of Obligation",
        extra={"inherited_context": "Inherited Context (from Parent Rule)"}),
    "LCH_LTD_GEN": dict(
        file="LCH_Limited_GeneralRules.xlsx", ccp="LCH_LTD", name="LCH Limited General Regulations",
        hier=["Chapter Number and Name", "Rule Number and Name", "Sub-rule"],
        text="Sub-rule Text", summary="Plain English Summary",
        yn="Member Obligation (Y/N)",
        cls="Classification (Member/Conditional/Informational)", nature="Nature of Obligation"),
    "LCH_SA_REPO": dict(
        file="LCH_SA-Repo.xlsx", ccp="LCH_SA", name="LCH SA Clearing Rules (RepoClear)",
        hier=["Chapter Number and Name", "Rule Number and Name", "Sub-rule"],
        text="Sub-rule Text", summary="Plain-English Summary",
        yn="Member Obligation (Y/N)", cls="Classification", nature="Nature of Obligation"),
}

DEF_PAT = re.compile(r"definition|defined term|\bmeans\b", re.I)


def clean(v):
    if v is None:
        return ""
    s = str(v)
    if s.strip().lower() in ("nan", "none", "nat"):
        return ""
    return s.strip()


def normalise_book(code, cfg, src_dir):
    path = os.path.join(src_dir, cfg["file"])
    df = pd.ExcelFile(path).parse(0)
    rows = []
    for i, r in df.iterrows():
        text = clean(r.get(cfg["text"]))
        hier = [clean(r.get(c)) for c in cfg["hier"]]
        citation = " > ".join([h for h in hier if h])
        rec = {
            "source_row_id": f"{code}-{i+2:05d}",   # +2 = Excel row incl. header
            "ccp": cfg["ccp"],
            "book_code": code,
            "book_name": cfg["name"],
            "excel_row": int(i) + 2,
            "hierarchy": hier,
            "citation": citation,
            "rule_text": text,
            "char_len": len(text),
            "text_sha1": hashlib.sha1(text.encode("utf-8")).hexdigest(),
            # carried over from the earlier tagging pass -- cross-check only
            "prior_plain_summary": clean(r.get(cfg["summary"])),
            "prior_member_obligation": clean(r.get(cfg["yn"])),
            "prior_classification": clean(r.get(cfg["cls"])),
            "prior_nature": clean(r.get(cfg["nature"])),
        }
        for k, col in (cfg.get("extra") or {}).items():
            rec[k] = clean(r.get(col))
        # NewRule column in the ICE files confirmed as an extraction artefact -- dropped
        # heuristic: is this row a definition? used to build the glossary layer
        rec["is_definition_candidate"] = bool(
            DEF_PAT.search(rec["prior_nature"]) or
            DEF_PAT.search(rec["prior_classification"])
        )
        rows.append(rec)
    return rows


def main(src_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "books": {}}
    grand = 0
    for code, cfg in BOOKS.items():
        rows = normalise_book(code, cfg, src_dir)
        with open(os.path.join(out_dir, f"{code}.jsonl"), "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

        defs = sum(r["is_definition_candidate"] for r in rows)
        empty = sum(1 for r in rows if not r["rule_text"])
        long_rows = sum(1 for r in rows if r["char_len"] > 4000)
        prior_y = sum(1 for r in rows if r["prior_member_obligation"].upper().startswith("Y"))
        # rows that still need full enrichment: everything that isn't an obvious definition
        workload = len(rows) - defs
        report["books"][code] = {
            "ccp": cfg["ccp"], "name": cfg["name"], "source_file": cfg["file"],
            "rows": len(rows), "empty_text": empty,
            "definition_candidates": defs,
            "rows_over_4k_chars": long_rows,
            "prior_member_obligation_Y": prior_y,
            "prior_Y_rate": round(prior_y / len(rows), 3),
            "prior_classification_values": sorted(
                {r["prior_classification"] for r in rows if r["prior_classification"]}),
            "distinct_prior_nature_values": len(
                {r["prior_nature"] for r in rows if r["prior_nature"]}),
            "enrichment_workload": workload,
        }
        grand += len(rows)
        print(f"{code:14s} {len(rows):5d} rows | defs {defs:5d} | >4k {long_rows:3d} | "
              f"prior-Y {prior_y:4d} ({prior_y/len(rows):.0%}) | to enrich ~{workload}")

    report["total_rows"] = grand
    report["total_enrichment_workload"] = sum(
        b["enrichment_workload"] for b in report["books"].values())
    with open(os.path.join(out_dir, "corpus_report.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nTOTAL {grand} rows | to enrich ~{report['total_enrichment_workload']}")
    return report


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
