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
  `arthur-geometry.js` (new), `arthur-avatar.css` (the same bytes as package 1). `arthur-avatar.js` is
  package 2's (it loads arthur-geometry.js and accepts per-action source rectangles) with one patch
  (t-595, `AVATAR_PATCH` in `setup/vendor_arthur.py`): it loads the idle strip before the avatar is
  ready and every other image the first time a draw needs it, and keeps the frame on screen until the
  image arrives; the package loaded all 33 images first, about 11 MB. The gaze atlas is a
  lossless WebP (t-595; `to_webp` checks the decoded pixels equal the PNG's); the strips stay PNG, the 11 original
  ones byte-identical to package 1's (Chrome decodes a WebP strip one level off on 693 edge pixels, which the
  Studio look baseline refuses). The atlas had its image data recompressed
  because its original bytes matched a private-term rule of the release leak scan by chance (the gaze frames'
  pixels, sha256 0748e95b7b1fe3dfd48686f9ddc5520b7bfa2fdeadbd0bb47d3f186773bd751a over their RGBA bytes in
  order, are the package's), and since t-595 it is cut to the full-width rows of the 16 gaze frames (1536 x 416, decoded 2.5 MB instead of 14 MB, which stalled Chrome's main thread over 100 ms at 6x CPU slowdown), because the avatar draws only those from it; `stripsManifest` in `../arthur.js` moves the frames up by the cut. Deviation (owner-accepted 2026-10-12, decision dec-48ee879e9201): Chrome decodes the WebP atlas at most one level off the PNG's on edge pixels of the gaze frames (about 18,700 pixels over the 16 frames), and the atlas is drawn only while the cursor moves. `arthur-host-controller.js` is not shipped: `../arthur.js` keeps
  driving Arthur with its own priority and reaction queue.
- Converted: the 21 new actions (sleeping, stamping, raising_hand, juggling, drinking_coffee,
  shield_up, counting_coins, packing_box, merging_papers, telescope, spit, sunglasses, nightcap,
  yawn_stretch, mimic_typing, dance, party_hat, headphones, detective, checklist, peekaboo). The
  package ships each as a full-size sheet (`assets/new-sheets`, about 25 MB together) and as six
  normalized 192 x 208 runtime frames (`assets/frames/<action>/00.png` to `05.png`). Only the frames are
  used: the script pastes the six frames left to right into one transparent
  1152 x 208 strip, `assets/strips/<action>.png`, with the pixels unchanged (it reads every strip
  back and compares each frame), so a new action has the same shape as an original one. Checklist
  keeps the package's pose order [1,1,2,3,4,5]: its frames 00 and 01 are the same pose.
- `assets/avatar-manifest.js` is the package's own file, byte for byte. Its new actions still name
  the full-size sheets, which are not shipped; the Studio reads the manifest through
  `stripsManifest` in `alpaca/web/arthur.js`, which points each new action at its strip, keeps its
  frames, durations, loop flag, label and trigger, and drops the full-size sheet geometry
  (sourceSize, frameRects, destinationRect).

The directory weighs about 8.3 MB (`alpaca/tests/test_hub_arthur.py` holds it under 12 MB). The hub
serves its images and scripts with `Cache-Control: max-age=86400` and a sha256 ETag, under URLs that
carry a content version (`?v=`, `arthur_version` in `../../serve.py`), so a visit within a day asks for
none of them and a changed file has a new URL; the stylesheet revalidates. The artwork is the owner's own and ships in every
project and release with no license line (owner, 2026-10-08). `../arthur.js` places Arthur in the hub
sidebar.
