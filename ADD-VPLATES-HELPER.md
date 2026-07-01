# Add the vplates combinations helper to a Playwright project

> Usable two ways: follow it yourself as a README, or paste this whole file as a
> prompt to an AI coding assistant working inside your e2e repo (include the
> helper file `vplates-combinations.helper.js` alongside it).

## Goal

Integrate `vplates-combinations.helper.js` into an existing Playwright e2e
project so a test can:

1. Open `https://vplates.com.au/browse-styles` (JS-hydrated — content loads a
   few seconds after navigation; custom plates are the slowest).
2. Discover every plate style dynamically (no style list is hardcoded).
3. Visit each style page and read its "Combination options" patterns from the
   live DOM (no patterns are hardcoded).
4. Generate one valid combination per pattern and try each of them in the test.

## Step 1 — Copy the helper

Copy `vplates-combinations.helper.js` unchanged into the project, e.g.:

```
e2e/
  helpers/
    vplates-combinations.helper.js
  tests/
    vplates-combinations.spec.js   <- created in step 2
```

It is self-contained CommonJS with zero dependencies beyond the Playwright
`page` object passed into it. For an ESM/TypeScript project, change the
`module.exports = { ... }` at the bottom of the file to `export { ... }`.

## Step 2 — Create the spec

Create `tests/vplates-combinations.spec.js`:

```js
const { test, expect } = require('@playwright/test');
const { collectAllCombinations } = require('../helpers/vplates-combinations.helper');

test.describe('vplates — combinations per style', () => {
  test('generate and try one combination per pattern of every style', async ({ page }) => {
    // ~40 style pages on a slow, JS-hydrated site: give it room.
    test.setTimeout(15 * 60 * 1000);

    const { entries, skipped } = await collectAllCombinations(page, {
      seed: 42,          // fixed seed => reproducible combinations across runs
      comboTimeout: 20000,
    });

    console.log(`Generated ${entries.length} combinations`);
    for (const s of skipped) console.log(`skipped: ${s.style} — ${s.reason}`);

    expect(entries.length).toBeGreaterThan(0);

    for (const e of entries) {
      // e.style       -> "Custom", "Euro", "Heritage", ...
      // e.slug        -> "custom"
      // e.url         -> style page URL
      // e.pattern     -> raw pattern text scraped from the page
      // e.combination -> characters to type into the entry form, e.g. "1QF3XX"
      // e.display     -> as printed on the plate, e.g. "1QF.3XX"

      // TODO: replace with your project's flow for trying a combination,
      // e.g. open the plate checker, fill e.combination, assert the result.
      await tryCombination(page, e);
    }
  });
});

async function tryCombination(page, entry) {
  // Implement using your project's existing page objects / fixtures.
  console.log(`${entry.style}: ${entry.pattern} -> ${entry.combination}`);
}
```

If the project uses TypeScript, name it `.spec.ts` and add types via JSDoc or
`any` on the returned entries; the helper's JSDoc gives editor hints as-is.

## Step 3 — Run it headed first

```bash
npx playwright test vplates-combinations --headed --project=chromium
```

vplates.com.au sits behind bot protection (plain HTTP fetch returns 403), so a
real browser context is required. If the site challenges headless Chromium,
run headed or set a realistic user agent in `playwright.config`:

```js
use: {
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
}
```

## Helper API (for reference)

| Export | Signature | Purpose |
|---|---|---|
| `collectAllCombinations` | `(page, opts?) => { entries, skipped }` | Full pipeline: discover styles → scrape patterns → one combination per pattern. |
| `getStyleLinks` | `(page, opts?) => [{ name, slug, url }]` | Deduped style pages from `/browse-styles`. |
| `scrapeStylePatterns` | `(page, styleUrl, opts?) => string[]` | Raw pattern strings for one style (`[]` if the style has no combination section). |
| `generateCombination` | `(rawPattern, rng?) => { raw, from, to, combination, display } \| null` | Pure function, no browser needed. |
| `makeRng` | `(seed) => () => number` | Seedable PRNG for reproducible runs. |

Options for `collectAllCombinations`: `baseUrl` (default `https://vplates.com.au`),
`comboTimeout` (ms per style page, default 15000), `seed` (default `Date.now()`).

## Behavior notes

- Nothing about styles or patterns is hardcoded: both are read from the live
  DOM each run. Patterns are parsed generically as a trailing `<FROM> to <TO>`
  range — leading label text (like "6 Digit") is ignored, fixed literals and
  dot separators are inferred per position, and pure-numeric ranges are drawn
  as whole numbers so they never exceed the upper bound.
- Styles without a "Combination options" section (some licensed styles) and any
  unparseable pattern land in the returned `skipped` array instead of failing
  the run — assert on `skipped` if you want the test to flag site changes.
- Only three selectors couple the helper to the site (constants at the top of
  the file, verified 2026-07-02): the style links anchor, the section title
  `h3`, and the combo `li` items. If the site redesigns, update those first.
