// Renders the recommendation card + option list into the results panel. The panel itself
// isn't aria-live (that's #sr-status); a user-initiated render moves focus to the panel so
// screen readers land on and read the new content instead of staying on the search field.
import { esc, fmtMoney, fmtH, fmtCo2, modeIcon, modeLabel, statusLabel } from "./format.js";
import { t, currentLangCode } from "./i18n.js";

const panel = () => document.getElementById("results");

/** Show a hidden panel for real: flip `hidden` off, force a reflow so the .show transition
 * actually animates from the pre-transition state, then add .show. Without the reflow the
 * browser coalesces both class changes into one paint and the transition never fires - this
 * was previously dead code (the reveal never actually ran). */
function reveal(el) {
  if (el.hidden) {
    el.hidden = false;
    void el.offsetWidth; // eslint-disable-line no-unused-expressions -- force layout, see above
  }
  el.classList.add("show");
}

function weatherChip(w) {
  if (!w) return "";
  const u = esc(w.units || "°");
  const feels = w.feels != null
    ? " <span class=\"wx-feels\">" + esc(t("wx.feels", { temp: w.feels, units: w.units || "°" })) + "</span>"
    : "";
  const fc = w.forecast
    ? "<div class=\"wx-fc\">" + esc(w.forecast.emoji) + " " + esc(w.forecast.temp) + esc(w.forecast.units)
      + " &middot; " + esc(w.forecast.date) + "<br>" + esc(w.forecast.desc || "") + "</div>"
    : (w.forecast_note ? "<div class=\"wx-fc\">" + esc(w.forecast_note) + "</div>" : "");
  // the weather glyph is the one emoji left in the product - it arrives in the server
  // payload, not authored in UI code, so it's out of scope for the icon-sprite swap.
  return "<div class=\"wx\">"
    + "<span class=\"wx-ico\" aria-hidden=\"true\">" + esc(w.emoji || "\u{1F321}️") + "</span>"
    + "<div class=\"wx-main\"><div class=\"wx-temp\">" + (w.temp != null ? esc(w.temp) + u : "") + feels + "</div>"
    + "<div class=\"wx-desc\">" + esc(w.desc || "") + (w.place ? " &middot; " + esc(w.place) : "") + "</div></div>"
    + fc + "</div>";
}

function legLabel(l) {
  return "<span class=\"leg\">" + modeIcon(l.mode) + " "
    + "<span class=\"sr-only\">" + esc(modeLabel(l.mode)) + "</span>" + fmtMoney(l.cost) + "</span>";
}

// String.replace(token, replacement) treats a *string* replacement specially - "$'", "$&",
// "$`", "$$", and "$<name>" are all $-pattern substitutions, not literal text. A real-world
// name containing one of those sequences (e.g. a place or option name with "$&" in it) would
// silently corrupt the output. Passing a replacer *function* instead disables all of that:
// the returned value is inserted verbatim, no matter what it contains.
function spliceToken(escapedTemplate, token, htmlFragment) {
  return escapedTemplate.replace(token, () => htmlFragment);
}

function destDescription(data, placeLabel) {
  const c = placeLabel || data.dest.city;
  return "<bdi dir=\"ltr\">" + esc(data.origin.iata) + " <svg class=\"icon\" aria-hidden=\"true\"><use href=\"#i-arrow\"/></svg> "
    + esc(data.dest.iata) + "</bdi>" + (c ? " (" + esc(c) + ")" : "");
}

// rec.flyInto is "Fly into {hub}, then {mode} the rest." - escape the translated template
// first, then splice a <strong>-wrapped, independently-escaped hub name in for the {hub}
// token and the (already-escaped) mode label in for {mode}. The catalog string never reaches
// innerHTML unescaped; the tokens are substituted with pre-built, pre-escaped fragments only
// after that escape pass runs.
const HUB_TOKEN = "@@HUB@@";
const MODE_TOKEN = "@@MODE@@";
function flyIntoLine(hub, mode) {
  const escapedHub = esc(hub);
  const escapedMode = esc(mode);
  const template = esc(t("rec.flyInto", { hub: HUB_TOKEN, mode: MODE_TOKEN }));
  const withHub = spliceToken(template, HUB_TOKEN, "<strong>" + escapedHub + "</strong>");
  return spliceToken(withHub, MODE_TOKEN, escapedMode);
}

function recommendationCard(R, rec, isDirect) {
  let heroValue, heroLabel, subline, breakeven = "";

  if (isDirect) {
    heroValue = t("rec.flyDirect");
    heroLabel = t("rec.noBeat", { money: fmtMoney(R.threshold) });
    subline = esc(t("rec.directBest"));
  } else if (rec.dominant) {
    heroValue = fmtMoney(rec.savings_vs_baseline);
    heroLabel = t("rec.cheaperNoSlower");
    subline = flyIntoLine(rec.name.split(" + ")[0], modeLabel(rec.legs[1]?.mode))
      + " " + esc(t("rec.cleanWin"));
  } else {
    heroValue = fmtMoney(rec.savings_vs_baseline);
    heroLabel = t("rec.savedVs", { money: fmtMoney(R.threshold) });
    subline = flyIntoLine(rec.name.split(" + ")[0], modeLabel(rec.legs[1]?.mode));
    if (rec.extra_hours_vs_baseline > 0 && rec.breakeven_vot != null) {
      breakeven = esc(t("rec.adds", { hours: fmtH(rec.extra_hours_vs_baseline), money: fmtMoney(rec.breakeven_vot) }));
      if (R.vot != null) {
        const delta = rec.savings_vs_baseline - R.vot * rec.extra_hours_vs_baseline;
        const key = delta >= 0 ? "rec.atRateAhead" : "rec.atRateBehind";
        breakeven += " " + esc(t(key, { money: fmtMoney(R.vot), diff: fmtMoney(Math.abs(delta)) }));
      }
    } else if (rec.extra_hours_vs_baseline <= 0) {
      breakeven = esc(t("rec.noSlower"));
    }
  }

  const routeLine = isDirect
    ? modeIcon("fly") + " " + esc(t("rec.flyDirectTo", { dest: rec.name.replace("Fly direct to ", "") }))
    : modeIcon("fly") + "<svg class=\"icon icon--arrow\" aria-hidden=\"true\"><use href=\"#i-arrow\"/></svg>" + modeIcon(rec.legs[1]?.mode) + " " + esc(rec.name);

  return "\n"
    + "    <div class=\"rec-card " + (isDirect ? "rec-card--direct" : "rec-card--split") + "\">\n"
    + "      <p class=\"rec-route\">" + routeLine + "</p>\n"
    + "      <p class=\"rec-hero\"><span class=\"rec-hero-value\">" + esc(heroValue) + "</span>\n"
    + "        <span class=\"rec-hero-label\">" + esc(heroLabel) + "</span></p>\n"
    + "      <p class=\"rec-sub\">" + subline + "</p>\n"
    + (breakeven ? "      <p class=\"rec-breakeven\">" + breakeven + "</p>\n" : "")
    + "    </div>";
}

/** One leg of an option's itinerary - real airport identity, a clock schedule (example or
 * live), the airport-arrival buffer for a flight leg, per-leg price + provenance, and a
 * one-click verify link. verify_url always opens in a new tab: it's a hop off the app to a
 * third-party site, never something that should navigate the plan away. */
function itineraryLegRow(leg) {
  const tag = leg.is_live
    ? "<span class=\"tag tag--ok\">" + esc(t("itin.liveTag")) + "</span>"
    : "<span class=\"tag tag--base\">" + esc(t("opt.est")) + "</span>";
  const carrier = leg.carrier
    ? " <span class=\"itin-carrier\">" + esc(leg.carrier)
      + (leg.flight_number ? " " + esc(leg.flight_number) : "") + "</span>"
    : "";
  // "ASE - Aspen" not "ASE - Aspen, Aspen": small airports often have name == city.
  const apLabel = (a) => esc(a.iata) + " — " + esc(a.name)
    + (a.city && a.city !== a.name ? ", " + esc(a.city) : "");
  const fromLabel = apLabel(leg.from);
  const toLabel = apLabel(leg.to);
  const checkin = leg.checkin_by
    ? "<div class=\"itin-checkin\">" + esc(t("itin.checkinBy",
        { day: leg.checkin_by.day, clock: leg.checkin_by.clock })) + "</div>\n"
    : "";
  return "\n"
    + "      <li class=\"itin-leg\">\n"
    + "        <div class=\"itin-leg-route\">" + modeIcon(leg.mode)
    + " <span class=\"sr-only\">" + esc(modeLabel(leg.mode)) + "</span> "
    + "<bdi dir=\"ltr\">" + fromLabel
    + " <svg class=\"icon icon--arrow\" aria-hidden=\"true\"><use href=\"#i-arrow\"/></svg> "
    + toLabel + "</bdi> " + tag + carrier + "</div>\n"
    + "        <div class=\"itin-leg-time\"><bdi dir=\"ltr\">" + esc(leg.depart_day) + " "
    + esc(leg.depart_clock) + " <svg class=\"icon icon--arrow\" aria-hidden=\"true\">"
    + "<use href=\"#i-arrow\"/></svg> " + esc(leg.arrive_day) + " " + esc(leg.arrive_clock)
    + "</bdi> &middot; " + esc(fmtH(leg.duration_h)) + "</div>\n"
    + checkin
    + "        <div class=\"itin-leg-price\">" + fmtMoney(leg.cost) + " &middot; "
    + esc(leg.price_basis) + "</div>\n"
    + "        <a class=\"itin-leg-verify\" href=\"" + esc(leg.verify_url)
    + "\" target=\"_blank\" rel=\"noopener noreferrer\">" + esc(t("itin.verify"))
    + " <svg class=\"icon\" aria-hidden=\"true\"><use href=\"#i-link\"/></svg></a>\n"
    + "      </li>";
}

/** Collapsible itinerary block for one option - <details> so a list of several options doesn't
 * force every leg's worth of text on screen before anyone asks for it. */
function itineraryBlock(o) {
  const itin = o.itinerary;
  if (!itin || !itin.legs || !itin.legs.length) return "";
  const noteKey = itin.example_day ? "itin.example" : "itin.live";
  return "\n"
    + "      <details class=\"itin\">\n"
    + "        <summary>" + esc(t("itin.summary")) + "</summary>\n"
    + "        <p class=\"itin-note\">" + esc(t(noteKey)) + "</p>\n"
    + "        <ol class=\"itin-legs\">" + itin.legs.map(itineraryLegRow).join("") + "</ol>\n"
    + "      </details>";
}

function optionRow(o, recName, greenestName) {
  const { text: statusText, tone } = statusLabel(o.status);
  const legs = o.legs.map(legLabel).join(" + ");
  const sign = o.savings_vs_baseline > 0 ? "pos" : (o.savings_vs_baseline < 0 ? "neg" : "zero");
  const savingsText = o.savings_vs_baseline > 0
    ? t("opt.saves", { money: fmtMoney(o.savings_vs_baseline) })
    : (o.savings_vs_baseline < 0 ? t("opt.more", { money: fmtMoney(-o.savings_vs_baseline) }) : t("opt.baseline"));
  const isGreenest = greenestName != null && o.name === greenestName;
  // "greenest" is a plain-text tag, not a color swap - same accessible pattern the cost/status
  // tags already use, so it reads fine with no color perception at all.
  const co2Line = o.co2e_kg != null
    ? "<span class=\"opt-co2" + (isGreenest ? " opt-co2--greenest" : "") + "\">"
      + (isGreenest ? "<svg class=\"icon\" aria-hidden=\"true\"><use href=\"#i-leaf\"/></svg> " : "") + fmtCo2(o.co2e_kg) + " " + esc(t("opt.est"))
      + (isGreenest ? " <span class=\"tag tag--ok\">" + esc(t("opt.greenest")) + "</span>" : "") + "</span>"
    : "";
  return "\n"
    + "    <li class=\"opt " + (o.name === recName ? "opt--win" : "") + "\">\n"
    + "      <div class=\"opt-top\">\n"
    + "        <span class=\"opt-name\" dir=\"auto\">" + esc(o.name) + "</span>\n"
    + "        <span class=\"opt-price\">" + fmtMoney(o.cost) + "</span>\n"
    + "      </div>\n"
    + "      <div class=\"opt-meta\">\n"
    + "        <span>" + legs + "</span>\n"
    + "        <span>" + fmtH(o.hours_eff) + (o.is_split ? " &middot; " + esc(o.buffer_h ? t("opt.buffer") : t("opt.multimodal")) : "") + "</span>\n"
    + "      </div>\n"
    + "      <div class=\"opt-meta\">\n"
    + "        <span class=\"opt-saves\" data-sign=\"" + sign + "\">" + esc(savingsText) + "</span>\n"
    + "        <span class=\"tag tag--" + tone + "\">" + (o.name === recName ? "★ " : "") + esc(statusText) + "</span>\n"
    + "      </div>\n"
    + (co2Line ? "      <div class=\"opt-meta opt-meta--co2\">" + co2Line + "</div>\n" : "")
    + itineraryBlock(o)
    + "\n    </li>";
}

// Mobile-only bottom-sheet expand/collapse (see the #results/.sheet-toggle rules in
// styles.css) - everything past the hero recommendation card (why-greenest-differs note,
// weather, the full option list, caveats) is one tap away instead of fighting the map for
// space by default. No-op wrapper element on desktop: the floating panel there already shows
// everything at once, so .sheet-toggle just stays display:none per the CSS.
// aria-expanded/label are derived from #results' own .results-expanded class, the single
// source of truth - reread here rather than tracked in a second module-level flag, so a fresh
// renderPlan() (a new plan, or a language-switch re-render of the same one) always reflects
// whatever the visitor last chose instead of silently resetting it.
function sheetToggleButton(optionCount) {
  const expanded = panel().classList.contains("results-expanded");
  return "\n"
    + "    <button type=\"button\" id=\"sheet-toggle\" class=\"btn btn--ghost btn--sm sheet-toggle\" "
    + "aria-expanded=\"" + expanded + "\" aria-controls=\"opt-list\">\n"
    + "      <svg class=\"icon\" aria-hidden=\"true\"><use href=\"#i-arrow\"/></svg> "
    + esc(expanded ? t("results.sheetCollapse") : t("results.sheetExpand", { count: optionCount })) + "\n"
    + "    </button>";
}

/** Flip the bottom sheet between its peek height (just the hero card) and full height (the
 * complete option list) - a cheap in-place class/label swap, not a re-render, so it can't
 * disturb scroll position or steal focus the way rebuilding the whole panel would. */
export function toggleSheet() {
  const el = panel();
  const expanded = el.classList.toggle("results-expanded");
  const btn = document.getElementById("sheet-toggle");
  if (!btn) return;
  btn.setAttribute("aria-expanded", String(expanded));
  const count = document.querySelectorAll("#opt-list > .opt").length;
  btn.innerHTML = "<svg class=\"icon\" aria-hidden=\"true\"><use href=\"#i-arrow\"/></svg> "
    + esc(expanded ? t("results.sheetCollapse") : t("results.sheetExpand", { count }));
}

// ------------------------------------------------------------------- cheapest-day strip
// One chip per candidate date in the sweep window (api.js's fetchDates), the cheapest one
// marked, every chip carrying the basis its price came from. It mounts between the
// recommendation card and the sheet toggle because it is the second headline answer - "and
// here is the cheapest DAY" - so it has to sit inside the mobile sheet's collapsed peek
// height, not buried below the option list.
//
// renderPlan() only emits the empty mount; the render functions below fill it separately once
// the sweep lands, so the plan itself never waits on a call that prices the whole window.

const stripEl = () => document.getElementById("date-strip");

// Formatters are rebuilt only when the language actually changes - one Intl.DateTimeFormat
// per chip per render would be the expensive way to do this. timeZone:"UTC" is mandatory, not
// tidiness: a YYYY-MM-DD is parsed as UTC midnight, so anywhere west of Greenwich a local-time
// formatter renders every chip as the day before.
let _fmtLang = null;
let _chipFmt = null;
let _longFmt = null;
function dateFormatters() {
  const lang = currentLangCode();
  if (lang !== _fmtLang || !_chipFmt) {
    const build = (opts) => {
      try {
        return new Intl.DateTimeFormat(lang, { ...opts, timeZone: "UTC" });
      } catch {
        // an unexpected tag should degrade to the browser default, never throw out of a render
        return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: "UTC" });
      }
    };
    _chipFmt = build({ weekday: "short", month: "short", day: "numeric" });
    _longFmt = build({ weekday: "long", year: "numeric", month: "long", day: "numeric" });
    _fmtLang = lang;
  }
  return { chip: _chipFmt, long: _longFmt };
}

function isoToUtcDate(iso) {
  const [y, m, d] = String(iso).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

/** Today in the visitor's own timezone as YYYY-MM-DD - the same floor app.js puts on the date
 * inputs, and string-comparable against a row's `date` because both are zero-padded ISO. */
function localTodayIso() {
  const now = new Date();
  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
}

/** One date as a pickable chip: the day, its price, and where that price came from.
 *
 * A date that could not be priced still gets a chip, so the window doesn't silently change
 * shape, but not one anyone can press: no data-date for the delegated handler to match, and
 * disabled so it never takes focus as an actionable control. */
function dateChip(row, best, activeDate) {
  const { chip, long } = dateFormatters();
  const when = isoToUtcDate(row.date);
  const dayLabel = "<span class=\"date-chip-day\"><bdi>" + esc(chip.format(when)) + "</bdi></span>";

  if (!row.ok) {
    return "\n        <li class=\"date-strip-item\">"
      + "<button type=\"button\" class=\"date-chip date-chip--failed\" disabled>"
      + dayLabel
      + "<span class=\"date-chip-price\">" + esc(t("dates.rowFailed")) + "</span>"
      + "</button></li>";
  }

  const isActive = row.date === activeDate;
  const isBest = best != null && row.date === best.date;
  const cls = "date-chip" + (isBest ? " date-chip--best" : "") + (isActive ? " date-chip--active" : "");
  // The basis tag is not decoration: a modelled number and a real fare look identical without
  // it, and on the static build the whole window is modelled. Same tone vocabulary the
  // itinerary legs already use (live reads ok, an estimate reads neutral), with "mixed"
  // warn-toned because a part-live price is the one that can quietly mislead.
  const tone = { live: "ok", mixed: "warn" }[row.basis] || "base";
  const basisTag = "<span class=\"tag tag--" + tone + "\">"
    + esc(t("dates.basis." + row.basis)) + "</span>";
  // Both tags share one row: the winner is then a wider chip rather than a taller one, and the
  // strip keeps its whole height inside the mobile sheet's peek instead of clipping the very
  // marker it exists to show.
  const bestTag = isBest
    ? "<span class=\"tag tag--ok\">" + esc(t("dates.cheapest")) + "</span>"
    : "";
  return "\n        <li class=\"date-strip-item\">"
    + "<button type=\"button\" class=\"" + cls + "\" data-date=\"" + esc(row.date) + "\""
    + " aria-pressed=\"" + String(isActive) + "\""
    + " aria-label=\"" + esc(t("dates.pickAria", { date: long.format(when) })) + "\">"
    + dayLabel
    + "<span class=\"date-chip-price\"><bdi dir=\"ltr\">" + fmtMoney(row.cost) + "</bdi></span>"
    + "<span class=\"date-chip-tags\">" + basisTag + bestTag + "</span>"
    + "</button></li>";
}

function fillStrip(html) {
  const el = stripEl();
  if (!el) return; // the panel is showing an error/empty/loading state, not a plan
  el.innerHTML = html;
  el.hidden = false;
}

export function clearDateStrip() {
  const el = stripEl();
  if (!el) return;
  el.innerHTML = "";
  el.hidden = true;
}

export function renderDateStripLoading() {
  fillStrip("\n      <p class=\"date-strip-note\">" + esc(t("dates.loading")) + "</p>\n    ");
}

export function renderDateStripError() {
  fillStrip("\n      <p class=\"date-strip-note date-strip-note--err\">"
    + esc(t("dates.failed")) + "</p>\n    ");
}

/**
 * Render a finished sweep. `activeDate` is whatever #date currently holds, which is the chip
 * that gets aria-pressed. Returns { summary } - the same sentence shown under the strip, for
 * app.js to push through announce() - or null when there was nothing worth showing.
 */
export function renderDateStrip(payload, activeDate) {
  // The sweep already drops past dates (dates.candidate_dates), so this filter only ever bites
  // in one case: a tab left open across midnight, re-rendered from cached data by a language
  // switch. Yesterday must not come back as a pickable chip.
  const today = localTodayIso();
  const rows = (payload.dates || []).filter((r) => r.date >= today);
  if (!rows.length) {
    clearDateStrip();
    return null;
  }

  const best = payload.best || null;
  const bestRow = best ? rows.find((r) => r.ok && r.date === best.date) || null : null;
  const { long } = dateFormatters();

  let note = "";
  if (payload.comparable === false) {
    // Some days priced live and some by model. A cheapest-day computed across two different
    // bases is not a comparison, so say that instead of naming a winner as fact.
    note = t("dates.notComparable");
  } else if (bestRow) {
    const bestWhen = long.format(isoToUtcDate(best.date));
    if (best.date === activeDate) {
      note = t("dates.sameDay");
    } else if (bestRow.savings_vs_anchor > 0) {
      note = t("dates.cheapestNote", {
        date: bestWhen, money: fmtMoney(best.cost), savings: fmtMoney(bestRow.savings_vs_anchor),
      });
    } else {
      // Either the picked date never priced, or it ties the winner - no savings figure to
      // claim, so don't invent one.
      note = t("dates.cheapestPlain", { date: bestWhen, money: fmtMoney(best.cost) });
    }
  }

  fillStrip("\n"
    + "      <p class=\"date-strip-head\">" + esc(t("dates.head", { window: payload.window })) + "</p>\n"
    + "      <ul class=\"date-strip-list\" role=\"list\">"
    + rows.map((r) => dateChip(r, best, activeDate)).join("") + "\n"
    + "      </ul>\n"
    + (note ? "      <p class=\"date-strip-note\">" + esc(note) + "</p>\n" : "")
    + "    ");
  centreOnWinner();
  return { summary: note };
}

/** Centre the cheapest chip in the strip's own scroller (falling back to the picked one when
 * nothing won). A panel this narrow only fits four of seven chips, and the winner sits at an
 * arbitrary offset in the window, so leaving the scroller at its start regularly hides the one
 * chip the whole strip exists to point at.
 *
 * Deliberately not scrollIntoView(): that walks up and scrolls every ancestor scroller too, and
 * the sweep lands a moment after the plan did, so it would yank #results out from under anyone
 * who had already started reading. Nudging scrollLeft by a signed VISUAL delta is also the one
 * form of this that needs no RTL special case - a bigger scrollLeft moves content left in both
 * directions, whatever sign the container's own scrollLeft happens to carry. */
function centreOnWinner() {
  const el = stripEl();
  const list = el?.querySelector(".date-strip-list");
  const target = el?.querySelector(".date-chip--best") || el?.querySelector(".date-chip--active");
  if (!list || !target) return;
  const lr = list.getBoundingClientRect();
  const tr = target.getBoundingClientRect();
  list.scrollLeft += (tr.left + tr.width / 2) - (lr.left + lr.width / 2);
}

/** Full render of a successful plan response. `placeLabel` is the free-text search label,
 * if the user searched rather than clicked, for the "X -> Y (place)" heading. `focusPanel`
 * should be true only for a user-initiated render (a new plan finishing) - not for a
 * language-switch re-render of the same data, which must not steal focus. */
export function renderPlan(data, placeLabel, focusPanel = false) {
  const R = data.result;
  const rec = R.options.find((o) => o.name === R.recommended);
  const base = R.options.find((o) => o.status === "baseline");
  const isDirect = rec.name === base.name;

  const srcTag = data.pricing_source === "estimate"
    ? "<span class=\"badge badge--est\">" + esc(t("badge.estimate")) + "</span>"
    : "<span class=\"badge badge--live\">" + esc(data.pricing_source) + "</span>";

  const greenestName = R.greenest;
  const greenestOpt = greenestName ? R.options.find((o) => o.name === greenestName) : null;
  const optionsHtml = R.options.map((o) => optionRow(o, rec.name, greenestName)).join("");

  const cautionLines = [
    t("caution.split"),
    data.pricing_source === "estimate" ? t("caution.pricesEst") : t("caution.prices"),
    ...(data.notes || []),
  ];

  // cheapest vs greenest, shown as a plain sentence rather than picking one for the user - 
  // only worth a callout when they're actually different options.
  const NAME_TOKEN = "@@NAME@@";
  const cheapestVsGreenest = (greenestOpt && greenestOpt.name !== rec.name)
    ? "<p class=\"rec-greenest-note\"><svg class=\"icon\" aria-hidden=\"true\"><use href=\"#i-leaf\"/></svg> "
      + spliceToken(
        esc(t("greenest.note", { name: NAME_TOKEN, co2: fmtCo2(greenestOpt.co2e_kg), co2rec: fmtCo2(rec.co2e_kg) })),
        NAME_TOKEN,
        "<strong>" + esc(greenestOpt.name) + "</strong>",
      )
      + "</p>"
    : "";

  const ROUTE_TOKEN = "@@ROUTE@@";
  const allOptionsHeading = spliceToken(
    esc(t("results.allOptions", { route: ROUTE_TOKEN })),
    ROUTE_TOKEN,
    destDescription(data, placeLabel),
  );

  panel().innerHTML = "\n"
    + "    <div class=\"results-head\">\n"
    + "      <h2 class=\"results-title\">" + esc(t("results.recommendation")) + " " + srcTag + "</h2>\n"
    + "      <button type=\"button\" id=\"copy-link\" class=\"btn btn--ghost btn--sm\">\n"
    + "        <svg class=\"icon\" aria-hidden=\"true\"><use href=\"#i-link\"/></svg> " + esc(t("results.copyLink")) + "\n"
    + "      </button>\n"
    + "    </div>\n"
    + recommendationCard(R, rec, isDirect) + "\n"
    + "    <div id=\"date-strip\" class=\"date-strip\" hidden></div>\n"
    + sheetToggleButton(R.options.length) + "\n"
    + cheapestVsGreenest + "\n"
    + weatherChip(data.weather) + "\n"
    + "    <h2 class=\"results-subtitle\">" + allOptionsHeading + "</h2>\n"
    + "    <ul class=\"opt-list\" id=\"opt-list\">" + optionsHtml + "</ul>\n"
    + "    <div class=\"notes\">\n"
    + "      <p class=\"notes-head\">" + esc(t("results.headsUp")) + "</p>\n"
    + "      <ul>" + cautionLines.map((n) => "<li>" + esc(n) + "</li>").join("") + "</ul>\n"
    + "    </div>";
  reveal(panel());
  panel().scrollTop = 0;
  if (focusPanel) panel().focus({ preventScroll: true });
  return { rec };
}

// No retry button: it'd need a new label in all 46 catalogs, and the error text already
// tells the user what to do.
export function renderError(msg, focusPanel = false) {
  const el = panel();
  // A previous plan may have left the mobile sheet expanded (see toggleSheet()) - this state
  // has no option list and no toggle button to shrink it back, so drop back to the compact
  // peek height rather than stranding the visitor with a full-height sheet hiding the map.
  el.classList.remove("results-expanded");
  el.innerHTML = "\n"
    + "    <div class=\"state-panel state-panel--error\">\n"
    + "      <p class=\"state-title\">" + esc(t("err.title")) + "</p>\n"
    + "      <p class=\"state-body\">" + esc(msg || t("err.fallback")) + "</p>\n"
    + "    </div>";
  reveal(el);
  el.scrollTop = 0;
  if (focusPanel) el.focus({ preventScroll: true });
}

// Empty-state motif: viewBox 0 0 280 90, same arc/rail/node semantics as the map and the
// h1 mark - blue dotted hop arc, ink transfer node, green dashed haul leg. Mirrors under
// [dir="rtl"] via the .empty-art svg{transform:scaleX(-1)} rule in styles.css.
const EMPTY_ART = "\n"
  + "    <svg class=\"empty-art\" viewBox=\"0 0 280 90\" width=\"140\" height=\"45\" aria-hidden=\"true\">\n"
  + "      <path class=\"arc-path draw-in\" d=\"M20 70 Q100 8 190 52\" fill=\"none\" stroke-width=\"2.5\" stroke-linecap=\"round\" stroke-dasharray=\"1 7\"/>\n"
  + "      <path class=\"rail-path draw-in\" d=\"M190 52 L262 66\" fill=\"none\" stroke-width=\"2.5\" stroke-linecap=\"round\" stroke-dasharray=\"8 7\"/>\n"
  + "      <circle class=\"dot-origin\" cx=\"20\" cy=\"70\" r=\"4\"/>\n"
  + "      <circle class=\"dot-node\" cx=\"190\" cy=\"52\" r=\"5\"/>\n"
  + "      <circle class=\"dot-dest\" cx=\"262\" cy=\"66\" r=\"4\"/>\n"
  + "    </svg>";

export function renderEmpty() {
  const el = panel();
  el.classList.remove("results-expanded"); // see the comment in renderError() above
  el.innerHTML = "\n"
    + "    <div class=\"state-panel state-panel--empty\">\n"
    + EMPTY_ART
    + "      <p class=\"state-title\">" + esc(t("empty.title")) + "</p>\n"
    + "      <p class=\"state-body\">" + esc(t("empty.body")) + "</p>\n"
    + "    </div>";
  reveal(el);
}

export function renderLoading() {
  const el = panel();
  // Every new plan attempt (planTo()) routes through here first, so this is also the natural
  // place to drop a carried-over expanded sheet back to peek for the NEW route - the visitor
  // gets the map back while it's working, same as renderError()/renderEmpty() above. A
  // language switch on an already-finished plan (rerenderCurrent()) skips this function
  // entirely and goes straight to renderPlan(), so it isn't affected - that path still
  // preserves whatever the visitor had open.
  el.classList.remove("results-expanded");
  el.innerHTML = "\n"
    + "    <div class=\"state-panel state-panel--loading\">\n"
    + "      <p class=\"state-title\">" + esc(t("loading.title")) + "</p>\n"
    + "    </div>";
  reveal(el);
}
