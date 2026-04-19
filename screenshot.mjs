import puppeteer from './node_modules/puppeteer/lib/esm/puppeteer/puppeteer.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const screenshotDir = path.join(__dirname, 'temporary screenshots');

if (!fs.existsSync(screenshotDir)) {
  fs.mkdirSync(screenshotDir, { recursive: true });
}

const url   = process.argv[2] || 'http://localhost:3000';
const label = process.argv[3] ? `-${process.argv[3]}` : '';

// Auto-increment screenshot filename
let n = 1;
while (fs.existsSync(path.join(screenshotDir, `screenshot-${n}${label}.png`))) n++;
const outputPath = path.join(screenshotDir, `screenshot-${n}${label}.png`);

// Flags derived from label
const isClose  = label.includes('close');
const isMobile = label.includes('mobile');
const fullPage = !isClose;

const viewportW = isMobile ? 375 : 1440;
const viewportH = isMobile ? 812  : isClose ? 1100 : 900;

const browser = await puppeteer.launch({
  headless: true,
  args: ['--no-sandbox', '--disable-setuid-sandbox'],
});

const page = await browser.newPage();
await page.setViewport({ width: viewportW, height: viewportH, deviceScaleFactor: 2 });
await page.goto(url, { waitUntil: 'networkidle0', timeout: 30000 });

// Wait for initial render
await new Promise(r => setTimeout(r, 800));

// For full-page shots: un-stick the nav so it doesn't repeat in the Puppeteer stitch
if (fullPage) {
  await page.addStyleTag({
    content: `
      nav { position: relative !important; top: auto !important; }
    `
  });
}

// Scroll through page to trigger IntersectionObserver reveals
await page.evaluate(async () => {
  const totalHeight = document.body.scrollHeight;
  for (let y = 0; y <= totalHeight; y += 400) {
    window.scrollTo(0, y);
    await new Promise(r => setTimeout(r, 60));
  }
  window.scrollTo(0, 0);
  await new Promise(r => setTimeout(r, 300));
});

await new Promise(r => setTimeout(r, 500));

await page.screenshot({ path: outputPath, fullPage });
await browser.close();

console.log(`Screenshot saved: ${outputPath}`);
