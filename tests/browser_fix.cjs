const {chromium} = require("C:/Users/LEAF/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");
const zlib = require("node:zlib");
// 生成一张纯色 PNG（不依赖额外库）
function png(w, h, rgb) {
  const crcT = Array.from({length: 256}, (_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
  const crc = b => { let c = 0xffffffff; for (const x of b) c = crcT[(c ^ x) & 255] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
  const chunk = (t, d) => { const l = Buffer.alloc(4); l.writeUInt32BE(d.length); const td = Buffer.concat([Buffer.from(t), d]); const c = Buffer.alloc(4); c.writeUInt32BE(crc(td)); return Buffer.concat([l, td, c]); };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
  const row = Buffer.concat([Buffer.from([0]), Buffer.from(Array(w).fill(rgb).flat())]);
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(Buffer.concat(Array(h).fill(row)))), chunk("IEND", Buffer.alloc(0))]);
}
const genUrl = "data:image/png;base64," + png(256, 384, [235, 225, 205]).toString("base64");
const fixUrl = "data:image/png;base64," + png(256, 384, [200, 160, 90]).toString("base64");

(async () => {
  const browser = await chromium.launch({channel: "chrome", headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = []; let inpaintForm = null;
  page.on("pageerror", e => errors.push(e.message));
  await page.route("**/api/health", r => r.fulfill({json: {ok: true, model: "m", modelReady: true, detail: "接口正常"}}));
  await page.route("**/api/analyze", r => r.fulfill({json: {spec: "- gold pendant, 5 petals"}}));
  await page.route("**/api/check", r => r.fulfill({json: {match: false, issues: ["花瓣数量多了一片", "扣头形状不同"]}}));
  await page.route("**/api/generate", r => r.fulfill({json: {image: genUrl, size: "256x384", retries: 0, flags: []}}));
  await page.route("**/api/inpaint", async r => {
    inpaintForm = r.request().postDataBuffer().toString("latin1");
    await new Promise(res => setTimeout(res, 400));
    r.fulfill({json: {image: fixUrl, mask_used: false, outside_unchanged: true, retries: 0, region: [1, 1, 2, 2]}});
  });
  await page.goto("http://127.0.0.1:8800/");
  await page.setInputFiles("#fileInput", {name: "ring.png", mimeType: "image/png", buffer: png(200, 200, [210, 170, 80])});
  const card = page.locator(".vcard").first();
  console.log("fix button before generate disabled:", await card.locator(".fix").isDisabled());
  await card.locator(".redo").click();
  await page.waitForFunction(() => document.querySelector(".vcard .vqa").textContent.includes("疑似"));
  console.log("card buttons:", (await card.locator(".vact button").allTextContents()).join(" | "));
  await card.locator(".fix").click();
  await page.waitForSelector("#fixDialog[open]");
  console.log("dialog title:", await page.textContent("#fixTitle"));
  console.log("issues listed:", (await page.locator("#fixIssues label").allTextContents()).join(" / "));
  console.log("go disabled before drawing:", await page.isDisabled("#fixGo"));
  const box = await page.locator("#fixCanvas").boundingBox();
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2, r = box.width / 6;
  await page.mouse.move(cx + r, cy); await page.mouse.down();
  for (let a = 0; a <= 360; a += 15) await page.mouse.move(cx + r * Math.cos(a * Math.PI / 180), cy + r * Math.sin(a * Math.PI / 180));
  await page.mouse.up();
  console.log("go enabled after drawing:", !(await page.isDisabled("#fixGo")));
  await page.screenshot({path: "C:/claude/work/beibei_fix_dialog.png"});
  await page.locator("#fixIssues input").nth(1).uncheck();
  await page.fill("#fixExtra", "花瓣应该是5片");
  await page.click("#fixGo");
  await page.waitForFunction(() => document.querySelectorAll(".vcard .vers .chip").length === 2);
  const decoded = Buffer.from(inpaintForm, "latin1").toString("utf8");
  const prompt = (decoded.match(/name="prompt"\r\n\r\n([\s\S]*?)\r\n--/) || [])[1] || "";
  console.log("form parts:", ["base", "marked", "mask", "reference", "prompt", "size"].map(k => k + "=" + decoded.includes(`name="${k}"`)).join(" "));
  console.log("prompt has checked issue:", prompt.includes("花瓣数量多了一片"), "| unchecked issue excluded:", !prompt.includes("扣头形状不同"));
  console.log("prompt has user extra:", prompt.includes("花瓣应该是5片"), "| has fact sheet:", prompt.includes("5 petals"), "| starts:", prompt.slice(0, 60));
  console.log("version chips:", (await card.locator(".vers .chip").evaluateAll(els => els.map(e => e.textContent + (e.classList.contains("local") ? "·局部" : "") + (e.classList.contains("on") ? "(当前)" : "")))).join(" "));
  console.log("note:", await page.textContent("#genNote"));
  console.log("page errors:", errors.length ? errors.join(" | ") : "none");
  await browser.close();
  process.exit(errors.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
