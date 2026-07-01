/**
 * vplates-combinations.helper.js
 *
 * Playwright helper for https://vplates.com.au — dynamically discovers every
 * plate style on /browse-styles, reads each style's "Combination options"
 * patterns, and generates one valid plate combination per pattern.
 *
 * Drop this file into your Playwright project and use it from a test:
 *
 *   const { collectAllCombinations } = require('./vplates-combinations.helper');
 *
 *   test('try every style/pattern combination', async ({ page }) => {
 *     test.setTimeout(15 * 60 * 1000); // ~40 style pages, JS-hydrated site
 *
 *     const { entries, skipped } = await collectAllCombinations(page, { seed: 42 });
 *     console.log(`Generated ${entries.length} combinations, skipped:`, skipped);
 *
 *     for (const e of entries) {
 *       // e = { style, slug, url, pattern, from, to, combination, display }
 *       // e.combination -> raw characters, e.g. "1QF3XX" (what the entry form takes)
 *       // e.display     -> as printed on the plate, e.g. "1QF.3XX"
 *       // ...drive your plate-availability / checkout flow with e.combination
 *     }
 *   });
 *
 * ESM projects: change module.exports at the bottom to `export { ... }`.
 *
 * Nothing is hardcoded about the patterns themselves: every pattern is read
 * from the page at runtime and parsed generically as a "<FROM> to <TO>" range
 * (any leading label text is ignored). Letters, digits, fixed literals, and
 * separator dots are inferred per position from the scraped range. Styles
 * without a "Combination options" section are reported in `skipped`.
 */

'use strict';

const BASE_URL = 'https://vplates.com.au';

// Selectors verified against the live site on 2026-07-02.
const SEL = {
  styleLinks: 'a[href^="/browse-styles/"]',
  sectionTitle: 'h3.product-information__title',
  comboItems: 'li.product-information__data-item-combos-item',
};

const COMBINATION_SECTION_TITLE = /combination options/i;

/* ------------------------------------------------------------------ */
/* Random generation (seedable so failing runs can be reproduced)      */
/* ------------------------------------------------------------------ */

/** mulberry32 PRNG — returns a () => number in [0, 1). */
function makeRng(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Random integer in [min, max] inclusive. */
function randInt(rng, min, max) {
  return min + Math.floor(rng() * (max - min + 1));
}

/* ------------------------------------------------------------------ */
/* Pattern parsing & combination generation                            */
/* ------------------------------------------------------------------ */

/**
 * Extract a { from, to } range from a scraped pattern string, or null.
 * Generic: matches the trailing "<FROM> to <TO>" pair of plate tokens, so any
 * leading label text the site puts before the range is ignored.
 */
function parsePattern(raw) {
  if (!raw) return null;
  const m = raw.toUpperCase().match(/([A-Z0-9.]+)\s+TO\s+([A-Z0-9.]+)\s*$/);
  if (!m) return null;
  return { from: m[1], to: m[2] };
}

const isDigit = (c) => c >= '0' && c <= '9';
const isLetter = (c) => c >= 'A' && c <= 'Z';

/**
 * Generate one combination matching a raw pattern string.
 *
 * @param {string} raw  e.g. "AA.000 to ZZ.9999"
 * @param {() => number} [rng]  optional PRNG (defaults to Math.random)
 * @returns {{ raw, from, to, combination, display } | null}
 *   display     - with the plate's dot separators, e.g. "1QF.3XX"
 *   combination - separators stripped, e.g. "1QF3XX"
 */
function generateCombination(raw, rng = Math.random) {
  const parsed = parsePattern(raw);
  if (!parsed) return null;
  const { from, to } = parsed;

  let display;

  const fromDigits = from.replace(/\./g, '');
  const toDigits = to.replace(/\./g, '');
  if (/^\d+$/.test(fromDigits) && /^\d+$/.test(toDigits)) {
    // Pure numeric range (e.g. Heritage "100.000 to 285.000"). Pick a number
    // in [from, to] as a whole, then re-apply the FROM side's dot grouping —
    // per-character generation could overshoot the upper bound.
    const value = randInt(rng, Number(fromDigits), Number(toDigits));
    const digits = String(value).padStart(fromDigits.length, '0').split('');
    display = from
      .split('')
      .map((c) => (c === '.' ? '.' : digits.shift()))
      .join('');
  } else {
    // Template range. Use the FROM template's shape (the minimal valid length
    // when the two sides differ, e.g. "AAA to ZZZZZZ" -> 3 letters). Where
    // lengths match, honour per-position bounds from the TO side; a position
    // identical on both sides (Euro's "V", the dots) is a literal.
    const sameShape = from.length === to.length;
    display = from
      .split('')
      .map((fc, i) => {
        const tc = sameShape ? to[i] : null;
        if (fc === '.') return '.';
        if (tc !== null && fc === tc) return fc; // fixed literal
        if (isLetter(fc)) {
          const hi = tc !== null && isLetter(tc) ? tc : 'Z';
          return String.fromCharCode(randInt(rng, fc.charCodeAt(0), hi.charCodeAt(0)));
        }
        if (isDigit(fc)) {
          const hi = tc !== null && isDigit(tc) ? tc : '9';
          return String(randInt(rng, Number(fc), Number(hi)));
        }
        return fc;
      })
      .join('');
  }

  return { raw, from, to, combination: display.replace(/\./g, ''), display };
}

/* ------------------------------------------------------------------ */
/* Scraping                                                            */
/* ------------------------------------------------------------------ */

/**
 * Open /browse-styles and return every unique style page.
 * The listing is JS-hydrated and slow to load, hence the generous timeout.
 *
 * @param {import('@playwright/test').Page} page
 * @param {{ baseUrl?: string, timeout?: number }} [opts]
 * @returns {Promise<Array<{ name: string, slug: string, url: string }>>}
 */
async function getStyleLinks(page, opts = {}) {
  const baseUrl = opts.baseUrl || BASE_URL;
  const timeout = opts.timeout || 30000;

  await page.goto(`${baseUrl}/browse-styles`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector(SEL.styleLinks, { timeout });

  const hrefs = await page.$$eval(SEL.styleLinks, (anchors) =>
    anchors.map((a) => a.getAttribute('href'))
  );

  const slugs = [
    ...new Set(
      hrefs
        .filter((h) => h && h.startsWith('/browse-styles/'))
        .map((h) => h.replace('/browse-styles/', '').replace(/\/+$/, ''))
        .filter(Boolean)
    ),
  ];

  return slugs.map((slug) => ({
    slug,
    url: `${baseUrl}/browse-styles/${slug}`,
    name: slug
      .split('-')
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' '),
  }));
}

/**
 * Visit one style page and return its raw combination pattern strings
 * (deduped — some styles repeat their first pattern). Returns [] when the
 * style has no "Combination options" section (e.g. licensed styles).
 *
 * @param {import('@playwright/test').Page} page
 * @param {string} styleUrl
 * @param {{ comboTimeout?: number }} [opts]
 * @returns {Promise<string[]>}
 */
async function scrapeStylePatterns(page, styleUrl, opts = {}) {
  const comboTimeout = opts.comboTimeout || 15000;

  await page.goto(styleUrl, { waitUntil: 'domcontentloaded' });
  // Wait for hydration: section titles render once the page content is ready.
  await page.waitForSelector(SEL.sectionTitle, { timeout: comboTimeout }).catch(() => {});

  const sectionTitle = page
    .locator(SEL.sectionTitle)
    .filter({ hasText: COMBINATION_SECTION_TITLE })
    .first();
  if ((await sectionTitle.count()) === 0) return [];

  // The <li> items exist in the DOM even while collapsed, but clicking keeps
  // the flow faithful to a real user and future-proofs against lazy accordions.
  await sectionTitle.click({ timeout: 2000 }).catch(() => {});
  await page.waitForSelector(SEL.comboItems, { state: 'attached', timeout: comboTimeout });

  const items = await page.$$eval(SEL.comboItems, (lis) =>
    lis.map((li) => li.textContent.replace(/\s+/g, ' ').trim()).filter(Boolean)
  );
  return [...new Set(items)];
}

/**
 * Full pipeline: discover styles, scrape each style's patterns, and generate
 * one combination per pattern.
 *
 * @param {import('@playwright/test').Page} page
 * @param {{ baseUrl?: string, comboTimeout?: number, seed?: number }} [opts]
 * @returns {Promise<{
 *   entries: Array<{ style, slug, url, pattern, from, to, combination, display }>,
 *   skipped: Array<{ style, url, reason }>,
 * }>}
 */
async function collectAllCombinations(page, opts = {}) {
  const rng = makeRng(opts.seed !== undefined ? opts.seed : Date.now());
  const styles = await getStyleLinks(page, opts);

  const entries = [];
  const skipped = [];

  for (const style of styles) {
    let patterns;
    try {
      patterns = await scrapeStylePatterns(page, style.url, opts);
    } catch (err) {
      skipped.push({ style: style.name, url: style.url, reason: `scrape failed: ${err.message}` });
      continue;
    }

    if (patterns.length === 0) {
      skipped.push({ style: style.name, url: style.url, reason: 'no "Combination options" section' });
      continue;
    }

    for (const pattern of patterns) {
      const gen = generateCombination(pattern, rng);
      if (!gen) {
        skipped.push({ style: style.name, url: style.url, reason: `unparseable pattern: "${pattern}"` });
        continue;
      }
      entries.push({
        style: style.name,
        slug: style.slug,
        url: style.url,
        pattern: gen.raw,
        from: gen.from,
        to: gen.to,
        combination: gen.combination,
        display: gen.display,
      });
    }
  }

  return { entries, skipped };
}

module.exports = {
  BASE_URL,
  SEL,
  makeRng,
  parsePattern,
  generateCombination,
  getStyleLinks,
  scrapeStylePatterns,
  collectAllCombinations,
};
