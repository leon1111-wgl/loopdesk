// Checks that every text/fill pair the console uses meets WCAG AA (4.5:1) in both themes, reading
// the values from src/styles/tokens.css. Run with `pnpm contrast`; exits 1 on a failing pair.
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("../src/styles/tokens.css", import.meta.url), "utf8");
const block = (re) => Object.fromEntries([...(css.match(re)?.[1] ?? "").matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));
const light = block(/:root\s*\{([^}]*)\}/);
const dark = { ...light, ...block(/:root\[data-theme="dark"\]\s*\{([^}]*)\}/) };

// "rgb(r g b / a%)" composited over a hex background (the translucent highlighter band).
const over = (rgba, bgHex) => {
  const m = rgba.match(/rgb\((\d+) (\d+) (\d+) \/ (\d+)%\)/);
  const a = Number(m[4]) / 100;
  const bg = [1, 3, 5].map((i) => parseInt(bgHex.slice(i, i + 2), 16));
  return "#" + [1, 2, 3].map((i, k) => Math.round(Number(m[i]) * a + bg[k] * (1 - a)).toString(16).padStart(2, "0")).join("");
};
const mix = (aHex, pct, bHex) => "#" + [1, 3, 5].map((i) => Math.round(parseInt(aHex.slice(i, i + 2), 16) * pct + parseInt(bHex.slice(i, i + 2), 16) * (1 - pct)).toString(16).padStart(2, "0")).join("");
const highlight = (re) => css.match(re)?.[1].match(/--highlight:\s*(rgb\([^)]*\))/)?.[1];
light.band = over(highlight(/:root\s*\{([^}]*)\}/), light.bg);
dark.band = over(highlight(/:root\[data-theme="dark"\]\s*\{([^}]*)\}/), dark.bg);
for (const t of [light, dark]) for (const c of ["acc-knowledge", "acc-ops"]) t[`${c}-soft`] = mix(t[c], 0.14, t.surface);

const lum = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

// [foreground, background]: "#ffffff" stands for white text on a solid fill.
const pairs = [
  ...["text", "text-2", "text-3", "primary"].flatMap((fg) => ["surface", "surface-2", "bg", "hover"].map((bg) => [fg, bg])),
  ["text-2", "primary-soft"], ["text-3", "primary-soft"], ["primary", "primary-soft"], ["text-2", "pressed"], ["text-3", "pressed"],
  ["primary", "band", 3], // the intro headline's evidenced phrase: large text on the highlighter band
  ...["acc-knowledge", "acc-ops"].flatMap((c) => [["text", `${c}-soft`], [c, `${c}-soft`, 3]]), // active page tab and its icon
  ...["acc-service", "acc-knowledge", "acc-ops"].flatMap((c) => [[c, "surface"], [c, "bg"], [c, "surface-2"]]),
  ...["success", "warning", "danger", "info"].flatMap((c) => [[c, `${c}-soft`], [c, "surface"]]),
  ...["primary-solid", "primary-solid-hover", "success-solid", "warning-solid", "danger-solid"].map((c) => ["#ffffff", c]),
];
let failed = 0;
for (const [theme, t] of [["light", light], ["dark", dark]]) {
  for (const [fg, bg, min = 4.5] of pairs) {
    const a = fg.startsWith("#") ? fg : t[fg];
    const r = ratio(a, bg.startsWith("#") ? bg : t[bg]);
    if (r < min) { failed++; console.log(`FAIL ${theme}: ${fg} on ${bg} = ${r.toFixed(2)}`); }
  }
}
console.log(failed ? `${failed} pair(s) below their minimum` : `all ${pairs.length * 2} pairs meet WCAG AA (4.5:1, 3:1 for large text)`);
process.exit(failed ? 1 : 0);
