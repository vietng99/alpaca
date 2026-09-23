# Third-party notices

Alpaca is released under the MIT license in `LICENSE` (Copyright (c) 2026 Alpaca contributors).
This file lists every file in the tree that comes from somewhere else, with its upstream, its
license and the path of the license text that travels with it. Every entry is compatible with
distribution under MIT. A file not listed here is part of Alpaca under `LICENSE`.

## Vendored or third-party files

| files | what | upstream | license | license text |
|---|---|---|---|---|
| `alpaca/wiki/**` | the embedded wiki engine (bitemporal assertion-log memory store) | the Rune-2 template (no public URL recorded) | MIT, Copyright (c) 2026 Rune-2 template contributors | `alpaca/wiki/LICENSE` |
| `alpaca/tests/wiki/**` | the wiki engine's tests, adapted to the vendored surface | the Rune-2 template suite | MIT (same notice) | `alpaca/wiki/LICENSE` |
| `plugin/alpaca/skills/caveman/**` | the caveman writing skills (seven sub-skills and two helper scripts; `caveman-compress/scripts/check.py` is a local adaptation) | caveman by Julius Brussee | MIT, Copyright (c) 2026 Julius Brussee | `plugin/alpaca/skills/caveman/LICENSE` |
| `plugin/alpaca/skills/humanizer/**` | the humanizer writing skill | humanizer by Siqi Chen | MIT, Copyright (c) 2025 Siqi Chen | `plugin/alpaca/skills/humanizer/LICENSE` |
| `plugin/alpaca/skills/eli5/**` | the eli5 explanation skill | eli5 (upstream URL not recorded in the source) | MIT, Copyright (c) 2026 (no holder named in the upstream notice) | `plugin/alpaca/skills/eli5/LICENSE` |
| `plugin/alpaca/skills/i-have-adhd/**`, `plugin/alpaca/hooks/adhd-always-on.sh` | the i-have-adhd skill and its always-on hook (flattened from `i-have-adhd/hooks/always-on.sh`) | i-have-adhd by Ayoub Ghriss | MIT, Copyright (c) 2026 Ayoub Ghriss | `plugin/alpaca/skills/i-have-adhd/LICENSE` |
| `alpaca/web/vendor/plex-sans.woff2` | IBM Plex Sans Regular, version 3.005 (self-hosted web font) | https://github.com/IBM/plex | SIL Open Font License 1.1, Copyright 2018 IBM Corp. | `alpaca/web/vendor/OFL.txt` |
| `alpaca/web/vendor/plex-sans-medium.woff2` | IBM Plex Sans Medium, version 3.005 (self-hosted web font) | https://github.com/IBM/plex | SIL Open Font License 1.1, Copyright 2018 IBM Corp. | `alpaca/web/vendor/OFL.txt` |
| `alpaca/web/vendor/plex-mono.woff2` | IBM Plex Mono Regular, version 2.005 (self-hosted web font) | https://github.com/IBM/plex | SIL Open Font License 1.1, Copyright 2017 IBM Corp. | `alpaca/web/vendor/OFL.txt` |
| `alpaca/web/countries-110m.json` | coastline data for the sign-in globe (TopoJSON, 177 countries plus land) | `countries-110m.json` of the world-atlas npm package 2.0.2, https://github.com/topojson/world-atlas, built from Natural Earth 1:110m Admin 0 country boundaries 4.1.0, https://www.naturalearthdata.com | world-atlas: ISC, Copyright 2013-2019 Michael Bostock; the Natural Earth data: public domain | `alpaca/web/vendor/world-atlas-LICENSE.txt` |

How the three font and one data entries were established: each font file's own name table
carries its copyright (name ID 0) and the sentence "This Font Software is licensed under the SIL
Open Font License, Version 1.1" (name ID 13); `alpaca/web/vendor/OFL.txt` is that license with
IBM's reserved font name "Plex". `countries-110m.json` parses to exactly the same JSON as
`countries-110m.json` in the world-atlas 2.0.2 package (the package's `package.json` names the ISC
license and its README names Natural Earth 4.1.0 as the source); the bytes differ only in that
one non-ASCII letter (in a country name) is written as a `\u` escape and the file has no final
newline.

`plugin/alpaca/BUNDLE-MANIFEST.json` records, for every bundled skill file, its upstream path, its
sha256 over the bytes in this tree, and its license status.

## The owner's own skills (part of Alpaca, MIT)

These bundled skills, their support files and their hooks are the owner's own work and are part
of Alpaca under `LICENSE`:

| skill | files |
|---|---|
| nuclear | `plugin/alpaca/skills/nuclear/**` |
| napalm | `plugin/alpaca/skills/napalm/**` |
| autodrive | `plugin/alpaca/skills/autodrive/**`, `plugin/alpaca/hooks/autodrive-*.sh` |
| timebomb | `plugin/alpaca/skills/timebomb/**` |
| sam | `plugin/alpaca/skills/sam/**`, `plugin/alpaca/hooks/sam-*`, `style/presets/**` |
| sang | `plugin/alpaca/skills/sang/**` |
| spear | `plugin/alpaca/skills/spear/**` |
| capture | `plugin/alpaca/skills/capture/**` |
| flare | `plugin/alpaca/skills/flare/**` |
| vsys | `plugin/alpaca/skills/vsys/**` |
| html-safe | `plugin/alpaca/rules/html-safe/**`, `plugin/alpaca/hooks/html-safe-always-on.sh` |

## The sign-in and hub UI (part of Alpaca, MIT)

The design of the sign-in page and the workspace hub (the rotating world globe, the relay feed,
the binary rings around the code field, the tactical backdrop and the unlock sequence) was
contributed by the project owner and is part of Alpaca under `LICENSE` (MIT). Its files:

| files | what |
|---|---|
| `alpaca/web/login.html` | the sign-in page |
| `alpaca/web/workspaces.html` | the workspace hub page |
| `alpaca/web/vault.css` | the styles of both pages |
| `alpaca/web/vault.js` | the behaviour of both pages: rings, relay feed, backdrop, unlock sequence, hub tiles |
| `alpaca/web/vault-globe.js` | the globe: coastlines, graticule, starfield, satellites, moon and comet; it marks no place |

The fonts and the coastline data these pages load are third-party files, listed in the table above.

## Dependencies installed at setup (not in this tree)

`setup/bootstrap.sh` installs the Python packages in `requirements.txt` into a local virtual
environment: PyYAML (MIT), pytest (MIT), html5lib (MIT) and, on Python older than 3.11, tomli
(MIT). They carry their own notices and are not part of this tree.
