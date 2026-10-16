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


# How legacy (pre-2023) championships are credited. See config.json "legacy".
MODES = {
    "current": ("Current regions only",
                "Only the nine current regions. Northeast and pre-2023 Western championships are ignored."),
    "bonus": ("Legacy regions as bonus points",
              "Northeast and the original Western each count as an extra region (11 in total)."),
    "both": ("Credit both successor regions",
             "A Northeast championship counts for New England and Mid-Atlantic. An original Western championship counts for Western and Rocky Mountain."),
    "location": ("Credit by location",
                 "Each Northeast or original Western championship counts only for the current region its host state belongs to."),
    "pre": ("Pre-2023 map only",
            "Only championships through 2022, under the seven regions of the time."),
    "post": ("2023+ map only",
             "Only championships from 2023 on, under today's nine regions."),
}
STAR_MODES = {"bonus", "both", "location"}


def main():
    config = json.loads((ROOT / "config.json").read_text())
    regions = config["regions"]
    legacy = config["legacy"]
    last_year = legacy["last_year"]
    legacy_labels = [v["label"] for v in legacy["regions"].values()]
    source = open_export(sys.argv[1] if len(sys.argv) > 1 else None)

    # Regional championships, detected by name (the WCA export has no flag for them).
    names_re = "|".join(map(re.escape, regions + list(legacy["regions"])))
    region_re = re.compile(r"\b(%s) (?:(Quiet|PBQ|FMC|Speedcubing) )?(?:Championships?|All-?Stars) (?:- .*)?\d{4}$" % names_re)
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
            "year": end.year, "state": r["city_name"].rsplit(", ", 1)[-1],
            "legacy": region in legacy["regions"] and end.year <= last_year,
            "delegates": [d.strip() for d in DELEGATE_RE.findall(r["delegates"])],
        }

    def credit(c, mode):
        """Regions a championship counts toward under a mode (empty list = not counted)."""
        old = legacy["regions"].get(c["region"]) if c["legacy"] else None
        if mode == "post":
            return [c["region"]] if c["year"] > last_year else []
        if mode == "pre":
            return [c["region"]] if c["year"] <= last_year else []
        if not old:
            return [c["region"]]
        if mode == "current":
            return []
        if mode == "bonus":
            return [old["label"]]
        if mode == "both":
            return sorted(set(old["by_state"].values()))
        if c["state"] not in old["by_state"]:
            sys.exit(f"No region mapping for {c['state']!r} ({c['name']}); add it to config.json")
        return [old["by_state"][c["state"]]]

    names = {}
    for r in rows(source, "WCA_export_persons.tsv"):
        if r["sub_id"] == "1":
            names[r["wca_id"]] = r["name"]

    competed = defaultdict(set)  # person -> competition ids
    for r in rows(source, "WCA_export_results.tsv"):
        if r["competition_id"] in comps:
            competed[r["person_id"]].add(r["competition_id"])
    # Delegates are listed by name only, so link them only when the name is unambiguous.
    name_counts = defaultdict(list)
    for wca_id, n in names.items():
        name_counts[n].append(wca_id)
    ids_by_name = {n: ids[0] for n, ids in name_counts.items() if len(ids) == 1}

    delegating = defaultdict(set)
    for c in comps.values():
        for d in c["delegates"]:
            delegating[d].add(c["id"])

    def board(people, main_only, mode):
        out = []
        for key, ids in people.items():
            name, wca_id = (names.get(key, key), key) if people is competed else (key, ids_by_name.get(key))
            by_region, has_main, has_current, total = defaultdict(list), set(), set(), 0
            for i in sorted(ids, key=lambda i: (comps[i]["year"], comps[i]["name"])):
                c = comps[i]
                targets = credit(c, mode) if c["main"] or not main_only else []
                total += bool(targets)
                for t in targets:
                    by_region[t].append(c["name"])
                    if c["main"]:
                        has_main.add(t)
                    if not c["legacy"]:
                        has_current.add(t)
            if not by_region:
                continue
            out.append({"name": name, "wca_id": wca_id, "regions": set(by_region), "by_region": by_region,
                        "total": total, "variant_only": set(by_region) - has_main,
                        "legacy_only": (set(by_region) - has_current) if mode in STAR_MODES else set()})
        out.sort(key=lambda p: (-len(p["regions"]), -p["total"], p["name"]))
        rank, prev = 0, None
        for i, p in enumerate(out, 1):
            key = (len(p["regions"]), p["total"])
            if key != prev:
                rank, prev = i, key
            p["rank"] = rank
        return out

    order = regions + legacy_labels
    boards = {}
    for variants_on in (True, False):
        for mode in MODES:
            used = {t for c in comps.values() for t in credit(c, mode)}
            cols = [r for r in order if r in used or (r in regions and mode != "pre")]
            boards[f"{'all' if variants_on else 'main'}-{mode}"] = {
                "regions": cols,
                "Competitors": board(competed, not variants_on, mode),
                "Delegates": board(delegating, not variants_on, mode),
            }
    render(boards, config["me"], len(comps))


def render(boards, me, n_comps):
    def marks(p):
        out = ""
        if p["legacy_only"]:
            only = ", ".join(sorted(p["legacy_only"]))
            out += f'<abbr title="Counts a region only through a pre-2023 Northeast/Western championship: {html.escape(only)}">*</abbr>'
        if p["variant_only"]:
            only = ", ".join(sorted(p["variant_only"]))
            out += f'<abbr title="Relies on a Quiet/PBQ/FMC championship, with no main championship, in: {html.escape(only)}">†</abbr>'
        return out

    def person(p):
        name = html.escape(p["name"])
        if not p["wca_id"]:
            return name
        return f'<a href="https://www.worldcubeassociation.org/persons/{p["wca_id"]}" target="_blank" rel="noopener">{name}</a>'

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
            out.append(f'<tr{cls}><td>{p["rank"]}</td><td>{person(p)}</td>'
                       f'<td>{len(p["regions"])}{marks(p)}</td>{cells}<td>{p["total"]}</td></tr>')
        out.append("</tbody></table>")
        return "".join(out)

    first = next(iter(boards))
    sections = "".join(
        f'<div class="view" data-key="{key}"{"" if key == first else " hidden"}>'
        + "".join(f"<section><h2>{k}</h2>{table(bs[k], bs['regions'])}</section>" for k in ("Competitors", "Delegates"))
        + "</div>"
        for key, bs in boards.items())
    options = "".join(f'<option value="{k}">{html.escape(label)}</option>' for k, (label, _) in MODES.items())
    hints = "".join(f'<p class="sub hint" data-mode="{k}"{"" if k == "current" else " hidden"}>{html.escape(h)}</p>'
                    for k, (_, h) in MODES.items())
    page = (ROOT / "template.html").read_text()
    page = (page.replace("{{sections}}", sections).replace("{{options}}", options).replace("{{hints}}", hints)
            .replace("{{updated}}", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
            .replace("{{count}}", str(n_comps)))
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(page)
    for key, bs in boards.items():
        if not key.startswith("all-"):
            continue
        for k in ("Competitors", "Delegates"):
            v = bs[k]
            print(f"\n{key} / {k} ({len(bs['regions'])} regions):", file=sys.stderr)
            for p in v[:3]:
                print(f"  {p['rank']:>3} {p['name']:<24} {len(p['regions'])}{'*' if p['legacy_only'] else ''}{'†' if p['variant_only'] else ''} total {p['total']}", file=sys.stderr)
            me_row = next((p for p in v if p["name"] == me), None)
            if me_row:
                print(f"  me: {me_row['rank']} {len(me_row['regions'])}{'*' if me_row['legacy_only'] else ''}", file=sys.stderr)


if __name__ == "__main__":
    main()
