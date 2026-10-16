#!/usr/bin/env python3
"""Build the static site: download the WCA TSV export and render dist/index.html."""
import html
import io
import json
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
EXPORT_PAGE = "https://www.worldcubeassociation.org/export/results"
DELEGATE_RE = re.compile(r"\[\{([^}]*)\}")


def find_export_url():
    page = urllib.request.urlopen(EXPORT_PAGE).read().decode()
    m = re.search(r'https://[^"]+\.tsv\.zip', page)
    if not m:
        sys.exit("Could not find TSV export link")
    return m.group(0)


def open_export(arg):
    """Return a zipfile for a local path/dir or the latest remote export."""
    if arg and Path(arg).is_dir():
        return Path(arg), None
    if arg:
        return None, zipfile.ZipFile(arg)
    url = find_export_url()
    print("Downloading", url, file=sys.stderr)
    return None, zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(url).read()))


def rows(source, name):
    directory, zf = source
    f = open(directory / name, encoding="utf-8") if directory else io.TextIOWrapper(zf.open(name), encoding="utf-8")
    with f:
        header = f.readline().rstrip("\n").split("\t")
        for line in f:
            yield dict(zip(header, line.rstrip("\n").split("\t")))


def main():
    config = json.loads((ROOT / "config.json").read_text())
    regions = config["regions"] + config["bonus_regions"]
    source = open_export(sys.argv[1] if len(sys.argv) > 1 else None)

    # Regional championships, detected by name (the WCA export has no flag for them).
    region_re = re.compile(r"\b(%s) (?:(Quiet|PBQ|FMC|Speedcubing) )?(?:Championships?|All-?Stars) (?:- .*)?\d{4}$"
                           % "|".join(map(re.escape, regions)))
    comps = {}
    for r in rows(source, "WCA_export_competitions.tsv"):
        if r["country_id"] != "USA" or r["cancelled"] == "1":
            continue
        # Sponsors vary ("CubingUSA", "MCA", "GAN", "WCC", ...), so match region + "Championship" anywhere.
        m = region_re.search(r["name"])
        if not m:
            continue
        region, variant = m.group(1), m.group(2)
        end = date(int(r["end_year"]), int(r["end_month"]), int(r["end_day"]))
        comps[r["id"]] = {
            "id": r["id"], "name": r["name"], "region": region, "variant": variant,
            "main": variant not in ("Quiet", "PBQ", "FMC"),
            "year": end.year, "ended": end <= date.today(),
            "delegates": [d.strip() for d in DELEGATE_RE.findall(r["delegates"])],
        }

    names = {}
    for r in rows(source, "WCA_export_persons.tsv"):
        if r["sub_id"] == "1":
            names[r["wca_id"]] = r["name"]

    competed = defaultdict(set)  # person -> competition ids
    for r in rows(source, "WCA_export_results.tsv"):
        if r["competition_id"] in comps:
            competed[r["person_id"]].add(r["competition_id"])
    competitors = {names.get(p, p): c for p, c in competed.items()}

    delegating = defaultdict(set)
    for c in comps.values():
        for d in c["delegates"]:
            delegating[d].add(c["id"])

    def board(people, main_only, bonus_on):
        out = []
        for name, ids in people.items():
            ids = [i for i in ids if (comps[i]["main"] or not main_only)
                   and (bonus_on or comps[i]["region"] not in bonus)]
            if not ids:
                continue
            by_region = defaultdict(list)
            for i in sorted(ids, key=lambda i: (comps[i]["year"], comps[i]["name"])):
                by_region[comps[i]["region"]].append(comps[i]["name"])
            has_main = {comps[i]["region"] for i in ids if comps[i]["main"]}
            out.append({"name": name, "regions": set(by_region), "by_region": by_region, "total": len(ids),
                        "variant_only": set(by_region) - has_main})
        out.sort(key=lambda p: (-len(p["regions"]), -p["total"], p["name"]))
        rank, prev = 0, None
        for i, p in enumerate(out, 1):
            key = (len(p["regions"]), p["total"])
            if key != prev:
                rank, prev = i, key
            p["rank"] = rank
        return out

    bonus = set(config["bonus_regions"])
    boards = {
        f"v{int(variants_on)}n{int(bonus_on)}": {
            "regions": [r for r in regions if bonus_on or r not in bonus],
            "Competitors": board(competitors, not variants_on, bonus_on),
            "Delegates": board(delegating, not variants_on, bonus_on),
        }
        for variants_on in (True, False) for bonus_on in (True, False)
    }
    render(boards, bonus, config["me"], len(comps))


def render(boards, bonus, me, n_comps):
    def marks(p):
        out = ""
        if bonus & p["regions"]:
            out += '<abbr title="Includes the Northeast bonus point">*</abbr>'
        if p["variant_only"]:
            only = ", ".join(sorted(p["variant_only"]))
            out += f'<abbr title="Relies on a Quiet/PBQ/FMC championship, with no main championship, in: {html.escape(only)}">†</abbr>'
        return out

    def table(rows_, regions):
        shown = [p for p in rows_ if len(p["regions"]) >= 3][:50]
        shown += [p for p in rows_ if p["name"] == me and p not in shown]
        out = ['<table><thead><tr><th>#</th><th>Name</th><th>Regions</th>'
               + "".join(f'<th class="r"><span>{html.escape(r)}</span></th>' for r in regions)
               + "<th>Total</th></tr></thead><tbody>"]
        for p in shown:
            cls = ' class="me"' if p["name"] == me else ""
            cells = "".join(
                f'<td class="r on" title="{html.escape(chr(10).join(p["by_region"][r]), quote=True)}"></td>'
                if r in p["regions"] else '<td class="r"></td>' for r in regions)
            out.append(f'<tr{cls}><td>{p["rank"]}</td><td>{html.escape(p["name"])}</td>'
                       f'<td>{len(p["regions"])}{marks(p)}</td>{cells}<td>{p["total"]}</td></tr>')
        out.append("</tbody></table>")
        return "".join(out)

    sections = "".join(
        f'<div class="mode mode-{mode}">'
        + "".join(f"<section><h2>{k}</h2>{table(bs[k], bs['regions'])}</section>" for k in ("Competitors", "Delegates"))
        + "</div>"
        for mode, bs in boards.items())
    page = (ROOT / "template.html").read_text()
    page = (page.replace("{{sections}}", sections)
            .replace("{{updated}}", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
            .replace("{{count}}", str(n_comps)))
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(page)
    for mode, bs in boards.items():
        for k in ("Competitors", "Delegates"):
            v = bs[k]
            print(f"\n{mode} / {k}:", file=sys.stderr)
            for p in v[:3]:
                print(f"  {p['rank']:>3} {p['name']:<24} {len(p['regions'])}{'*' if bonus & p['regions'] else ''}{'†' if p['variant_only'] else ''} total {p['total']}", file=sys.stderr)
            me_row = next((p for p in v if p["name"] == me), None)
            if me_row:
                print(f"  me: {me_row['rank']} {len(me_row['regions'])}{'†' if me_row['variant_only'] else ''}", file=sys.stderr)

if __name__ == "__main__":
    main()
