// Renders the recommendation card + option list into the results panel. The panel itself
// isn't aria-live (that's #sr-status); a user-initiated render moves focus to the panel so
// screen readers land on and read the new content instead of staying on the search field.
import { esc, fmtMoney, fmtH, fmtCo2, modeIcon, modeLabel, statusLabel } from "./format.js";
import { t } from "./i18n.js";

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
