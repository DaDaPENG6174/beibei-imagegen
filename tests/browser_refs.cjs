const {chromium} = require("C:/Users/LEAF/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");
const zlib = require("node:zlib");
function png(w, h, rgb) {
  const crcT = Array.from({length: 256}, (_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
  const crc = b => { let c = 0xffffffff; for (const x of b) c = crcT[(c ^ x) & 255] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
  const chunk = (t, d) => { const l = Buffer.alloc(4); l.writeUInt32BE(d.length); const td = Buffer.concat([Buffer.from(t), d]); const c = Buffer.alloc(4); c.writeUInt32BE(crc(td)); return Buffer.concat([l, td, c]); };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
  const row = Buffer.concat([Buffer.from([0]), Buffer.from(Array(w).fill(rgb).flat())]);
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(Buffer.concat(Array(h).fill(row)))), chunk("IEND", Buffer.alloc(0))]);
}
const genUrl = "data:image/png;base64," + png(256, 384, [235, 225, 205]).toString("base64");

(async () => {
  const browser = await chromium.launch({channel: "chrome", headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  const gens = [];          // 每次 /api/generate 的 multipart
  let analyzed = null;
  page.on("pageerror", e => errors.push(e.message));
  await page.route("**/api/health", r => r.fulfill({json: {ok: true, model: "m", modelReady: true, detail: "ok"}}));
  await page.route("**/api/analyze", async r => { analyzed = r.request().postDataBuffer().toString("latin1"); r.fulfill({json: {spec: "- gold ring"}}); });
  await page.route("**/api/check", r => r.fulfill({json: {match: true, issues: []}}));
  await page.route("**/api/generate", async r => { gens.push(r.request().postDataBuffer().toString("latin1")); r.fulfill({json: {image: genUrl, size: "1024x1536", retries: 0, flags: []}}); });
  await page.goto("http://127.0.0.1:8800/");
  await page.waitForFunction(() => document.getElementById("statusText").textContent === "接口正常");

  // 1) 折叠入口默认收起，展开后 7 栏
  console.log("collapsed by default:", await page.$eval("#moreBody", e => e.hidden));
  await page.click("#moreToggle");
  console.log("expanded:", !(await page.$eval("#moreBody", e => e.hidden)), "| aria:", await page.getAttribute("#moreToggle", "aria-expanded"));
  const labels = await page.$$eval("#slots .slot-head strong", els => els.map(e => e.textContent));
  console.log("slots:", labels.join("/"));

  // 2) 主视图栏上传 2 张、后视图栏上传 1 张（直接给隐藏 input，绕过 filechooser）
  const setSlot = async (ri, n) => {
    await page.evaluate(k => { document.querySelector("#moreBody input[type=file]").dataset.slot = String(k); }, ri);
    await page.setInputFiles("#moreBody input[type=file]", Array.from({length: n}, (_, i) => ({name: `s${ri}_${i}.png`, mimeType: "image/png", buffer: png(120, 160, [ri * 30, 120 + i * 10, 60])})));
    await page.waitForTimeout(300);
  };
  await setSlot(0, 2); await setSlot(5, 1);
  const states = await page.$$eval("#slots .slot-state", els => els.map(e => e.textContent));
  console.log("slot states:", states.filter(t => t !== "未上传").join(" | "));
  console.log("thumbnails:", await page.$$eval("#slots .slot-item img", els => els.length));

  // 3) 移除一张后再加回
  await page.click('#slots [data-unlink="0:1"]');
  await page.waitForTimeout(200);
  console.log("after unlink:", await page.$$eval("#slots .slot-item img", els => els.length));
  await setSlot(0, 1);

  // 4) 上传随手拍（产品档案应改用主视图栏照片）
  await page.setInputFiles("#fileInput", {name: "top.png", mimeType: "image/png", buffer: png(200, 200, [210, 170, 80])});
  await page.waitForTimeout(300);

  // 5) 生成后视图：有同方向参考 → 第一参考是背面栏照片，带 refs
  const cardOf = name => page.locator(".vcard").nth(["前视图","后视图","左视图","右视图","左后视图","右后视图"].indexOf(name));
  await cardOf("后视图").locator(".redo").click();
  await page.waitForFunction(() => document.querySelectorAll(".vcard .vers .chip").length >= 1, null, {timeout: 30000});
  const form = gens[gens.length - 1];
  const count = (name) => (form.match(new RegExp('name="' + name + '"', "g")) || []).length;
  console.log("back view: image=" + count("image") + " refs=" + count("refs") + " anchor=" + count("anchor") + " prompt=" + count("prompt"));
  console.log("back view prompt mentions extra refs:", form.includes("Additional images are real photos"));

  // 6) 生成左视图：无同方向参考 → 出推断提示
  await cardOf("左视图").locator(".redo").click();
  await page.waitForFunction(() => document.querySelectorAll(".vcard .vers .chip").length >= 2, null, {timeout: 30000});
  console.log("left view noref badge:", await cardOf("左视图").locator(".vnoref.show").count());
  console.log("back view noref badge:", await cardOf("后视图").locator(".vnoref.show").count());

  // 7) 产品档案：整体生成流程才会触发 analyze（ensureSpec），单卡测试里不调用属正常
  console.log("page errors:", errors.length ? errors.join(" | ") : "none");
  await browser.close();
  process.exit(errors.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
