import { expect, test } from "@playwright/test";

// Layout changes affect every role and route. Check usable viewports against the actual API;
// wide data tables may scroll inside their own container, never the entire workspace.
for (const variant of [
  { name: "desktop light", width: 1440, height: 960, dark: false },
  { name: "desktop dark", width: 1440, height: 960, dark: true },
  { name: "tablet", width: 820, height: 1180, dark: false },
  { name: "mobile", width: 390, height: 844, dark: false },
]) {
  test(`${variant.name}: navigation, readable charts and contained page layouts`, async ({ browser }) => {
    const context = await browser.newContext({
      viewport: { width: variant.width, height: variant.height },
      colorScheme: variant.dark ? "dark" : "light",
      reducedMotion: "reduce",
    });
    const page = await context.newPage();
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    try {
      // The intro page: its call to action, and the demo accounts on the same page.
      await page.goto("/");
      await expect(page.getByRole("button", { name: "进入控制台" }).first()).toBeVisible();
      await expect(page.getByRole("button", { name: /admin@echo.demo/ })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.getByRole("button", { name: /admin@echo.demo/ }).click();
      await expect(page).toHaveURL(/dashboard/);
      await expect(page.locator(".trend-chart .x-label")).toHaveCount(7);
      for (const label of await page.locator(".trend-chart .x-label").allTextContents()) expect(label).toMatch(/^\d{2}\/\d{2}$/);
      expect(await page.locator(".kpi").first().evaluate((el) => getComputedStyle(el).animationName)).toBe("none");
      // Keyboard users can reach and expand the export control without a pointer.
      await page.locator(".export-menu summary").focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("button", { name: "会话全文", exact: true })).toBeVisible();
      await page.keyboard.press("Enter");
      await expect(page.locator(".export-options")).not.toBeVisible();
      // Sections in the top bar (a bottom tab bar on phones), each with its row of page tabs.
      const nav = page.getByRole("navigation", { name: "导航菜单" });
      await nav.getByRole("link", { name: /^知识与质量/ }).click();
      await expect(page).toHaveURL(/knowledge/);
      await expect(page.getByRole("navigation", { name: "知识与质量页面" }).getByRole("link", { name: "评测中心" })).toBeVisible();
      if (variant.width < 780) {
        const box = await nav.boundingBox();
        expect(box && box.y + box.height).toBeCloseTo(variant.height, 0); // pinned to the bottom of the screen
      }
      for (const route of ["dashboard", "approvals", "handoffs", "tickets", "refunds", "knowledge", "evals", "flywheel", "traces", "audit", "chat"]) {
        await page.goto(`/${route}`);
        await expect(page.locator(".topbar")).toBeVisible();
        await expect(page.locator(route === "chat" ? ".composer textarea" : ".page-head h1")).toBeVisible();
        await expect(page.getByText("加载中…", { exact: true })).toHaveCount(0);
        const overflow = await page.evaluate(() => {
          const root = document.documentElement;
          const page = document.querySelector(".page");
          return { document: root.scrollWidth > innerWidth + 1, content: page ? page.scrollWidth > page.clientWidth + 1 : false };
        });
        expect(overflow, `${variant.name}: ${route} must not overflow horizontally`).toEqual({ document: false, content: false });
      }
      await expect(page.locator(".welcome h2")).toBeVisible();
      await expect.poll(() => page.locator(".messages").evaluate((el) => el.scrollTop)).toBe(0);
      expect(errors).toEqual([]);
    } finally {
      await context.close();
    }
  });
}
