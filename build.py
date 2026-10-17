#!/usr/bin/env python3
"""Build the static site: download the WCA TSV export and render dist/index.html."""
import html
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
EXPORT_PAGE = "https://www.worldcubeassociation.org/export/results"
LISTED_RE = re.compile(r"\[\{([^}]*)\}")


def find_export_url():
    page = urllib.request.urlopen(EXPORT_PAGE).read().decode()
    m = re.search(r'https://[^"]+\.tsv\.zip', page)
    if not m:
        sys.exit("Could not find TSV export link")
    return m.group(0)


def api(path):
    """GET from the WCA API, politely: throttled, and retrying on rate limits."""
    req = urllib.request.Request("https://www.worldcubeassociation.org/api/v0" + path,
                                 headers={"User-Agent": "wca-goals (github.com/joeyday/wca-goals)"})
    for attempt in range(6):
        time.sleep(0.5)
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 5:
                raise
            time.sleep(float(e.headers.get("Retry-After") or 5 * 2 ** attempt))


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
    "location": ("Credit by location",
                 "Each Northeast or original Western championship counts only for the current region its host state belongs to."),
    "current": ("Current regions only",
                "Only the nine current regions. Northeast and pre-2023 Western championships are ignored."),
    "bonus": ("Legacy regions as bonus points",
              "Northeast and the original Western each count as an extra region (11 in total)."),
    "both": ("Credit both successor regions",
             "A Northeast championship counts for New England and Mid-Atlantic. An original Western championship counts for Western and Rocky Mountain."),
    "pre": ("Pre-2023 map only",
            "Only championships through 2022, under the seven regions of the time."),
    "post": ("2023+ map only",
             "Only championships from 2023 on, under today's nine regions."),
}
ROLES = ("Competitors", "Delegates", "Organizers")
DEFAULT_MODE = next(iter(MODES))
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
            "year": end.year, "upcoming": end >= date.today(), "state": r["city_name"].rsplit(", ", 1)[-1],
            "legacy": region in legacy["regions"] and end.year <= last_year,
            "delegates": [d.strip() for d in LISTED_RE.findall(r["delegates"])],
            "organizers": [d.strip() for d in LISTED_RE.findall(r["organizers"])],
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

    # Registrations aren't in the export, so ask the WCA API about upcoming championships.
    # Failures just mean no yellow dots; they never break the build.
    planned = defaultdict(set)  # wca_id -> upcoming competition ids
    wca_ids, api_delegate_ids = {}, {}
    for c in comps.values():
        if not c["upcoming"]:
            continue
        try:
            # The export is a day old, so delegates added since then only show up in the API.
            live = api(f"/competitions/{c['id']}")
            c["delegates"] = [d["name"] for d in live["delegates"]]
            c["organizers"] = [d["name"] for d in live["organizers"]]
            api_delegate_ids.update({d["name"]: d["wca_id"] for d in live["delegates"] + live["organizers"] if d.get("wca_id")})
            for reg in api(f"/competitions/{c['id']}/registrations"):
                uid = reg["user_id"]
                if uid not in wca_ids:
                    wca_ids[uid] = api(f"/users/{uid}")["user"]["wca_id"]
                if wca_ids[uid]:
                    planned[wca_ids[uid]].add(c["id"])
        except Exception as e:
            print(f"Warning: could not load registrations for {c['id']}: {e}", file=sys.stderr)

    competed = defaultdict(set)  # person -> competition ids
    for r in rows(source, "WCA_export_results.tsv"):
        if r["competition_id"] in comps:
            competed[r["person_id"]].add(r["competition_id"])
    # Delegates are listed by name only, so link them only when the name is unambiguous.
    name_counts = defaultdict(list)
    for wca_id, n in names.items():
        name_counts[n].append(wca_id)
    ids_by_name = {n: ids[0] for n, ids in name_counts.items() if len(ids) == 1}
    ids_by_name.update(api_delegate_ids)

    delegating, organizing = defaultdict(set), defaultdict(set)
    for c in comps.values():
        for d in c["delegates"]:
            delegating[d].add(c["id"])
        for o in c["organizers"]:
            if o in ids_by_name:  # organizers can be groups ("Cube New England"); keep people only
                organizing[o].add(c["id"])

    def board(role, people, main_only, mode, hypo):
        out = []
        for key, ids in people.items():
            name, wca_id = (names.get(key, key), key) if role == "Competitors" else (key, ids_by_name.get(key))
            by_region, has_main, has_current, total = defaultdict(list), set(), set(), 0
            for i in sorted(ids, key=lambda i: (comps[i]["year"], comps[i]["name"])):
                c = comps[i]
                if role != "Competitors" and c["upcoming"]:
                    continue  # listings for championships that aren't over yet only count when hypothetical
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
            plan, upcoming = defaultdict(list), []
            future = planned.get(key, ()) if role == "Competitors" else [i for i in ids if comps[i]["upcoming"]]
            for i in sorted(future, key=lambda i: comps[i]["name"]):
                c = comps[i]
                targets = credit(c, mode) if c["main"] or not main_only else []
                upcoming.append((c, targets))
                for t in targets:
                    if t not in by_region:
                        plan[t].append(c["name"])
            if hypo:  # pretend every registration or listing for an unfinished championship counts
                for c, targets in upcoming:
                    total += bool(targets)
                    for t in targets:
                        by_region[t].append(c["name"])
                        if c["main"]:
                            has_main.add(t)
                        has_current.add(t)
            out.append({"name": name, "wca_id": wca_id, "regions": set(by_region), "by_region": by_region, "plan": plan, "plan_label": ("Registered, not yet attended:" if role == "Competitors" else f"Listed {role[:-1].lower()}, not over yet:"),
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
            for hypo in (False, True):
                boards[f"{'all' if variants_on else 'main'}-{mode}-{'hypo' if hypo else 'now'}"] = {
                    "regions": cols,
                    **{role: board(role, people, not variants_on, mode, hypo) for role, people in
                       (("Competitors", competed), ("Delegates", delegating), ("Organizers", organizing))},
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
        floor = min(3, len(rows_[0]["regions"])) if rows_ else 3  # organizer boards top out below 3 regions
        shown = [p for p in rows_ if len(p["regions"]) >= floor][:50]
        shown += [p for p in rows_ if p["name"] == me and p not in shown]
        out = ['<table><thead><tr><th>#</th><th>Name</th><th>Regions</th>'
               + "".join(f'<th class="r"><span>{html.escape(r)}</span></th>' for r in regions)
               + "<th>Total</th></tr></thead><tbody>"]
        for p in shown:
            cls = ' class="me"' if p["name"] == me else ""
            def cell(r):
                if r in p["plan"]:
                    tip = p["plan_label"] + "\n" + chr(10).join(p["plan"][r])
                    return f'<td class="r plan" title="{html.escape(tip, quote=True)}"></td>'
                if r in p["regions"]:
                    return f'<td class="r on" title="{html.escape(chr(10).join(p["by_region"][r]), quote=True)}"></td>'
                return '<td class="r"></td>'

            cells = "".join(map(cell, regions))
            out.append(f'<tr{cls}><td>{p["rank"]}</td><td>{person(p)}</td>'
                       f'<td>{len(p["regions"])}{marks(p)}</td>{cells}<td>{p["total"]}</td></tr>')
        out.append("</tbody></table>")
        return "".join(out)

    first = next(iter(boards))
    sections = "".join(
        f'<div class="view" data-key="{key}"{"" if key == first else " hidden"}>'
        + "".join(f"<section><h2>{k}</h2>{table(bs[k], bs['regions'])}</section>" for k in ROLES)
        + "</div>"
        for key, bs in boards.items())
    options = "".join(f'<option value="{k}">{html.escape(label)}</option>' for k, (label, _) in MODES.items())
    hints = "".join(f'<p class="sub hint" data-mode="{k}"{"" if k == DEFAULT_MODE else " hidden"}>{html.escape(h)}</p>'
                    for k, (_, h) in MODES.items())
    page = (ROOT / "template.html").read_text()
    page = (page.replace("{{sections}}", sections).replace("{{options}}", options).replace("{{hints}}", hints)
            .replace("{{updated}}", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
            .replace("{{count}}", str(n_comps)))
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(page)
    for key, bs in boards.items():
        if not key.startswith("all-") or not key.endswith("-now"):
            continue
        for k in ROLES:
            v = bs[k]
            print(f"\n{key} / {k} ({len(bs['regions'])} regions):", file=sys.stderr)
            for p in v[:3]:
                print(f"  {p['rank']:>3} {p['name']:<24} {len(p['regions'])}{'*' if p['legacy_only'] else ''}{'†' if p['variant_only'] else ''} total {p['total']}", file=sys.stderr)
            me_row = next((p for p in v if p["name"] == me), None)
            if me_row:
                print(f"  me: {me_row['rank']} {len(me_row['regions'])}{'*' if me_row['legacy_only'] else ''}", file=sys.stderr)


if __name__ == "__main__":
    main()
