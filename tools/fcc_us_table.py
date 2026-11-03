"""Parse the US Table (Federal, Non-Federal, FCC Rule Part(s)) out of the
FCC Online Table of Frequency Allocations PDF.

Usage: python -I parse_us.py <fcctable.pdf> <outdir> [first_page last_page]

Cell geometry comes from the drawn table rules (thin filled rects); each word
is placed in the cell found by casting rays from the word to the nearest rule
on each side.  Band edges come from the band-edge token on the first line of
each cell.  Nothing is guessed: cells that do not fit the expected shape are
listed in coverage.txt and left out of the JSON.
"""
import json
import re
import sys
from decimal import Decimal

import pdfplumber

BAND_RE = re.compile(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$")
FN_RE = re.compile(r"^(?:5\.\d+[A-Z]*|US\d+[A-Z]*|NG\d+[A-Z]*|G\d+[A-Z]*)$")
TITLE_RE = re.compile(
    r"Table of Frequency Allocations\s+([\d.]+)-([\d.]+)\s+(kHz|MHz|GHz)")
SEE_RE = re.compile(r"^\(see (previous|next) page\)$", re.I)
SEE_BAND_RE = re.compile(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?) (kHz|MHz|GHz): see previous page$", re.I)
SOFT = "\u00ad"  # marks a hyphen that ended a wrapped line
MULT = {"kHz": Decimal(1000), "MHz": Decimal(1000000), "GHz": Decimal(1000000000)}

LO_LIMIT = 1_000_000          # keep bands whose hi is above 1 MHz
HI_LIMIT = 6_000_000_000      # ... and whose lo is below 6 GHz


def segments(page):
    """Split the page's rules into horizontal and vertical segments."""
    hs, vs = [], []
    objs = list(page.rects) + list(page.lines)
    for r in objs:
        x0, x1, top, bot = r["x0"], r["x1"], r["top"], r["bottom"]
        w, h = x1 - x0, bot - top
        if w < 1.0 and h < 1.0:
            continue  # junction squares
        if w >= h and h < 3.0:
            hs.append((x0, x1, (top + bot) / 2))
        elif h > w and w < 3.0:
            vs.append(((x0 + x1) / 2, top, bot, x0, x1))
    return hs, vs


def cluster(values, tol):
    out = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def find_doubles(vs):
    """Return the two double rules: (Intl|US) and (US|Rule) as (left_x0, right_x1)."""
    by_x = {}
    for xm, top, bot, x0, x1 in vs:
        by_x.setdefault(round(x0, 1), []).append((top, bot, x0, x1))
    xs = cluster(by_x.keys(), 0.3)
    cl = []
    for group in xs:
        items = [it for x in group for it in by_x[x]]
        total = sum(b - t for t, b, _, _ in items)
        cl.append((min(it[2] for it in items), max(it[3] for it in items), total))
    cl.sort()
    pairs = []
    for a, b in zip(cl, cl[1:]):
        if 0.6 <= b[0] - a[0] <= 2.6:
            pairs.append((min(a[2], b[2]), a[0], b[1]))
    pairs.sort(reverse=True)
    best = sorted(pairs[:2], key=lambda p: p[1])
    if len(best) != 2:
        return None
    return (best[0][1], best[0][2]), (best[1][1], best[1][2])


def text_of(words):
    """Join words of one line; a gap under 0.8 pt means the same word."""
    out = ""
    prev = None
    for w in words:
        if prev is not None:
            out += "" if w["x0"] - prev["x1"] < 0.8 else " "
        out += w["text"]
        prev = w
    return out


def group_lines(words, tol=2.5):
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        for ln in lines:
            if abs(ln["top"] - w["top"]) <= tol:
                ln["words"].append(w)
                break
        else:
            lines.append({"top": w["top"], "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
        ln["x0"] = ln["words"][0]["x0"]
        ln["text"] = text_of(ln["words"])
    lines.sort(key=lambda ln: ln["top"])
    return lines


def join(prev, nxt):
    if prev.endswith("-"):
        return prev[:-1] + SOFT + nxt   # decided later: real hyphen or word break
    if prev.endswith("/"):
        return prev + nxt
    return prev + " " + nxt


def is_primary(name):
    for tok in name.split():
        letters = re.sub(r"[^A-Za-z]", "", tok)
        if letters:
            return letters.upper() == letters
    return False


def parse_services(lines, margin, problems):
    """Lines after the band-edge line -> services, cell footnotes."""
    services = []
    cell_fns = []
    all_fns = []
    for ln in lines:
        txt = ln["text"].strip()
        if SEE_RE.match(txt):
            continue
        toks = txt.split()
        indented = ln["x0"] > margin + 2.5
        if all(FN_RE.match(t) for t in toks):
            if indented and services and not cell_fns:
                services[-1]["footnotes"] += toks
            else:
                cell_fns += toks
            all_fns += toks
            continue
        if BAND_RE.match(toks[0]):
            problems.append(f"second band-edge token inside cell: {txt!r}")
            return None
        # trailing footnotes
        k = len(toks)
        while k > 0 and FN_RE.match(toks[k - 1]):
            k -= 1
        name_toks, fns = toks[:k], toks[k:]
        if any(FN_RE.match(t) for t in name_toks):
            problems.append(f"footnote token inside service text: {txt!r}")
            return None
        name = " ".join(name_toks)
        cont = indented or name[:1].islower() or name[:1] == "("
        if cont:
            if not services:
                problems.append(f"continuation line with no service before it: {txt!r}")
                return None
            if cell_fns:
                problems.append(f"service text after cell footnotes: {txt!r}")
                return None
            services[-1]["name"] = join(services[-1]["name"], name)
            services[-1]["footnotes"] += fns
        else:
            if cell_fns:
                problems.append(f"service text after cell footnotes: {txt!r}")
                return None
            services.append({"name": name, "footnotes": list(fns)})
        all_fns += fns
    for s in services:
        s["primary"] = is_primary(s["name"])
    return services, all_fns


def parse_rules(lines):
    entries = []
    if not lines:
        return entries
    margin = min(ln["x0"] for ln in lines)
    for ln in lines:
        txt = ln["text"].strip()
        if SEE_RE.match(txt):
            continue
        indented = ln["x0"] > margin + 2.5
        if entries and (indented or txt[:1].islower() or txt[:1] == "("):
            entries[-1] = join(entries[-1], txt)
        else:
            entries.append(txt)
    return entries


def main():
    src, outdir = sys.argv[1], sys.argv[2]
    first = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    last = int(sys.argv[4]) if len(sys.argv) > 4 else 45

    lists = {"federal": [], "non_federal": []}
    report = []           # per-page coverage lines
    unparsed = []         # (page, column, y, reason, text)
    notes = []
    debug = []
    unit = None
    corpus = {}
    span_notes = []

    with pdfplumber.open(src) as pdf:
        for pno in range(first, last + 1):
            page = pdf.pages[pno - 1]
            words = page.extract_words()
            for w in words:
                corpus[w["text"].strip("(),;").lower()] = corpus.get(w["text"].strip("(),;").lower(), 0) + 1
            full = page.extract_text() or ""
            m = TITLE_RE.search(full)
            if m:
                unit = m.group(3)
            if unit is None:
                raise SystemExit(f"no unit known at page {pno}")
            mult = MULT[unit]

            hs, vs = segments(page)
            dbl = find_doubles(vs)
            if dbl is None:
                unparsed.append((pno, "-", 0, "could not find the column rules", ""))
                continue
            (il, ir), (rl, rr) = dbl
            US_L, US_R, RU_L = ir, rl, rr
            RU_R = max(h[1] for h in hs)
            page_bottom = max(h[2] for h in hs)

            # header: data starts below the "Federal Table" heading row
            data_top = 0.0
            for i, w in enumerate(words):
                if w["text"] in ("Federal", "Non-Federal") and w["top"] < 130 \
                        and i + 1 < len(words) and words[i + 1]["text"] == "Table":
                    data_top = max(data_top, w["bottom"])
            # footer "Page N" inside the last rule-part cell
            drop = set()
            for i, w in enumerate(words):
                if w["text"] == "Page" and i + 1 < len(words) and words[i + 1]["text"].isdigit() \
                        and abs(words[i + 1]["top"] - w["top"]) < 2 and w["x0"] > RU_L - 40:
                    drop.add(i)
                    drop.add(i + 1)

            fn_vs = [v for v in vs if US_L + 2 < v[0] < US_R - 2]
            ru_vs = [v for v in vs if RU_L + 2 < v[0] < RU_R - 2]

            def ray(x, y, region_l, region_r, inner_vs):
                top = max([h[2] for h in hs if h[0] - 0.6 <= x <= h[1] + 0.6 and h[2] < y],
                          default=0.0)  # open at the page head: runs on from the previous page
                bot = min([h[2] for h in hs if h[0] - 0.6 <= x <= h[1] + 0.6 and h[2] > y],
                          default=page.height)  # open at the page foot: runs on to the next page
                left = max([v[0] for v in inner_vs if v[1] - 0.6 <= y <= v[2] + 0.6 and v[0] < x],
                           default=region_l)
                right = min([v[0] for v in inner_vs if v[1] - 0.6 <= y <= v[2] + 0.6 and v[0] > x],
                            default=region_r)
                return top, bot, left, right

            us_cells, ru_cells = {}, {}
            for i, w in enumerate(words):
                if i in drop or w["top"] < data_top:
                    continue
                x, y = w["x0"] + 1.0, (w["top"] + w["bottom"]) / 2
                if US_L < x < US_R:
                    region, cells = (US_L, US_R, fn_vs), us_cells
                elif RU_L < x < RU_R:
                    region, cells = (RU_L, RU_R, ru_vs), ru_cells
                else:
                    continue
                top, bot, left, right = ray(x, y, *region)
                if top is None or bot is None:
                    unparsed.append((pno, "?", y, "word outside any closed cell", w["text"]))
                    continue
                if top < data_top - 3:
                    unparsed.append((pno, "?", y, "cell top above table data", w["text"]))
                    continue
                key = (round(top, 0), round(bot, 0), round(left, 0), round(right, 0))
                cells.setdefault(key, []).append(w)
                # word crossing a column rule
                if w["x1"] > right + 1.5 and right < region[1] - 1:
                    notes.append(f"p{pno}: word {w['text']!r} at y={y:.0f} runs past the column rule at x={right:.0f}")

            # all band tokens seen in the US columns on this page
            seen_tokens = [w["text"] for i, w in enumerate(words)
                           if i not in drop and w["top"] >= data_top and US_L < w["x0"] + 1 < US_R
                           and BAND_RE.match(w["text"])]

            # rule-part cells
            rcells = []
            for (top, bot, left, right), ws in ru_cells.items():
                rcells.append({"top": top, "bot": bot, "entries": parse_rules(group_lines(ws))})

            # classify the US cells
            fn_x = sorted(set(round(v[0]) for v in fn_vs))
            emitted = []
            for (top, bot, left, right) in sorted(us_cells, key=lambda k: (k[0], k[2])):
                ws = us_cells[(top, bot, left, right)]
                if abs(left - US_L) <= 2 and abs(right - US_R) <= 2:
                    targets = ["federal", "non_federal"]
                    col = "Fed+NonFed"
                elif abs(left - US_L) <= 2:
                    targets, col = ["federal"], "Fed"
                elif abs(right - US_R) <= 2:
                    targets, col = ["non_federal"], "NonFed"
                else:
                    unparsed.append((pno, "?", top, f"cell between x={left}..{right} not a known column",
                                     " ".join(w["text"] for w in ws)))
                    continue
                lines = group_lines(ws)
                margin = min(ln["x0"] for ln in lines)
                first_line = lines[0]["text"].strip()
                bm = BAND_RE.match(first_line)
                see_band = SEE_BAND_RE.match(first_line)
                if see_band:
                    lines = lines[1:]
                rules = []
                if "non_federal" in targets:
                    for rc in rcells:
                        ov = min(bot, rc["bot"]) - max(top, rc["top"])
                        if ov > 1.5:
                            rules += rc["entries"]
                            rc["used"] = True
                            if rc["top"] < top - 1.5 or rc["bot"] > bot + 1.5:
                                span_notes.append(
                                    f"p{pno}: rule-part cell y={rc['top']:.0f}-{rc['bot']:.0f} "
                                    f"{rc['entries']} spans beyond US cell y={top:.0f}-{bot:.0f} ({first_line})")
                problems = []
                if bm is None:
                    # continuation from the previous page?
                    is_top = top <= min(k[0] for k in us_cells) + 2  # first cell row on page
                    if not is_top:
                        unparsed.append((pno, col, top, "cell has no band-edge token on its first line",
                                         " | ".join(ln["text"] for ln in lines)))
                        continue
                    res = parse_services(lines, margin, problems)
                    if res is None:
                        unparsed.append((pno, col, top, "; ".join(problems),
                                         " | ".join(ln["text"] for ln in lines)))
                        continue
                    services, fns = res
                    for t in targets:
                        if not lists[t]:
                            unparsed.append((pno, col, top, "continuation with nothing before it",
                                             " | ".join(ln["text"] for ln in lines)))
                            continue
                        prev = lists[t][-1]
                        if not prev.get("_open_bottom") and (services or fns):
                            notes.append(f"p{pno}: {col} cell at the page head has no band-edge token, "
                                         f"but the {t} band before it ({prev['_tok']} p{prev['page']}) "
                                         f"was closed at its page foot; appended to it anyway")
                        if see_band:
                            shi = int(Decimal(see_band.group(2)) * MULT[see_band.group(3)])
                            slo = int(Decimal(see_band.group(1)) * MULT[see_band.group(3)])
                            notes.append(f"p{pno}: {col} continuation marker {first_line!r} "
                                         f"continues {t} band {prev['_tok']} p{prev['page']}"
                                         + ("" if (slo, shi) == (prev['lo_hz'], prev['hi_hz'])
                                            else "  ** marker edges differ from the band's own edges **"))
                        prev["services"] += services
                        prev["footnotes"] += fns
                        if t == "non_federal":
                            prev["rule_parts"] += rules
                        if services or fns or (t == "non_federal" and rules):
                            prev.setdefault("_cont", []).append(pno)
                        prev["_open_bottom"] = bot >= page.height - 1
                    emitted.append(f"(continuation of previous page into {col}: "
                                   f"{' | '.join(ln['text'] for ln in lines)[:80]})")
                    continue
                if len(lines[0]["words"]) != 1:
                    unparsed.append((pno, col, top, "extra words on the band-edge line",
                                     " | ".join(ln["text"] for ln in lines)))
                    continue
                res = parse_services(lines[1:], margin, problems)
                if res is None:
                    unparsed.append((pno, col, top, "; ".join(problems),
                                     " | ".join(ln["text"] for ln in lines)))
                    continue
                services, fns = res
                lo = int(Decimal(bm.group(1)) * mult)
                hi = int(Decimal(bm.group(2)) * mult)
                if hi <= lo:
                    unparsed.append((pno, col, top, "band edges not increasing", first_line))
                    continue
                for t in targets:
                    rec = {"lo_hz": lo, "hi_hz": hi,
                           "services": [dict(s, footnotes=list(s["footnotes"])) for s in services],
                           "footnotes": list(fns), "page": pno}
                    if t == "non_federal":
                        rec["rule_parts"] = list(rules)
                    rec["_tok"] = first_line + " " + unit
                    rec["_y"] = top
                    rec["_open_bottom"] = bot >= page.height - 1
                    lists[t].append(rec)
                emitted.append(f"{first_line} [{col}]")
                debug.append({"page": pno, "col": col, "y": top,
                              "lines": [(round(ln["x0"] - margin, 1), ln["text"]) for ln in lines],
                              "rules": rules})
            for rc in rcells:
                if rc["entries"] and not rc.get("used"):
                    unparsed.append((pno, "Rule", rc["top"], "rule-part cell next to no Non-Federal cell",
                                     "; ".join(rc["entries"])))
            # words near the US/rule columns that were not placed in any cell
            for i, w in enumerate(words):
                if i in drop or w["top"] < data_top:
                    continue
                x = w["x0"] + 1.0
                if (US_L - 4 < x <= US_L) or (US_R <= x <= RU_L) or x >= RU_R:
                    unparsed.append((pno, "-", w["top"], "word on a column rule, not placed", w["text"]))
                if x <= US_L and w["x1"] > US_L + 2:
                    notes.append(f"p{pno}: International-column word {w['text']!r} runs into the US columns")
            report.append((pno, unit, seen_tokens, emitted, fn_x))

    # dedupe footnotes keeping order
    def dedupe(xs):
        out = []
        for x in xs:
            if x not in out:
                out.append(x)
        return out

    hyph_log = {}

    def unsoft(text):
        out = []
        for tok in text.split(" "):
            while SOFT in tok:
                a, b = tok.split(SOFT, 1)
                wa = re.split(r"[-(/]", a)[-1]
                wb = re.split(r"[-)/]", b)[0]
                joined = (wa + wb).strip("(),;").lower()
                hyph = (wa + "-" + wb).strip("(),;").lower()
                if corpus.get(joined, 0) > 0 and corpus.get(hyph, 0) == 0:
                    tok = a + b
                    hyph_log[f"{wa}-|{wb} -> {wa + wb}"] = hyph_log.get(f"{wa}-|{wb} -> {wa + wb}", 0) + 1
                else:
                    tok = a + "-" + b
                    hyph_log[f"{wa}-|{wb} -> {wa}-{wb}"] = hyph_log.get(f"{wa}-|{wb} -> {wa}-{wb}", 0) + 1
            out.append(tok)
        return " ".join(out)

    for lst in lists.values():
        for r in lst:
            for sv in r["services"]:
                sv["name"] = unsoft(sv["name"])
            if "rule_parts" in r:
                r["rule_parts"] = [unsoft(x) for x in r["rule_parts"]]

    final = {}
    gaps = {}
    for name, lst in lists.items():
        # keep those overlapping [1 MHz, 6 GHz)
        sel = [r for r in lst if r["hi_hz"] > LO_LIMIT and r["lo_hz"] < HI_LIMIT]
        probs = []
        ordered = sorted(sel, key=lambda r: (r["lo_hz"], r["hi_hz"]))
        if [id(r) for r in ordered] != [id(r) for r in sel]:
            probs.append("bands are not in ascending order in the PDF reading order")
        if ordered and ordered[0]["lo_hz"] > LO_LIMIT:
            probs.append(f"GAP below first band: {LO_LIMIT} .. {ordered[0]['lo_hz']}")
        if ordered and ordered[-1]["hi_hz"] < HI_LIMIT:
            probs.append(f"GAP above last band: {ordered[-1]['hi_hz']} .. {HI_LIMIT}")
        for a, b in zip(ordered, ordered[1:]):
            if b["lo_hz"] > a["hi_hz"]:
                probs.append(f"GAP {a['hi_hz']} .. {b['lo_hz']} Hz  (after {a['_tok']} p{a['page']}, "
                             f"before {b['_tok']} p{b['page']})")
            elif b["lo_hz"] < a["hi_hz"]:
                probs.append(f"OVERLAP {a['_tok']} p{a['page']} with {b['_tok']} p{b['page']}")
        gaps[name] = probs
        out = []
        for r in ordered:
            rec = {"lo_hz": r["lo_hz"], "hi_hz": r["hi_hz"],
                   "services": [{"name": s["name"], "primary": s["primary"],
                                 "footnotes": dedupe(s["footnotes"])} for s in r["services"]],
                   "footnotes": dedupe(r["footnotes"]),
                   "page": r["page"]}
            if "rule_parts" in r:
                rec["rule_parts"] = dedupe(r["rule_parts"])
            if r.get("_cont"):
                rec["continued_on_pages"] = r["_cont"]
            out.append(rec)
        final[name] = out

    doc = {"source": "FCC Online Table of Frequency Allocations (47 CFR 2.106), revised 2022-07-01",
           "federal": final["federal"], "non_federal": final["non_federal"]}
    with open(f"{outdir}/us-allocations.json", "w") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write("\n")

    with open(f"{outdir}/coverage.txt", "w") as f:
        f.write("Coverage report for us-allocations.json\n")
        f.write(f"federal bands: {len(final['federal'])}, non_federal bands: {len(final['non_federal'])}\n")
        f.write("Scope: every US-Table cell overlapping 1 MHz .. 6 GHz (PDF pages %d-%d read). The first band\n"
                "in each list (535-1605 kHz) straddles 1 MHz and the last (5925-6425 MHz) straddles 6 GHz;\n"
                "both are kept whole. A cell spanning the Federal and Non-Federal columns is emitted in both\n"
                "lists. Cells with no services (an empty column for that band) are emitted with services [].\n"
                "A cell that runs onto the next page has the next page's text appended and lists that page\n"
                "in continued_on_pages. Band footnotes = every footnote in the cell; a service's own\n"
                "footnotes = those printed after it on its line(s).\n\n" % (first, last))
        for name in ("federal", "non_federal"):
            f.write(f"== Tiling problems in {name} (1 MHz .. 6 GHz) ==\n")
            for p in gaps[name] or ["none"]:
                f.write(f"  {p}\n")
            f.write("\n")
        f.write("== Cells not parsed (left out of the JSON) ==\n")
        if not unparsed:
            f.write("  none\n")
        for u in unparsed:
            f.write(f"  p{u[0]} {u[1]} y={u[2]:.0f}: {u[3]}: {u[4]}\n")
        f.write("\n== Line-end hyphens: kept or removed (word counts from all words on pages read) ==\n")
        for k, v in sorted(hyph_log.items()):
            f.write(f"  {k}  (x{v})\n")
        f.write("\n== Notes ==\n")
        for n in notes or ["none"]:
            f.write(f"  {n}\n")
        f.write("\n== Rule-part cells shared by adjacent Non-Federal rows (expected: the PDF merges the\n"
                "   FCC Rule Part(s) cell across rows with the same rule parts; each row gets all of them) ==\n")
        for n in span_notes or ["none"]:
            f.write(f"  {n}\n")
        f.write("\n== Per page: band-edge tokens seen in the US columns vs cells emitted ==\n")
        for pno, unit_, seen, emitted, fn_x in report:
            f.write(f"p{pno} ({unit_}), Fed|NonFed rule x={fn_x}\n")
            f.write(f"  tokens seen ({len(seen)}): {' '.join(seen)}\n")
            used = [e.split(" [")[0] for e in emitted if not e.startswith("(")]
            left = list(seen)
            for u in used:
                if u in left:
                    left.remove(u)
            extra = [u for u in used if u not in seen]
            f.write(f"  tokens not used as a cell's band edge: {left or 'none'}"
                    f"{'; emitted but not seen: ' + str(extra) if extra else ''}\n")
            f.write(f"  cells emitted ({len(emitted)}):\n")
            for e in emitted:
                f.write(f"    {e}\n")
    with open(f"{outdir}/../fcc-tools/debug-cells.json", "w") as f:
        json.dump(debug, f, indent=0)


if __name__ == "__main__":
    main()
