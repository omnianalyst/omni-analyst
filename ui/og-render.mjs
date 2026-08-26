import { chromium } from "@playwright/test";
import { pathToFileURL } from "node:url";

const out = process.argv[2];
const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 1200, height: 630 },
  deviceScaleFactor: 2,
});
await page.goto(pathToFileURL(process.argv[3]).href);
await page.evaluate(() => document.fonts.ready);
await page.waitForTimeout(400);
await page.screenshot({ path: out });
await browser.close();
console.log("written", out);
