// Runtime i18n: language registry, catalog loading with English fallback, and t().
// Zero dependencies, no build step — plain fetch() of static JSON under ./i18n/.
// en.json is the schema: every other catalog is a subset/superset of its keys, and any
// key missing from a loaded catalog (or a catalog that fails to load at all) falls back
// to the English string. That fallback must be silent — a translator's catalog landing
// mid-week should never throw or log in a visitor's console.

export const LANGS = [
  { code: "en", native: "English", en: "English" },
  { code: "es", native: "Español", en: "Spanish" },
  { code: "fr", native: "Français", en: "French" },
  { code: "de", native: "Deutsch", en: "German" },
  { code: "it", native: "Italiano", en: "Italian" },
  { code: "pt-BR", native: "Português (Brasil)", en: "Portuguese (Brazil)" },
  { code: "pt-PT", native: "Português (Portugal)", en: "Portuguese (Portugal)" },
  { code: "nl", native: "Nederlands", en: "Dutch" },
  { code: "ca", native: "Català", en: "Catalan" },
  { code: "tr", native: "Türkçe", en: "Turkish" },
  { code: "sv", native: "Svenska", en: "Swedish" },
  { code: "nb", native: "Norsk bokmål", en: "Norwegian Bokmål" },
  { code: "da", native: "Dansk", en: "Danish" },
  { code: "fi", native: "Suomi", en: "Finnish" },
  { code: "is", native: "Íslenska", en: "Icelandic" },
  { code: "et", native: "Eesti", en: "Estonian" },
  { code: "lv", native: "Latviešu", en: "Latvian" },
  { code: "lt", native: "Lietuvių", en: "Lithuanian" },
  { code: "pl", native: "Polski", en: "Polish" },
  { code: "cs", native: "Čeština", en: "Czech" },
  { code: "sk", native: "Slovenčina", en: "Slovak" },
  { code: "hu", native: "Magyar", en: "Hungarian" },
  { code: "ro", native: "Română", en: "Romanian" },
  { code: "bg", native: "Български", en: "Bulgarian" },
  { code: "el", native: "Ελληνικά", en: "Greek" },
  { code: "uk", native: "Українська", en: "Ukrainian" },
  { code: "ru", native: "Русский", en: "Russian" },
  { code: "hr", native: "Hrvatski", en: "Croatian" },
  { code: "sr", native: "Српски", en: "Serbian" },
  { code: "sl", native: "Slovenščina", en: "Slovenian" },
  { code: "ar", native: "العربية", en: "Arabic", rtl: true },
  { code: "he", native: "עברית", en: "Hebrew", rtl: true },
  { code: "fa", native: "فارسی", en: "Persian", rtl: true },
  { code: "ur", native: "اردو", en: "Urdu", rtl: true },
  { code: "hi", native: "हिन्दी", en: "Hindi" },
  { code: "bn", native: "বাংলা", en: "Bengali" },
  { code: "sw", native: "Kiswahili", en: "Swahili" },
  { code: "ja", native: "日本語", en: "Japanese" },
  { code: "ko", native: "한국어", en: "Korean" },
  { code: "zh-Hans", native: "简体中文", en: "Chinese (Simplified)" },
  { code: "zh-Hant", native: "繁體中文", en: "Chinese (Traditional)" },
  { code: "vi", native: "Tiếng Việt", en: "Vietnamese" },
  { code: "th", native: "ไทย", en: "Thai" },
  { code: "id", native: "Bahasa Indonesia", en: "Indonesian" },
  { code: "ms", native: "Bahasa Melayu", en: "Malay" },
  { code: "fil", native: "Filipino", en: "Filipino" },
];

const LANG_BY_CODE = new Map(LANGS.map((l) => [l.code, l]));
const RTL_CODES = new Set(LANGS.filter((l) => l.rtl).map((l) => l.code));

export function isRtl(code) {
  return RTL_CODES.has(code);
}

function langInfo(code) {
  return LANG_BY_CODE.get(code) || null;
}

const DEFAULT_LANG = "en";
let enCatalog = null; // always loaded, always present — the fallback of last resort
let activeCatalog = null; // === enCatalog when active language is "en"
let activeCode = DEFAULT_LANG;

// Memoizes every catalog this session ever successfully fetched (English included), keyed by
// code, so switching back to a language visited earlier in the session is instant with no
// re-fetch. A failed fetch is never memoized, so it's retried on the next attempt.
const catalogMemo = new Map();

async function fetchCatalog(code) {
  if (catalogMemo.has(code)) return catalogMemo.get(code);
  try {
    const res = await fetch(`./i18n/${code}.json`);
    if (!res.ok) return null;
    const catalog = await res.json();
    catalogMemo.set(code, catalog);
    return catalog;
  } catch {
    return null; // network error / bad JSON — degrade silently, caller falls back to English
  }
}

/** Load en.json once (idempotent) — the fallback catalog underlying every other language.
 * Only a successful load is cached; a failed fetch returns {} for that call but leaves
 * enCatalog unset so the next call retries instead of being stuck on an empty catalog
 * for the rest of the session. */
async function ensureEnglish() {
  if (enCatalog) return enCatalog;
  const catalog = await fetchCatalog(DEFAULT_LANG);
  if (catalog) enCatalog = catalog;
  return catalog || {};
}

// Monotonic sequencing for loadLang() calls: if a slower earlier call's awaits resolve
// after a newer call has already started, the older call must not overwrite
// activeCatalog/activeCode with stale data. Bump the token synchronously at the top of
// each call and only commit state if this call is still the latest when it finishes.
let loadLangSeq = 0;

/**
 * Load and activate a language. Always resolves (never rejects) — an unknown code, a 404,
 * or a network error all degrade to English with no console noise and no unhandled rejection.
 * Returns the resolved code that ended up active (useful if a caller wants to confirm the
 * catalog it asked for actually loaded vs. silently fell back). If a newer loadLang() call
 * starts before this one finishes, this one's state commit is skipped (it still resolves
 * with the code it would have activated, for callers that only care about that).
 */
export async function loadLang(code) {
  const mySeq = ++loadLangSeq;
  if (!code || code === DEFAULT_LANG || !LANG_BY_CODE.has(code)) {
    await ensureEnglish();
    if (mySeq === loadLangSeq) {
      activeCatalog = enCatalog;
      activeCode = DEFAULT_LANG;
    }
    return DEFAULT_LANG;
  }
  // English (the fallback every t() call needs) and the target catalog don't depend on each
  // other — fetch both in parallel instead of serializing two round-trips on every boot/switch.
  const [, catalog] = await Promise.all([ensureEnglish(), fetchCatalog(code)]);
  if (!catalog) {
    // Missing/broken catalog (translator hasn't landed it yet, or fetch failed) — fall back
    // to English but keep the *chosen* code as active so t() still tries per-key lookups
    // against an empty catalog first (all misses) and per-key-falls-back to English.
    if (mySeq === loadLangSeq) {
      activeCatalog = {};
      activeCode = code;
    }
    return DEFAULT_LANG;
  }
  if (mySeq === loadLangSeq) {
    activeCatalog = catalog;
    activeCode = code;
  }
  return code;
}

export function currentLangCode() {
  return activeCode;
}

// Tags real browsers still emit that don't map 1:1 onto a LANGS code: zh has no bare "zh"
// entry (browsers report region-qualified tags, so map the common regions explicitly rather
// than guessing at a default script), and a handful of legacy ISO 639 codes some browsers/OSes
// still send for languages whose code changed (iw/in are the old ISO codes for he/id, tl is
// the old code for fil, no is the macrolanguage code browsers send instead of nb).
const ALIASES = {
  "zh-cn": "zh-Hans", "zh-sg": "zh-Hans",
  "zh-tw": "zh-Hant", "zh-hk": "zh-Hant", "zh-mo": "zh-Hant",
  "zh": "zh-Hans",
  "iw": "he", "in": "id", "tl": "fil", "no": "nb",
};

/** Resolve one BCP-47-ish navigator tag (e.g. "pt-BR", "en-US", "zh-CN") to a supported LANGS
 * code via alias/exact match only — no prefix fallback. Shared by both passes of detectLang()
 * and by resolveTag()'s first step, so the alias table only lives in one place. */
function resolveExact(tag) {
  const lower = tag.toLowerCase();
  if (ALIASES[lower]) return ALIASES[lower];
  const exact = LANGS.find((l) => l.code.toLowerCase() === lower);
  return exact ? exact.code : null;
}

/** Resolve one BCP-47-ish navigator tag to a supported LANGS code: alias/exact match first,
 * then a prefix match (e.g. "en-CA" -> "en"), or null if nothing matches at all. */
function resolveTag(tag) {
  const exact = resolveExact(tag);
  if (exact) return exact;
  const prefix = tag.toLowerCase().split("-")[0];
  const byPrefix = LANGS.find((l) => l.code.toLowerCase().split("-")[0] === prefix);
  return byPrefix ? byPrefix.code : null;
}

/** Autodetect a supported language from navigator.languages: exact match first across the
 * whole preference list, then a prefix-match pass, then "en". Never throws. */
export function detectLang() {
  try {
    const prefs = navigator.languages && navigator.languages.length
      ? navigator.languages
      : (navigator.language ? [navigator.language] : []);
    for (const tag of prefs) {
      const m = resolveExact(tag);
      if (m) return m;
    }
    for (const tag of prefs) {
      const m = resolveTag(tag);
      if (m) return m;
    }
  } catch {
    // navigator access blocked or malformed — fall through to default
  }
  return DEFAULT_LANG;
}

/** {x} interpolation — params values are stringified and inserted verbatim (caller is
 * responsible for escaping before this ever touches innerHTML; see format.js's esc()). */
function interpolate(str, params) {
  if (!params) return str;
  return str.replace(/\{(\w+)\}/g, (m, key) => (
    Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : m
  ));
}

/** Translate `key`, interpolating `params`, falling back per-key to English, and finally to
 * the key itself if even English is missing it (should never happen against the real catalog). */
export function t(key, params) {
  const active = activeCatalog || {};
  const en = enCatalog || {};
  const raw = Object.prototype.hasOwnProperty.call(active, key) ? active[key]
    : Object.prototype.hasOwnProperty.call(en, key) ? en[key]
    : key;
  return interpolate(raw, params);
}
