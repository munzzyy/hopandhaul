// Renders the recommendation card + option list into the (already aria-live) results panel.
import { esc, fmtMoney, fmtH, modeEmoji, modeLabel, statusLabel } from "./format.js";

const panel = () => document.getElementById("results");

function weatherChip(w) {
  if (!w) return "";
  const u = esc(w.units || "°");
  const feels = w.feels != null
    ? ` <span class="wx-feels">feels ${esc(w.feels)}${u}</span>` : "";
  const fc = w.forecast
    ? `<div class="wx-fc">${esc(w.forecast.emoji)} ${esc(w.forecast.temp)}${esc(w.forecast.units)}`
      + ` &middot; ${esc(w.forecast.date)}<br>${esc(w.forecast.desc || "")}</div>`
    : (w.forecast_note ? `<div class="wx-fc">${esc(w.forecast_note)}</div>` : "");
  return `<div class="wx">`
    + `<span class="wx-ico" aria-hidden="true">${esc(w.emoji || "\u{1F321}️")}</span>`
    + `<div class="wx-main"><div class="wx-temp">${w.temp != null ? esc(w.temp) + u : ""}${feels}</div>`
    + `<div class="wx-desc">${esc(w.desc || "")}${w.place ? " &middot; " + esc(w.place) : ""}</div></div>`
    + `${fc}</div>`;
}

function legLabel(l) {
  return `<span class="leg"><span aria-hidden="true">${modeEmoji(l.mode)}</span> `
    + `<span class="sr-only">${esc(modeLabel(l.mode))}</span>${fmtMoney(l.cost)}</span>`;
}

function destDescription(data, placeLabel) {
  const c = placeLabel || data.dest.city;
  return `${esc(data.origin.iata)} → ${esc(data.dest.iata)}${c ? " (" + esc(c) + ")" : ""}`;
}

function recommendationCard(R, rec, isDirect) {
  let heroValue, heroLabel, subline, breakeven = "";

  if (isDirect) {
    heroValue = "Fly direct";
    heroLabel = `No option beats it by ${fmtMoney(R.threshold)}`;
    subline = `The cheapest direct flight is already your best option.`;
  } else if (rec.dominant) {
    heroValue = fmtMoney(rec.savings_vs_baseline);
    heroLabel = "cheaper — and no slower";
    subline = `Fly into <strong>${esc(rec.name.split(" + ")[0])}</strong>, then `
      + `${esc(modeLabel(rec.legs[1]?.mode))} the rest. A clean win either way you weigh it.`;
  } else {
    heroValue = fmtMoney(rec.savings_vs_baseline);
    heroLabel = `saved vs flying direct (≥ ${fmtMoney(R.threshold)} rule)`;
    subline = `Fly into <strong>${esc(rec.name.split(" + ")[0])}</strong>, then `
      + `${esc(modeLabel(rec.legs[1]?.mode))} the rest.`;
    if (rec.extra_hours_vs_baseline > 0 && rec.breakeven_vot != null) {
      breakeven = `Adds ${fmtH(rec.extra_hours_vs_baseline)} vs direct. Worth it unless your time `
        + `is worth more than <strong>${fmtMoney(rec.breakeven_vot)}/hr</strong>.`;
      if (R.vot != null) {
        const delta = rec.savings_vs_baseline - R.vot * rec.extra_hours_vs_baseline;
        breakeven += ` At ${fmtMoney(R.vot)}/hr you come out ${delta >= 0 ? "ahead" : "behind"} `
          + `by ${fmtMoney(Math.abs(delta))}.`;
      }
    } else if (rec.extra_hours_vs_baseline <= 0) {
      breakeven = `And it's no slower than flying direct.`;
    }
  }

  const routeLine = isDirect
    ? `✈️ Fly direct to ${esc(rec.name.replace("Fly direct to ", ""))}`
    : `✈️→${modeEmoji(rec.legs[1]?.mode)} ${esc(rec.name)}`;

  return `
    <div class="rec-card ${isDirect ? "rec-card--direct" : "rec-card--split"}">
      <p class="rec-route">${routeLine}</p>
      <p class="rec-hero"><span class="rec-hero-value">${heroValue}</span>
        <span class="rec-hero-label">${heroLabel}</span></p>
      <p class="rec-sub">${subline}</p>
      ${breakeven ? `<p class="rec-breakeven">${breakeven}</p>` : ""}
    </div>`;
}

function optionRow(o, recName) {
  const { text: statusText, tone } = statusLabel(o.status);
  const legs = o.legs.map(legLabel).join(" + ");
  const savingsText = o.savings_vs_baseline > 0
    ? `saves ${fmtMoney(o.savings_vs_baseline)}`
    : (o.savings_vs_baseline < 0 ? `${fmtMoney(-o.savings_vs_baseline)} more` : "baseline");
  return `
    <li class="opt ${o.name === recName ? "opt--win" : ""}">
      <div class="opt-top">
        <span class="opt-name">${esc(o.name)}</span>
        <span class="opt-price">${fmtMoney(o.cost)}</span>
      </div>
      <div class="opt-meta">
        <span>${legs}</span>
        <span>${fmtH(o.hours_eff)}${o.is_split ? " &middot; " + (o.buffer_h ? "+buffer" : "multimodal") : ""}</span>
      </div>
      <div class="opt-meta">
        <span>${savingsText}</span>
        <span class="tag tag--${tone}">${o.name === recName ? "★ " : ""}${esc(statusText)}</span>
      </div>
    </li>`;
}

/** Full render of a successful plan response. `placeLabel` is the free-text search label,
 * if the user searched rather than clicked, for the "X → Y (place)" heading. */
export function renderPlan(data, placeLabel) {
  const R = data.result;
  const rec = R.options.find((o) => o.name === R.recommended);
  const base = R.options.find((o) => o.status === "baseline");
  const isDirect = rec.name === base.name;

  const srcTag = data.pricing_source === "estimate"
    ? `<span class="badge badge--est">estimate</span>`
    : `<span class="badge badge--live">${esc(data.pricing_source)}</span>`;

  const optionsHtml = R.options.map((o) => optionRow(o, rec.name)).join("");

  const cautionLines = [
    `Split legs booked separately aren't protected — a delayed flight can forfeit a `
    + `non-refundable train/bus ticket.`,
    `Prices ${data.pricing_source === "estimate" ? "are estimates and " : ""}move fast — `
    + `re-verify before booking.`,
    ...(data.notes || []),
  ];

  panel().innerHTML = `
    <div class="results-head">
      <h2 class="results-title">Recommendation ${srcTag}</h2>
      <button type="button" id="copy-link" class="btn btn--ghost btn--sm">
        <span aria-hidden="true">\u{1F517}</span> Copy link
      </button>
    </div>
    ${recommendationCard(R, rec, isDirect)}
    ${weatherChip(data.weather)}
    <h2 class="results-subtitle">All options &mdash; ${destDescription(data, placeLabel)}</h2>
    <ul class="opt-list">${optionsHtml}</ul>
    <div class="notes">
      <p class="notes-head">Heads up</p>
      <ul>${cautionLines.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
    </div>`;
  panel().hidden = false;
  panel().classList.add("show");
  return { rec };
}

export function renderError(msg) {
  const el = panel();
  el.innerHTML = `
    <div class="state-panel state-panel--error">
      <p class="state-title">Couldn't plan that route</p>
      <p class="state-body">${esc(msg || "Unknown error — try a different point or check your connection.")}</p>
    </div>`;
  el.hidden = false;
  el.classList.add("show");
}

export function renderEmpty() {
  const el = panel();
  el.innerHTML = `
    <div class="state-panel state-panel--empty">
      <p class="state-title">Pick where you're headed</p>
      <p class="state-body">Search a city above or click anywhere on the map — I'll compare
        flying direct against hopping into a cheaper hub and hauling the rest by train, bus,
        or car.</p>
    </div>`;
  el.hidden = false;
  el.classList.add("show");
}

export function renderLoading() {
  const el = panel();
  el.innerHTML = `
    <div class="state-panel state-panel--loading">
      <p class="state-title">Crunching the numbers&hellip;</p>
    </div>`;
  el.hidden = false;
  el.classList.add("show");
}
