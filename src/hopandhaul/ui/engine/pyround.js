// Python-3-compatible round-half-to-even (banker's rounding), to ndigits decimal places.
//
// JS's Math.round() always rounds half away from zero (22.5 -> 23), but Python 3's builtin
// round() rounds half to the nearest EVEN digit (22.5 -> 22, 23.5 -> 24). Every function ported
// from trip.py/geo.py/emissions.py that calls Python's round() must use this instead of
// Math.round()/toFixed() to stay in exact parity with the reference implementation — that's
// the whole point of the web-parity test (see ../../../tests/web_parity/).
//
// CPython's round() does NOT use a floating-point epsilon: it works from the exact decimal
// expansion of the double (via David Gay's dtoa, see Objects/floatobject.c's double_round())
// and only rounds to even when the digit after the cut point is a genuine 5 with nothing
// nonzero beyond it in that exact expansion. A naive `Math.abs(diff - 0.5) < 1e-9` epsilon
// check is wrong: round(2.675, 2) in real Python is 2.67 (2.675 is actually stored as
// 2.67499999999999982236..., genuinely below the halfway point, not a tie), but an epsilon
// nudge misclassifies it as a tie and returns 2.68. This reads the double's own exact decimal
// digits via toFixed() (well within its 100-digit limit for any realistic fare/hours/km
// magnitude this app deals with) and does the even/odd check on those digits directly, so it
// can tell a genuine tie from a near-tie the same way CPython does.
//
// ndigits omitted matches Python's round(x) with no second argument (used for geo.py's
// "round(fare/5)*5" pattern) — returns an integer-valued number, never -0.

export function pyRound(x, ndigits) {
  if (!Number.isFinite(x)) return x;

  const ndigitsGiven = ndigits !== undefined;
  const digits = Math.max(0, ndigitsGiven ? ndigits : 0);

  if (x === 0) return ndigitsGiven ? x : 0; // -0.0 only survives when ndigits was explicit

  const negative = x < 0;
  const abs = Math.abs(x);

  // Digits kept past the rounding point, purely to tell "exact tie" (all zeros beyond) from
  // "not quite a tie" — not needed for precision itself, since a double is fully determined by
  // ~17 significant digits.
  const extra = 40;
  const places = Math.min(100, digits + extra);
  const s = abs.toFixed(places);

  const dot = s.indexOf(".");
  const intPart = dot === -1 ? s : s.slice(0, dot);
  const fracPart = (dot === -1 ? "" : s.slice(dot + 1)).padEnd(digits + extra, "0");

  const keep = fracPart.slice(0, digits);
  const rest = fracPart.slice(digits);
  const firstDropped = rest[0] || "0";
  const remainderNonzero = /[1-9]/.test(rest.slice(1));

  const kept = (intPart + keep).split("").map(Number);
  const roundUp = () => {
    let i = kept.length - 1;
    while (i >= 0) {
      kept[i] += 1;
      if (kept[i] < 10) return;
      kept[i] = 0;
      i -= 1;
    }
    kept.unshift(1);
  };

  if (firstDropped > "5" || (firstDropped === "5" && remainderNonzero)) {
    roundUp();
  } else if (firstDropped === "5") {
    // Exact tie (nothing nonzero beyond the 5): round to even.
    const last = kept[kept.length - 1] ?? 0;
    if (last % 2 !== 0) roundUp();
  }
  // firstDropped < "5": truncate, no rounding needed.

  const intLen = kept.length - digits;
  const intStr = kept.slice(0, intLen).join("") || "0";
  const fracStr = kept.slice(intLen).join("");
  const resultStr = fracStr ? `${intStr}.${fracStr}` : intStr;

  const result = parseFloat(resultStr);
  if (!negative) return result;
  if (result === 0) return ndigitsGiven ? -0 : 0; // no int(-0) in Python; float -0.0 only if ndigits was explicit
  return -result;
}
