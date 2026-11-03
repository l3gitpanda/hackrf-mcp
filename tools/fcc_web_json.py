"""Turn the parsed US Table of Frequency Allocations (tools/fcc_us_table.py)
into the compact JSON the web UI's band plan strip reads.

    uv run --no-project --with pdfplumber python -I tools/fcc_us_table.py fcctable.pdf out/
    python tools/fcc_web_json.py out/us-allocations.json src/hackrf_mcp/web/static/fcc-us-allocations.json

The source is the FCC's Online Table of Frequency Allocations (47 CFR 2.106),
https://transition.fcc.gov/oet/spectrum/table/fcctable.pdf, a work of the US
Government. The eCFR prints the same table only as images. Footnotes are
left out (their codes mean little without their text); services keep their
printed case, which is how the table marks primary (UPPER CASE) and
secondary (Mixed case) allocations.
"""

import json
import sys


def compact(parsed: dict, revised: str) -> dict:
    def bands(rows, parts):
        out = []
        for b in rows:
            e = {"lo": b["lo_hz"], "hi": b["hi_hz"], "services": [s["name"] for s in b["services"]]}
            if parts:
                e["parts"] = b.get("rule_parts", [])
            out.append(e)
        return out

    return {
        "source": "FCC Online Table of Frequency Allocations (47 CFR 2.106)",
        "url": "https://transition.fcc.gov/oet/spectrum/table/fcctable.pdf",
        "revised": revised,
        "about": "The United States Table only (Federal and Non-Federal) with the FCC rule parts, about 1 MHz to 6 GHz. "
        "UPPER CASE services are primary, Mixed case secondary. Footnotes left out.",
        "federal": bands(parsed["federal"], False),
        "non_federal": bands(parsed["non_federal"], True),
    }


def write(data: dict, path: str) -> None:
    # One band per line, so a revision shows up as a readable diff.
    head = {k: v for k, v in data.items() if k not in ("federal", "non_federal")}
    lines = ["{"]
    lines += [f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}," for k, v in head.items()]
    for i, col in enumerate(("federal", "non_federal")):
        lines.append(f'  "{col}": [')
        lines.append(",\n".join("    " + json.dumps(b, ensure_ascii=False) for b in data[col]))
        lines.append("  ]" + ("," if i == 0 else ""))
    lines.append("}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    revised = sys.argv[3] if len(sys.argv) > 3 else "2022-07-01"
    write(compact(json.load(open(src)), revised), dst)
