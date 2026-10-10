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

`thinking-orbs.js` is the engine of thinking-orbs 0.3.2 (https://github.com/Jakubantalik/Libraries.dev,
packages/thinking-orbs, MIT license, Copyright 2026 Jakub Antalik; demo at https://libraries.dev/orbs),
the `src/engine/` and `src/presets.ts` files with the TypeScript stripped and wrapped into one ES module.
The React component is not included. The file names its source commit and carries the license text.
`../orb.js` draws it as the `<alpaca-orb>` element that the hub uses for loading and working states.
Rebuild it with `setup/vendor_thinking_orbs.mjs` (usage in that file).

`arthur/` is the owner's Arthur web avatar package. Package 1 (Arthur-Web-Package 1.0.0, supplied by
the owner on 2026-10-08; its `assets/source-provenance.json` names the archive
`arthur-project-action-pack.zip`, sha256 448ec7238abe8abc56740af96b52abce58c232f5dd1516411ad1122863e4d419)
gave the 11 original actions. Package 2 (`Arthur-Web-Package-2.zip`, package.json version 2.0.0,
sha256 3ac2386646bf81f03d724a9f9104bec84bbeedf95d616d0becd50e63a30cc197, supplied by the owner on
2026-10-08) adds 21 actions and replaces the runtime. `setup/vendor_arthur.py` rebuilds the
directory from the unpacked package 2 (usage in that file). What it copies and what it converts:

- Copied byte for byte from package 2: `arthur-core.js` (the same bytes as package 1),
  `arthur-geometry.js` (new), `arthur-avatar.js` (package 2's: it loads arthur-geometry.js and accepts
  per-action source rectangles), `arthur-avatar.css` (the same bytes as package 1),
  `assets/arthur-spritesheet.png` (the gaze atlas; its image data recompressed, zlib level 9, the same filtered pixel bytes, sha256 96f87e0f2a61c0b160b6e3447c4d43f0f8b78a8de5c0b6137922aa21b57f11df, because its original compressed bytes matched a private-term rule of the release leak scan by chance) and the 11 original `assets/strips/*.png` (all
  byte-identical to package 1's). `arthur-host-controller.js` is not shipped: `../arthur.js` keeps
  driving Arthur with its own priority and reaction queue.
- Converted: the 21 new actions (sleeping, stamping, raising_hand, juggling, drinking_coffee,
  shield_up, counting_coins, packing_box, merging_papers, telescope, spit, sunglasses, nightcap,
  yawn_stretch, mimic_typing, dance, party_hat, headphones, detective, checklist, peekaboo). The
  package ships each as a full-size sheet (`assets/new-sheets`, about 25 MB together) and as six
  normalized 192 x 208 runtime frames (`assets/frames/<action>/00.png` to `05.png`). Only the frames are
  used (brief section 2e): the script pastes the six frames left to right into one transparent
  1152 x 208 strip, `assets/strips/<action>.png`, with the pixels unchanged (it reads every strip
  back and compares each frame), so a new action has the same shape as an original one. Checklist
  keeps the package's pose order [1,1,2,3,4,5]: its frames 00 and 01 are the same pose.
- `assets/avatar-manifest.js` is the package's own file, byte for byte. Its new actions still name
  the full-size sheets, which are not shipped; the Studio reads the manifest through
  `stripsManifest` in `alpaca/web/arthur.js`, which points each new action at its strip, keeps its
  frames, durations, loop flag, label and trigger, and drops the full-size sheet geometry
  (sourceSize, frameRects, destinationRect).

The directory weighs about 10.6 MB (`alpaca/tests/test_hub_arthur.py` holds it under 12 MB). The hub
serves it with `Cache-Control: no-cache` and a sha256 ETag, so a reload revalidates each file and an
unchanged one answers 304 (`../../serve.py`). The artwork is the owner's own and ships in every
project and release with no license line (owner, 2026-10-08). `../arthur.js` places Arthur in the hub
sidebar.
