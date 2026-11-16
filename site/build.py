#!/usr/bin/env python3
"""Assemble the Vigil product page and its ecosystem map page into one static site.

The introduction in `site/index.html` is written by hand. Its "Where it fits" facts, the
map, and the page under `ecosystem/` all come from `docs/ecosystem-map.json`, so the page,
the README and the map cannot drift apart silently.
"""
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
import argparse
import json
import shutil

SOURCE = Path(__file__).resolve().parent
REPOSITORY = SOURCE.parent
PUBLIC_URL = "https://bkrabach.github.io/amplifier-constellation-vigil/"


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            if values["id"] in self.ids:
                raise ValueError(f"Duplicate HTML id: {values['id']}")
            self.ids.add(values["id"])
        self.refs.extend(values[key] for key in ("src", "href", "poster") if values.get(key))


def validate(folder, page, root):
    """Every local reference must exist inside the site; every in-page anchor must exist."""
    parser = References()
    parser.feed(page)
    for ref in parser.refs:
        url = urlsplit(ref)
        if url.scheme or url.netloc:
            continue
        if not url.path:
            if url.fragment and url.fragment not in parser.ids:
                raise ValueError(f"Missing anchor: {ref}")
            continue
        path = (folder / unquote(url.path)).resolve()
        if not path.is_relative_to(root) or not path.exists():
            raise ValueError(f"Missing or escaping local reference: {ref}")


def where_it_fits(record):
    """The same sentences the ecosystem map page shows, from the same record."""
    nodes = {node["id"]: node for node in record["nodes"]}
    relations = "\n".join(
        "        <li>" + escape(nodes[edge["from"]]["label"]) + " <strong>" + escape(edge["relation"]) + "</strong> "
        + escape(nodes[edge["to"]]["label"]) + (" (optional)" if edge.get("optional") else "") + ".</li>"
        for edge in record["edges"])
    roles = ("Outward roles: " + ", ".join(record["roles"]) + ". ") if record["roles"] else "Supporting component. "
    if record["supports_roles"]:
        roles += "Supports: " + ", ".join(record["supports_roles"]) + "."
    return {"@@SUMMARY@@": escape(record["summary"]), "@@ROLE_SUMMARY@@": escape(roles), "@@RELATIONS@@": relations}


def build(destination):
    destination = destination.resolve()
    if destination == SOURCE or SOURCE.is_relative_to(destination) or destination.is_relative_to(SOURCE):
        raise ValueError("Build into a separate output directory.")
    record = json.loads((REPOSITORY / "docs/ecosystem-map.json").read_text())
    if record.get("visibility") != "public":
        raise ValueError("Only a public record may be published.")
    if record.get("product_site", {}).get("url") != PUBLIC_URL:
        raise ValueError("The record's product_site must be this site's address.")
    product = REPOSITORY / "docs/product"
    if (product / "map.svg").read_bytes() != (REPOSITORY / "docs/ecosystem-map.svg").read_bytes():
        raise ValueError("docs/product/map.svg and docs/ecosystem-map.svg differ; regenerate both from the record.")

    destination.mkdir(parents=True, exist_ok=True)
    page = (SOURCE / "index.html").read_text()
    for placeholder, value in where_it_fits(record).items():
        if placeholder not in page:
            raise ValueError(f"The page no longer has {placeholder}.")
        page = page.replace(placeholder, value)
    if "@@" in page:
        raise ValueError("Unresolved template placeholder.")
    (destination / "index.html").write_text(page)
    shutil.copyfile(SOURCE / "style.css", destination / "style.css")
    shutil.copytree(SOURCE / "assets", destination / "assets", dirs_exist_ok=True)
    shutil.copytree(product, destination / "ecosystem", dirs_exist_ok=True)
    (destination / ".nojekyll").write_text("")
    for html in sorted(destination.rglob("*.html")):
        validate(html.parent, html.read_text(), destination)
    print(f"Built the product page and the ecosystem map page in {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    build(parser.parse_args().out)
