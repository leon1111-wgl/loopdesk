// Checks that every text/fill pair the console uses meets WCAG AA (4.5:1) in both themes, reading
// the values from src/styles/tokens.css. Run with `pnpm contrast`; exits 1 on a failing pair.
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("../src/styles/tokens.css", import.meta.url), "utf8");
const block = (re) => Object.fromEntries([...(css.match(re)?.[1] ?? "").matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));
const light = block(/:root\s*\{([^}]*)\}/);
const dark = { ...light, ...block(/:root\[data-theme="dark"\]\s*\{([^}]*)\}/) };

const lum = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

// [foreground, background]: "#ffffff" stands for white text on a solid fill.
const pairs = [
  ...["text", "text-2", "text-3", "primary"].flatMap((fg) => ["surface", "surface-2", "bg", "hover"].map((bg) => [fg, bg])),
  ["text-2", "primary-soft"], ["text-3", "primary-soft"], ["primary", "primary-soft"], ["text-2", "pressed"],
  ...["acc-service", "acc-knowledge", "acc-ops"].flatMap((c) => [[c, "surface"], [c, "bg"], [c, "surface-2"]]),
  ...["success", "warning", "danger", "info"].flatMap((c) => [[c, `${c}-soft`], [c, "surface"]]),
  ...["primary-solid", "primary-solid-hover", "success-solid", "warning-solid", "danger-solid"].map((c) => ["#ffffff", c]),
  // The intro headline's highlight block: white large text (>= 3:1) on each stop of the gradient.
  ...[...(css.match(/--highlight:[^;]*/)?.[0].matchAll(/#[0-9a-f]{6}/gi) ?? [])].map((m) => ["#ffffff", m[0], 3]),
];
let failed = 0;
for (const [theme, t] of [["light", light], ["dark", dark]]) {
  for (const [fg, bg, min = 4.5] of pairs) {
    const a = fg.startsWith("#") ? fg : t[fg];
    const r = ratio(a, bg.startsWith("#") ? bg : t[bg]);
    if (r < min) { failed++; console.log(`FAIL ${theme}: ${fg} on ${bg} = ${r.toFixed(2)}`); }
  }
}
console.log(failed ? `${failed} pair(s) below 4.5:1` : `all ${pairs.length * 2} pairs meet AA`);
process.exit(failed ? 1 : 0);
