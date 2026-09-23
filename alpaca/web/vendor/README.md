# Bundled interface assets

IBM Plex Sans Regular and Medium, and IBM Plex Mono Regular, are self-hosted from the IBM Plex project.
Source: https://github.com/IBM/plex (packages/plex-sans and packages/plex-mono, complete/woff2).
Retrieved 2026-09-22. Copyright and SIL Open Font License: OFL.txt.

The SVG paths in ../icons.js are original geometric interface icons. No third-party icon package or browser CDN is used.

`../countries-110m.json` is `countries-110m.json` from the world-atlas npm package, version 2.0.2
(https://github.com/topojson/world-atlas, ISC license, Copyright 2013-2019 Michael Bostock), built
from the Natural Earth 1:110m Admin 0 country boundaries, version 4.1.0 (public domain). It parses
to the same JSON as the file in that package; the bytes differ only in that one non-ASCII letter
(in a country name) is written as a `\u` escape and the file has no final newline. It feeds the
coastlines on the sign-in globe (`../vault-globe.js`).
