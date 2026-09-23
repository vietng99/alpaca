HTML PHONE-SAFE MODE is active. It applies whenever you author, edit, or publish an HTML file or an Artifact page, in any project.

Keep the entire file body pure ASCII. Do NOT emit literal non-ASCII glyphs. Use the ASCII-safe form instead:

- In HTML text: HTML entities. em dash `&mdash;`, en dash `&ndash;`, ellipsis `&hellip;`, middot `&middot;`, arrows `&rarr;` `&larr;` `&uarr;` `&darr;`, section `&sect;`, bullet `&bull;`, fractions `&frac12;` `&frac14;`, checks `&check;` `&#10007;`, non-breaking space `&nbsp;`, decorative glyphs by numeric ref e.g. `&#9685;` (half-circle), `&#9662;` (down triangle). Use straight quotes `"` `'`, never curly.
- In CSS `content:` strings: CSS unicode escapes, never HTML entities. down triangle `content:"\0025BE"`, section `content:"\0000A7"`.
- Always include `<meta charset="utf-8">` as the first element in `<head>`, plus `<meta name="viewport" content="width=device-width, initial-scale=1">`.

THEME &mdash; every HTML file MUST support both light and dark mode, with a visible toggle:

- Define the full palette as CSS custom properties on bare `:root` (this is the light theme). Style every element through those tokens &mdash; never hard-code a color that only works in one theme.
- Redefine only the tokens for dark under `@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){ ... } }`, and again under `:root[data-theme="dark"]{ ... }` so an explicit toggle wins in both directions.
- `body` must set an explicit `background` from a token (a transparent body borrows the host page's theme and breaks).
- Include a small toggle button plus a few lines of JS that flip `data-theme` on the root element and remember the choice in `localStorage`. Default (no stored choice) follows the OS via the media query.
- Respect `prefers-reduced-motion`; keep contrast legible in BOTH themes (do not just invert).

Final gate before handing any HTML to the user: run `grep -oP '[^\x00-\x7F]' <file> | wc -l` and it MUST print `0`. If not, convert the offenders (`perl -CSD -pi -e 's/\x{2014}/&mdash;/g; ...'` for text; hand-edit CSS `content:`). Also confirm the theme toggle works in both directions.

Why this exists: the user opens HTML on a phone, often via `file://`, where the browser ignores the charset meta and decodes as Latin-1, turning every multi-byte character into mojibake and making the file unreadable. HTML entities and CSS escapes are plain ASCII bytes, so they render correctly regardless of how the phone guesses the encoding.

Turn off for good by deleting the marker file named at session start.
