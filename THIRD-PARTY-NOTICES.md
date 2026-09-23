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

## Spec tools (vendored at pinned versions, `vendor/`)

`alpaca spec init` installs one of these into a project from the copies below, offline. The pins
(repository, tag, commit, and the sha256 of every vendored archive) are in `vendor/VENDOR.json`;
`alpaca spec verify` checks them, and `setup/vendor_spec_kits.py` rebuilds the directory. Each tool
is used as its upstream ships it; Alpaca does not copy either format into its own.

| files | what | upstream | version | license | license text |
|---|---|---|---|---|---|
| `vendor/spec-kit/speckit-1.0.10-claude-sh.tar.gz` | the output of spec-kit's `specify init` for Claude Code (skills under `.claude/skills/speckit-*`, the `.specify/` templates, scripts and memory), generated once from the pinned release | https://github.com/github/spec-kit (tag `v1.0.10`, commit `b5d97b41a3ad703800179eab0e711c1d7173422e`) | 1.0.10 | MIT, Copyright GitHub, Inc. | `vendor/spec-kit/LICENSE` |
| `vendor/openspec/npm/*.tgz` | the OpenSpec CLI, its Claude Code skills and `/opsx` commands (npm package `@fission-ai/openspec`), plus its production dependencies listed below, as published on the npm registry | https://github.com/Fission-AI/OpenSpec (tag `v1.13.1`, commit `634c557bd0470eec37861b46172c3f503d283c1b`) | 1.13.1 | MIT, Copyright (c) 2024 OpenSpec Contributors | `vendor/openspec/LICENSE` |

`vendor/spec-kit/constitution.md` is Alpaca's own file (MIT, `LICENSE`): the install puts it in place
of spec-kit's constitution so it points at `CLAUDE.md` and `doctrine/` instead of adding a second
rulebook. `bin/openspec` (the launcher) and `alpaca/spec_kits.py` are Alpaca's own.

### OpenSpec and its production dependencies

Every package below is unpacked by `bin/openspec` into `.alpaca/tools/` on first use. Each license
is MIT-compatible (MIT, ISC or BSD-3-Clause) and its full text travels inside the package tarball at
the member named in the last column. Mail addresses and web links in the copyright lines are left
out here; the license files hold them. Each tarball is the registry's file, except `iconv-lite`,
which is stored compressed at another gzip level; `vendor/VENDOR.json` records for every package the
registry integrity and the sha256 of the tar inside, which is the same for both.

| package | version | license | copyright line | license text |
|---|---|---|---|---|
| `@fission-ai/openspec` | 1.13.1 | MIT | Copyright (c) 2024 OpenSpec Contributors | `package/LICENSE` in `vendor/openspec/npm/fission-ai-openspec-1.13.1.tgz` |
| `@inquirer/ansi` | 2.0.8 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-ansi-2.0.8.tgz` |
| `@inquirer/checkbox` | 5.2.5 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-checkbox-5.2.5.tgz` |
| `@inquirer/confirm` | 6.3.2 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-confirm-6.3.2.tgz` |
| `@inquirer/core` | 11.2.1 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-core-11.2.1.tgz` |
| `@inquirer/core` | 12.0.3 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-core-12.0.3.tgz` |
| `@inquirer/editor` | 5.3.3 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-editor-5.3.3.tgz` |
| `@inquirer/expand` | 5.1.5 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-expand-5.1.5.tgz` |
| `@inquirer/external-editor` | 3.0.5 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-external-editor-3.0.5.tgz` |
| `@inquirer/figures` | 2.0.9 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-figures-2.0.9.tgz` |
| `@inquirer/input` | 5.1.6 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-input-5.1.6.tgz` |
| `@inquirer/number` | 4.2.3 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-number-4.2.3.tgz` |
| `@inquirer/password` | 5.2.2 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-password-5.2.2.tgz` |
| `@inquirer/prompts` | 8.7.2 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-prompts-8.7.2.tgz` |
| `@inquirer/rawlist` | 5.3.5 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-rawlist-5.3.5.tgz` |
| `@inquirer/search` | 4.3.3 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-search-4.3.3.tgz` |
| `@inquirer/select` | 5.2.5 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-select-5.2.5.tgz` |
| `@inquirer/type` | 4.1.1 | MIT | Copyright (c) 2025 Simon Boudrias | `package/LICENSE` in `vendor/openspec/npm/inquirer-type-4.1.1.tgz` |
| `@nodelib/fs.scandir` | 2.1.5 | MIT | Copyright (c) Denis Malinochkin | `package/LICENSE` in `vendor/openspec/npm/nodelib-fs.scandir-2.1.5.tgz` |
| `@nodelib/fs.stat` | 2.0.5 | MIT | Copyright (c) Denis Malinochkin | `package/LICENSE` in `vendor/openspec/npm/nodelib-fs.stat-2.0.5.tgz` |
| `@nodelib/fs.walk` | 1.2.8 | MIT | Copyright (c) Denis Malinochkin | `package/LICENSE` in `vendor/openspec/npm/nodelib-fs.walk-1.2.8.tgz` |
| `ansi-regex` | 6.3.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/ansi-regex-6.3.0.tgz` |
| `braces` | 3.0.3 | MIT | Copyright (c) 2014-present, Jon Schlinkert | `package/LICENSE` in `vendor/openspec/npm/braces-3.0.3.tgz` |
| `chalk` | 5.6.2 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/chalk-5.6.2.tgz` |
| `chardet` | 2.2.0 | MIT | Copyright (C) 2024 Dmitry Shirokov | `package/LICENSE` in `vendor/openspec/npm/chardet-2.2.0.tgz` |
| `cli-cursor` | 5.0.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/cli-cursor-5.0.0.tgz` |
| `cli-spinners` | 3.4.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/cli-spinners-3.4.0.tgz` |
| `cli-width` | 4.1.0 | ISC | Copyright (c) 2015, Ilya Radchenko | `package/LICENSE` in `vendor/openspec/npm/cli-width-4.1.0.tgz` |
| `commander` | 14.0.3 | MIT | Copyright (c) 2011 TJ Holowaychuk | `package/LICENSE` in `vendor/openspec/npm/commander-14.0.3.tgz` |
| `cross-spawn` | 7.0.6 | MIT | Copyright (c) 2018 Made With MOXY Lda | `package/LICENSE` in `vendor/openspec/npm/cross-spawn-7.0.6.tgz` |
| `diff` | 9.0.0 | BSD-3-Clause | Copyright (c) 2009-2015, Kevin Decker | `package/LICENSE` in `vendor/openspec/npm/diff-9.0.0.tgz` |
| `fast-glob` | 3.3.3 | MIT | Copyright (c) Denis Malinochkin | `package/LICENSE` in `vendor/openspec/npm/fast-glob-3.3.3.tgz` |
| `fast-string-truncated-width` | 3.0.3 | MIT | Copyright (c) 2024-present Fabio Spampinato | `package/license` in `vendor/openspec/npm/fast-string-truncated-width-3.0.3.tgz` |
| `fast-string-width` | 3.0.2 | MIT | Copyright (c) 2024-present Fabio Spampinato | `package/license` in `vendor/openspec/npm/fast-string-width-3.0.2.tgz` |
| `fast-wrap-ansi` | 0.2.2 | MIT | Copyright (c) 2025 James Garbutt | `package/LICENSE` in `vendor/openspec/npm/fast-wrap-ansi-0.2.2.tgz` |
| `fastq` | 1.20.3 | ISC | Copyright (c) 2015-2020, Matteo Collina | `package/LICENSE` in `vendor/openspec/npm/fastq-1.20.3.tgz` |
| `fill-range` | 7.1.1 | MIT | Copyright (c) 2014-present, Jon Schlinkert | `package/LICENSE` in `vendor/openspec/npm/fill-range-7.1.1.tgz` |
| `get-east-asian-width` | 1.7.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/get-east-asian-width-1.7.0.tgz` |
| `glob-parent` | 5.1.2 | ISC | Copyright (c) 2015, 2019 Elan Shanker | `package/LICENSE` in `vendor/openspec/npm/glob-parent-5.1.2.tgz` |
| `iconv-lite` | 0.7.3 | MIT | Copyright (c) 2011 Alexander Shtuchkin | `package/LICENSE` in `vendor/openspec/npm/iconv-lite-0.7.3.tgz` |
| `is-extglob` | 2.1.1 | MIT | Copyright (c) 2014-2016, Jon Schlinkert | `package/LICENSE` in `vendor/openspec/npm/is-extglob-2.1.1.tgz` |
| `is-glob` | 4.0.3 | MIT | Copyright (c) 2014-2017, Jon Schlinkert | `package/LICENSE` in `vendor/openspec/npm/is-glob-4.0.3.tgz` |
| `is-interactive` | 2.0.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/is-interactive-2.0.0.tgz` |
| `is-number` | 7.0.0 | MIT | Copyright (c) 2014-present, Jon Schlinkert | `package/LICENSE` in `vendor/openspec/npm/is-number-7.0.0.tgz` |
| `is-unicode-supported` | 2.1.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/is-unicode-supported-2.1.0.tgz` |
| `isexe` | 2.0.0 | ISC | Copyright (c) Isaac Z. Schlueter and Contributors | `package/LICENSE` in `vendor/openspec/npm/isexe-2.0.0.tgz` |
| `log-symbols` | 7.0.1 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/log-symbols-7.0.1.tgz` |
| `merge2` | 1.4.1 | MIT | Copyright (c) 2014-2020 Teambition | `package/LICENSE` in `vendor/openspec/npm/merge2-1.4.1.tgz` |
| `micromatch` | 4.0.8 | MIT | Copyright (c) 2014-present, Jon Schlinkert | `package/LICENSE` in `vendor/openspec/npm/micromatch-4.0.8.tgz` |
| `mimic-function` | 5.0.1 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/mimic-function-5.0.1.tgz` |
| `mute-stream` | 3.0.0 | ISC | Copyright (c) Isaac Z. Schlueter and Contributors | `package/LICENSE` in `vendor/openspec/npm/mute-stream-3.0.0.tgz` |
| `onetime` | 7.0.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/onetime-7.0.0.tgz` |
| `ora` | 9.4.1 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/ora-9.4.1.tgz` |
| `path-key` | 3.1.1 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/path-key-3.1.1.tgz` |
| `picomatch` | 2.3.2 | MIT | Copyright (c) 2017-present, Jon Schlinkert | `package/LICENSE` in `vendor/openspec/npm/picomatch-2.3.2.tgz` |
| `queue-microtask` | 1.2.3 | MIT | Copyright (c) Feross Aboukhadijeh | `package/LICENSE` in `vendor/openspec/npm/queue-microtask-1.2.3.tgz` |
| `restore-cursor` | 5.1.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/restore-cursor-5.1.0.tgz` |
| `reusify` | 1.1.0 | MIT | Copyright (c) 2015-2024 Matteo Collina | `package/LICENSE` in `vendor/openspec/npm/reusify-1.1.0.tgz` |
| `run-parallel` | 1.2.0 | MIT | Copyright (c) Feross Aboukhadijeh | `package/LICENSE` in `vendor/openspec/npm/run-parallel-1.2.0.tgz` |
| `safer-buffer` | 2.1.2 | MIT | Copyright (c) 2018 Nikita Skovoroda | `package/LICENSE` in `vendor/openspec/npm/safer-buffer-2.1.2.tgz` |
| `shebang-command` | 2.0.0 | MIT | Copyright (c) Kevin Mårtensson | `package/license` in `vendor/openspec/npm/shebang-command-2.0.0.tgz` |
| `shebang-regex` | 3.0.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/shebang-regex-3.0.0.tgz` |
| `signal-exit` | 4.1.0 | ISC | Copyright (c) 2015-2023 Benjamin Coe, Isaac Z. Schlueter, and Contributors | `package/LICENSE.txt` in `vendor/openspec/npm/signal-exit-4.1.0.tgz` |
| `stdin-discarder` | 0.3.2 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/stdin-discarder-0.3.2.tgz` |
| `string-width` | 8.2.2 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/string-width-8.2.2.tgz` |
| `strip-ansi` | 7.2.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/strip-ansi-7.2.0.tgz` |
| `to-regex-range` | 5.0.1 | MIT | Copyright (c) 2015-present, Jon Schlinkert | `package/LICENSE` in `vendor/openspec/npm/to-regex-range-5.0.1.tgz` |
| `which` | 2.0.2 | ISC | Copyright (c) Isaac Z. Schlueter and Contributors | `package/LICENSE` in `vendor/openspec/npm/which-2.0.2.tgz` |
| `yaml` | 2.9.1 | ISC | Copyright Eemeli Aro | `package/LICENSE` in `vendor/openspec/npm/yaml-2.9.1.tgz` |
| `yoctocolors` | 2.2.0 | MIT | Copyright (c) Sindre Sorhus | `package/license` in `vendor/openspec/npm/yoctocolors-2.2.0.tgz` |
| `zod` | 4.6.5 | MIT | Copyright (c) 2025 Colin McDonnell | `package/LICENSE` in `vendor/openspec/npm/zod-4.6.5.tgz` |

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

OpenSpec (`bin/openspec`, `alpaca spec init --kit openspec`) runs on the host's `node`, version
20.19.0 or later, which Alpaca does not ship. spec-kit needs no runtime beyond `bash` and `git`.
