#!/usr/bin/env node
/*
 * check.mjs - runs the JS port (src/hopandhaul/ui/engine/) over the same cases.json fixtures
 * gen_fixtures.py just generated from the real Python engine, and deep-equals the two. Any
 * drift between the browser engine and hopandhaul.trip/geo/server/emissions shows up here (and
 * in CI) instead of silently shipping a wrong answer to the Pages build.
 *
 * Run (after `python tests/web_parity/gen_fixtures.py`):
 *   node tests/web_parity/check.mjs
 *
 * Exit 0 = every fixture matches, 1 = a mismatch or a missing/unreadable fixture.
 */
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const ENGINE_DIR = path.join(REPO_ROOT, "src", "hopandhaul", "ui", "engine");
const DATA_DIR = path.join(REPO_ROOT, "src", "hopandhaul", "data");
const FIXTURES_DIR = path.join(HERE, "fixtures");
const CASES_PATH = path.join(HERE, "cases.json");

const engineUrl = (f) => pathToFileURL(path.join(ENGINE_DIR, f)).href;
const { plan } = await import(engineUrl("plan.js"));
const trip = await import(engineUrl("trip.js"));
const { loadData } = await import(engineUrl("data.js"));

// Node has no browser fetch()-a-local-file story worth relying on here - read the same two
// JSON files geo.py reads, straight off disk. Same bytes, same array order, so nearest_airport/
// discover_gateways see identical input on both sides.
function nodeLoader(filename) {
  return JSON.parse(readFileSync(path.join(DATA_DIR, filename), "utf8"));
}

function buildOptionString(opt) {
  const legs = opt.legs.map((leg) => `${leg.mode} ${leg.cost} ${leg.hours}`).join(" ; ");
  return `${opt.name} | ${legs}`;
}

function stripPrivate(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    if (!k.startsWith("_")) out[k] = v;
  }
  return out;
}

function runPlanCase(c) {
  const p = c.params;
  return plan({
    destLat: p.dest_lat,
    destLng: p.dest_lng,
    originIata: p.origin_iata ?? "JFK",
    date: p.date ?? null,
    vot: p.vot ?? null,
    threshold: p.threshold ?? trip.DEFAULT_THRESHOLD,
    maxGroundH: p.max_ground_h ?? 6.0,
    roundtrip: p.roundtrip ?? false,
    travelers: p.travelers ?? 1,
    ret: p.ret ?? null,
    transferBuffer: p.transfer_buffer ?? 1.0,
  });
}

function runEvaluateCase(c) {
  const travelers = c.travelers ?? 1;
  // Every real call site scales each option's leg costs by travelers BEFORE evaluate() ever
  // sees them (see the matching comment in gen_fixtures.py) - mirror that here.
  const options = c.options
    .map((o) => trip.parseOption(buildOptionString(o)))
    .map((o) => trip.scaleOption(o, travelers));
  const res = trip.evaluate(options, {
    threshold: c.threshold ?? trip.DEFAULT_THRESHOLD,
    vot: c.vot ?? null,
    transferBuffer: c.transfer_buffer ?? 0.0,
    maxHours: c.max_hours ?? null,
    travelers,
  });
  return stripPrivate(res);
}

function runCase(c) {
  if (c.type === "plan") return runPlanCase(c);
  if (c.type === "evaluate") return runEvaluateCase(c);
  throw new Error(`unknown case type ${JSON.stringify(c.type)} in ${c.name}`);
}

// Deep compare; numbers within a tiny epsilon (should be EXACT after pyRound - this epsilon is
// a safety net for float ULP noise between V8's and CPython's libm, not a tolerance for wrong
// values), everything else strict equality. Object key order doesn't matter; array order does.
function diff(a, b, at = "$") {
  if (typeof a === "number" && typeof b === "number") {
    return Math.abs(a - b) < 1e-9 ? null : `${at}: ${a} != ${b}`;
  }
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b)) return `${at}: array vs non-array (js=${JSON.stringify(a)}, py=${JSON.stringify(b)})`;
    if (a.length !== b.length) {
      return `${at}: length ${a.length} != ${b.length}\n  js=${JSON.stringify(a)}\n  py=${JSON.stringify(b)}`;
    }
    for (let i = 0; i < a.length; i++) {
      const d = diff(a[i], b[i], `${at}[${i}]`);
      if (d) return d;
    }
    return null;
  }
  if (a && b && typeof a === "object" && typeof b === "object") {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const k of keys) {
      const d = diff(a[k], b[k], `${at}.${k}`);
      if (d) return d;
    }
    return null;
  }
  return a === b ? null : `${at}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`;
}

async function main() {
  if (!existsSync(FIXTURES_DIR)) {
    console.error(`no fixtures at ${FIXTURES_DIR} — run: python tests/web_parity/gen_fixtures.py`);
    process.exit(1);
  }
  await loadData(nodeLoader);

  const cases = JSON.parse(readFileSync(CASES_PATH, "utf8"));
  let pass = 0;
  let fail = 0;
  const failures = [];

  for (const c of cases) {
    const fixturePath = path.join(FIXTURES_DIR, `${c.name}.json`);
    if (!existsSync(fixturePath)) {
      fail++;
      console.log(`FAIL  ${c.name}`);
      console.log(`      no fixture at ${fixturePath} — run gen_fixtures.py first`);
      failures.push({ name: c.name, d: "missing fixture" });
      continue;
    }
    const py = JSON.parse(readFileSync(fixturePath, "utf8"));
    let js;
    let err = null;
    try {
      js = runCase(c);
    } catch (e) {
      err = e.stack || e.message;
    }
    const d = err ? `error: ${err}` : diff(js, py);
    if (d) {
      fail++;
      console.log(`FAIL  ${c.name}`);
      console.log(`      ${d}`);
      failures.push({ name: c.name, d, js, py });
    } else {
      pass++;
      const tag = c.type === "plan" ? (js.ok ? js.result.recommended : `error:${js.code}`) : js.recommended;
      console.log(`ok    ${c.name}  (${c.type}, recommended: ${tag})`);
    }
  }

  console.log(`\n${pass} passed, ${fail} failed  (${cases.length} cases)`);
  if (fail) {
    console.log("\n--- first failure detail ---");
    const f = failures[0];
    console.log("js:", JSON.stringify(f.js, null, 2));
    console.log("py:", JSON.stringify(f.py, null, 2));
    process.exit(1);
  }
}

main();
