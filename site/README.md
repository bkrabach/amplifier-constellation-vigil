# Product page

The introduction to Vigil lives at <https://bkrabach.github.io/amplifier-constellation-vigil/>.
It says what Vigil adds to an agent, how a turn goes, where Vigil fits among the
Constellation building blocks, and how to start. The README and
[the design](../docs/DESIGN.md) remain the documents that define what is available.

The page follows the Amplifier Constellation
[map guide](https://github.com/microsoft/amplifier-constellation/blob/main/docs/ecosystem-maps.md)
and uses that site's colours and type, so the two read as one family. Vigil is an
independent project; the page says so and does not present itself as part of that site.

## What is written by hand and what is generated

| File | Source |
| --- | --- |
| `site/index.html`, `site/style.css`, `site/assets/` | Written here. The illustration is inline SVG. |
| "Where it fits" summary, role line and relationship list in the page | Filled in by `site/build.py` from `docs/ecosystem-map.json`. |
| `docs/ecosystem-map.svg`, `docs/product/` | Generated from `docs/ecosystem-map.json` by the Constellation renderer. Published under `ecosystem/`. |

`docs/ecosystem-map.json` is the one record of Vigil's role, parts and relationships.
Edit it, never the generated SVG or page.

## Build and preview

Python 3.10 or newer; nothing to install:

```sh
python3 site/build.py --out _site
python3 -m http.server --directory _site 8000
```

Open `http://localhost:8000`. The build fills the generated parts of the page, checks
every local reference and anchor, and refuses to build if the two copies of the map
differ or the record is not public. `tests/test_site.py` runs the same build.

The `Product page` workflow builds pull requests and publishes changes from `main` with
GitHub Pages (publishing source: GitHub Actions). Only `_site` is deployed.

## When Vigil's role, parts or relationships change

1. Edit `docs/ecosystem-map.json`. Cite the revision you reviewed and keep proposals
   apart from what exists.
2. Regenerate from a checkout of `microsoft/amplifier-constellation`:

   ```sh
   python3 scripts/render_ecosystem_maps.py --registry /path/to/vigil/docs/ecosystem-map.json \
     --local-only --staging /tmp/vigil-map
   cp -r /tmp/vigil-map/amplifier-constellation-vigil/docs/. /path/to/vigil/docs/
   python3 scripts/render_ecosystem_maps.py --check-local /path/to/vigil
   ```

3. Update the "Where this fits" block in the README and any sentence on the page that
   the change makes untrue, then build and look at the page at desktop and phone widths.

## What the page may claim

The illustration explains an idea. It is not a screenshot and not evidence of a
deployed capability. The map shows scope; it does not certify conformance with a
contract or compatibility with every ACP agent. Keep limits on the page next to the
promises, and keep private deployment details, hostnames and review notes out of it.

To refresh the link-preview image, render the page's illustration at 1200×630 and save
it as `site/assets/social-card.png`.
