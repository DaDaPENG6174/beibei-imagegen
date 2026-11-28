# -*- coding: utf-8 -*-
"""
贝贝生图  —  本地服务
================================================
作用：
  1. 中转站 stariver.top 不返回 CORS 头，浏览器没法直接调，由本服务代为转发。
  2. API Key 只存在这个文件里，网页源码中不含 key，不会外泄。
  3. 上游偶发 502（Upstream request failed）时自动重试，用户不用手动再点。

用法：双击「启动.bat」，浏览器会自动打开页面。关掉黑窗口即停止。
"""
import base64
import json
import os
import re
import sys
import time
import mimetypes
import urllib.request
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ================== 配置区（要改就改这里） ==================
API_BASE = "https://stariver.top/v1"
API_KEY = "sk-your-key-here"
MODEL = "gpt-image-2.6-flare"
VISION_MODEL = "gpt-5.6-luna"   # 看图模型：产品档案提取、生成结果质检（防幻觉流程用）
HOST = "127.0.0.1"
PORT = 8710               # 可在 config.json 里加 "port": 端口号 覆盖
OPEN_BROWSER = True       # 启动后自动打开浏览器
TIMEOUT = 300             # 单次生成超时（秒）

# ---- 上游抽风时的自动重试 ----
TRIES = 3                 # 最多试几次（含第一次）
RETRY_PAUSE = 2           # 第 1 次重试前等 2 秒，往后翻倍
RETRY_ON = (429, 500, 502, 503, 504)   # 这些状态码值得重试
TOTAL_BUDGET = 480        # 一次生成最多耗多久（秒），超了就不再重试
CONTENT_TRIES = 3         # 出图后硬校验（透明底/重复视图）不过关时，最多重画几次（含第一次）
# ===========================================================

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = "index.html"
CONFIG_FILE = os.path.join(HERE, "config.json")   # 页面里「重新配置」保存的设置，优先于上面的默认值


def load_config():
    """读取 config.json（如果有），覆盖上面的默认配置"""
    global API_BASE, API_KEY, MODEL, VISION_MODEL, PORT
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        return
    API_BASE = str(c.get("api_base") or API_BASE).rstrip("/")
    API_KEY = str(c.get("api_key") or API_KEY)
    MODEL = str(c.get("model") or MODEL)
    VISION_MODEL = str(c.get("vision_model") or VISION_MODEL)
    if isinstance(c.get("port"), int) and 1024 < c["port"] < 65536:
        PORT = c["port"]


def save_config(api_base, api_key, model, vision_model):
    global API_BASE, API_KEY, MODEL, VISION_MODEL
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"api_base": api_base, "api_key": api_key, "model": model,
                   "vision_model": vision_model}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
    API_BASE, API_KEY, MODEL, VISION_MODEL = api_base, api_key, model, vision_model


load_config()


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def _auth_headers():
    return {
        "Authorization": "Bearer " + API_KEY,
        "Accept": "application/json",
        "User-Agent": "JinxiangStudio/1.0",
    }


def _friendly(status, raw):
    """把上游的原始报错翻成人话"""
    msg = (raw or "")[:400]
    try:
        j = json.loads(raw)
        err = j.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or msg
        elif isinstance(err, str):
            msg = err
        elif j.get("message"):
            msg = j["message"]
    except Exception:
        pass

    low = (msg or "").lower()
    if status in (502, 503, 504) or "upstream" in low:
        return "上游生图服务暂时抽风（HTTP %s）" % status
    if status == 401:
        return "API Key 无效或已过期（HTTP 401）。请改 server.py 里的 API_KEY。"
    if status == 403:
        return "没有权限调用这个模型（HTTP 403）。确认 MODEL 名字在 /models 列表里。"
    if status == 429:
        return "请求太频繁或额度用尽（HTTP 429）。等一分钟再试，或检查余额。"
    if status == 400:
        return "上游参数被拒（HTTP 400）：%s" % msg
    return "HTTP %s：%s" % (status, msg)


def _send(make_req):
    """发出请求，失败自动重试。返回 (json, 错误信息, 重试次数)

    make_req 是个函数：每次重试都重新构造 Request，避免 body 被消费掉。
    """
    started = time.time()
    last_err = "未知错误"
    tried = 0

    for attempt in range(1, TRIES + 1):
        tried = attempt
        wait = None
        try:
            with urllib.request.urlopen(make_req(), timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8")), None, attempt - 1
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            last_err = _friendly(e.code, raw)
            if e.code in RETRY_ON:
                wait = RETRY_PAUSE * (2 ** (attempt - 1))
            else:
                return None, last_err, attempt - 1        # 重试也没用
        except Exception as e:
            last_err = "连不上生图接口：%s" % e
            wait = RETRY_PAUSE * (2 ** (attempt - 1))

        if attempt >= TRIES:
            break
        if time.time() - started + (wait or 0) > TOTAL_BUDGET:
            break
        log("第 %d 次失败（%s），%d 秒后重试…" % (attempt, last_err, wait))
        time.sleep(wait)

    log("放弃：%d 次都没成功" % tried)
    return None, last_err, tried - 1


def upstream_health():
    """探测 key 是否可用。返回 (是否正常, 说明, 模型列表)"""
    def make():
        return urllib.request.Request(API_BASE + "/models", headers=_auth_headers(), method="GET")

    try:
        with urllib.request.urlopen(make(), timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return True, "接口正常", [m.get("id", "") for m in data.get("data", [])]
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return False, _friendly(e.code, raw), []
    except Exception as e:
        return False, "连不上上游：%s" % e, []


def build_multipart(fields, files):
    """普通字段 + 文件 → (body, content_type)"""
    boundary = "----JinxiangBoundary7d91f3a2"
    out = []
    for k, v in fields.items():
        out.append(("--" + boundary + "\r\n").encode())
        out.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode())
        out.append(str(v).encode("utf-8") + b"\r\n")
    for name, filename, mime, blob in files:
        out.append(("--" + boundary + "\r\n").encode())
        out.append(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                    % (name, filename)).encode("utf-8"))
        out.append(("Content-Type: %s\r\n\r\n" % mime).encode())
        out.append(blob + b"\r\n")
    out.append(("--" + boundary + "--\r\n").encode())
    return b"".join(out), "multipart/form-data; boundary=" + boundary


def call_edit(images, prompt, size, mask=None):
    """有参考图 → /images/edits（multipart）。images = [(文件名, mime, 字节), ...]
    第 1 张是原始产品照（权威参考），第 2 张（可选）是已确认的前视图（只用来对齐设计）。
    mask（可选）：PNG 字节，透明处为允许重画的区域，作用于第 1 张图（区域重制用）。"""
    fields = {"model": MODEL, "prompt": prompt, "n": "1", "size": size}
    field = "image[]" if len(images) > 1 else "image"
    parts = [(field, fn, mime, blob) for fn, mime, blob in images]
    if mask:
        parts.append(("mask", "mask.png", "image/png", mask))
    body, ctype = build_multipart(fields, parts)
    h = _auth_headers()
    h["Content-Type"] = ctype

    def make():
        return urllib.request.Request(API_BASE + "/images/edits", data=body,
                                      headers=h, method="POST")
    return _send(make)


def call_vision(prompt, images, max_tokens=700):
    """看图问答（chat completions）。images = [(mime, 字节), ...]。返回 (文本, 错误)"""
    content = [{"type": "text", "text": prompt}]
    for mime, blob in images:
        content.append({"type": "image_url", "image_url": {
            "url": "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode())}})
    payload = json.dumps({"model": VISION_MODEL, "max_tokens": max_tokens,
                          "messages": [{"role": "user", "content": content}]}).encode()
    h = _auth_headers()
    h["Content-Type"] = "application/json"

    def make():
        return urllib.request.Request(API_BASE + "/chat/completions", data=payload,
                                      headers=h, method="POST")
    data, err, _ = _send(make)
    if err:
        return None, err
    try:
        return data["choices"][0]["message"]["content"].strip(), None
    except Exception:
        return None, "看图模型返回了看不懂的内容"


def call_generate(prompt, size):
    """无参考图 → /images/generations（JSON）"""
    payload = json.dumps({"model": MODEL, "prompt": prompt, "n": 1, "size": size}).encode()
    h = _auth_headers()
    h["Content-Type"] = "application/json"

    def make():
        return urllib.request.Request(API_BASE + "/images/generations", data=payload,
                                      headers=h, method="POST")
    return _send(make)


# ---------- 出图后的硬校验（不靠模型，用代码判断） ----------
try:
    from PIL import Image, ImageChops, ImageFilter, ImageStat
    import io
    HAVE_PIL = True
except Exception:          # 没装 Pillow 就跳过校验，不影响出图
    HAVE_PIL = False

ALPHA_BAD = 0.02           # 透明像素超过 2% 算「透明底」
DUP_DIFF = 6.0             # 与前视图平均差异小于 6/255 算「重复视图」
BG_TOP, BG_BOTTOM = (253, 251, 246), (234, 227, 215)   # 与提示词里的渐变一致


def _gradient(size):
    w, h = size
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / float(max(h - 1, 1))
        col.putpixel((0, y), tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    return col.resize((w, h))


def _flat_rgb(im):
    """带透明的图铺到暖白渐变上；无透明的原样转 RGB"""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = _gradient(im.size)
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def inspect_image(blob, anchor_blob=None):
    """返回 (处理后的字节, mime, 问题列表)。问题：transparent / duplicate"""
    if not HAVE_PIL:
        return blob, None, []
    flags = []
    try:
        im = Image.open(io.BytesIO(blob))
        im.load()
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        out_blob, out_mime = blob, None
        if has_alpha:
            a = im.convert("RGBA").getchannel("A")
            hist = a.histogram()
            frac = sum(hist[:250]) / float(im.size[0] * im.size[1])
            if frac > ALPHA_BAD:
                flags.append("transparent")
            buf = io.BytesIO()
            _flat_rgb(im).save(buf, "PNG")          # 不论多少，交付的一律是铺好底的图
            out_blob, out_mime = buf.getvalue(), "image/png"
            im = Image.open(io.BytesIO(out_blob))
        if anchor_blob:
            ref = _flat_rgb(Image.open(io.BytesIO(anchor_blob))).resize((96, 128))
            cur = _flat_rgb(im).resize((96, 128))
            d = ImageStat.Stat(ImageChops.difference(ref, cur)).mean
            if sum(d) / len(d) < DUP_DIFF:
                flags.append("duplicate")
        return out_blob, out_mime, flags
    except Exception as e:
        log("硬校验出错，跳过：%s" % e)
        return blob, None, []


MASK_GROW = 6               # 区域重制：划圈区域向外扩几像素，盖住接缝
MASK_FEATHER = 4            # 再羽化几像素，让新旧像素自然过渡
API_SIZES = ("1024x1024", "1536x1024", "1024x1536")


def region_alpha(mask_blob, size):
    """前端遮罩（透明 = 要重画）→ L 通道：255 = 重画，0 = 保持原样。并返回扩边羽化后的版本。"""
    m = Image.open(io.BytesIO(mask_blob)).convert("RGBA")
    if m.size != size:
        m = m.resize(size, Image.NEAREST)
    region = m.getchannel("A").point(lambda a: 255 if a < 128 else 0)
    soft = region
    if MASK_GROW:
        soft = soft.filter(ImageFilter.MaxFilter(MASK_GROW * 2 + 1))
    if MASK_FEATHER:
        soft = soft.filter(ImageFilter.GaussianBlur(MASK_FEATHER))
    return region, soft


def api_mask(region):
    """给上游接口的遮罩：与第 1 张图同尺寸的 RGBA，要重画的区域 alpha=0"""
    out = Image.new("RGBA", region.size, (0, 0, 0, 255))
    out.putalpha(region.point(lambda a: 0 if a else 255))
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


RETRY_HINT = {
    "transparent": " IMPORTANT: the previous attempt had a transparent or cut-out background. "
                   "This time render a fully opaque seamless ivory-to-beige gradient background, no alpha, no cutout edges.",
    "duplicate": " IMPORTANT: the previous attempt looked identical to Image 2 (same camera angle). "
                 "This time the camera MUST be clearly moved to the requested angle; the product must be visibly rotated "
                 "compared with Image 2, only its design is shared.",
}
FLAG_TEXT = {"transparent": "背景透明/有抠图残边", "duplicate": "与前视图几乎一样，没转角度"}


def parse_multipart(raw, boundary):
    fields, files = {}, {}
    files_multi = {}          # 同名字段收集为列表（区域重制之外的 refs 多图参考）
    delim = b"--" + boundary.encode()
    for ch in raw.split(delim):
        if not ch or ch in (b"--\r\n", b"--", b"\r\n"):
            continue
        ch = ch.lstrip(b"\r\n")
        if ch.startswith(b"--") or b"\r\n\r\n" not in ch:
            continue
        head, body = ch.split(b"\r\n\r\n", 1)
        if body.endswith(b"\r\n"):
            body = body[:-2]
        head_txt = head.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]*)"', head_txt)
        if not name_m:
            continue
        name = name_m.group(1)
        fn_m = re.search(r'filename="([^"]*)"', head_txt)
        ct_m = re.search(r"Content-Type:\s*([^\r\n]+)", head_txt, re.I)
        if fn_m:
            item = (fn_m.group(1) or "image.png",
                    (ct_m.group(1).strip() if ct_m else "image/png"),
                    body)
            if name == "refs":                      # 只有多参考字段允许重复
                files_multi.setdefault("refs", []).append(item)
            elif name in files:
                files[name] = item                  # 其余字段沿用旧语义：后者覆盖
            else:
                files[name] = item
        else:
            fields[name] = body.decode("utf-8", "replace")
    files.update(files_multi)
    return fields, files


class Handler(BaseHTTPRequestHandler):
    server_version = "JinxiangStudio"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- 输出工具 ----------
    def _json(self, obj, code=200):
        blob = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _serve_file(self, name):
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            self._json({"error": "找不到 " + name}, 404)
            return
        with open(path, "rb") as f:
            blob = f.read()
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if mime.startswith("text/"):
            mime += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            self._serve_file(PAGE)
        elif p == "/api/health":
            ok, detail, ids = upstream_health()
            self._json({
                "ok": ok,
                "model": MODEL,
                "modelReady": (MODEL in ids) if ids else False,
                "detail": detail,
            })
        elif p == "/api/config":
            # 不回传 Key，只告诉页面有没有配置
            self._json({"api_base": API_BASE, "model": MODEL, "vision_model": VISION_MODEL,
                        "has_key": bool(API_KEY) and API_KEY != "sk-your-key-here"})
        elif p.startswith("/api/"):
            self._json({"error": "未知接口"}, 404)
        else:
            self._serve_file(p.lstrip("/"))

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/generate":
            self._handle_generate()
        elif p == "/api/analyze":
            self._handle_analyze()
        elif p == "/api/check":
            self._handle_check()
        elif p == "/api/config":
            self._handle_config()
        elif p == "/api/inpaint":
            self._handle_inpaint()
        else:
            self._json({"error": "未知接口"}, 404)

    def _handle_config(self):
        """页面「重新配置」：保存接口地址、Key、模型。只接受本页面发来的请求。"""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin not in ("http://" + host,):
            self._json({"error": "只允许本机页面修改配置"}, 403)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            self._json({"error": "请求格式不对"}, 400)
            return
        base = str(body.get("api_base") or "").strip().rstrip("/")
        model = str(body.get("model") or "").strip()
        vision = str(body.get("vision_model") or "").strip()
        key = str(body.get("api_key") or "").strip() or API_KEY   # 留空 = 保留原 Key
        if not re.match(r"^https?://[^\s/]+", base):
            self._json({"error": "接口地址要以 http:// 或 https:// 开头"}, 400)
            return
        if not model or not vision:
            self._json({"error": "生图模型和看图模型都要填"}, 400)
            return
        save_config(base, key, model, vision)
        log("配置已更新：%s / %s / %s" % (API_BASE, MODEL, VISION_MODEL))
        self._json({"ok": True})

    def _handle_inpaint(self):
        """区域重制：只重画用户划圈的区域，其余像素由本服务按原图逐像素保留。

        表单：base（要修改的生成图）、marked（带红圈的同一张图）、reference（原始产品照）、
              mask（PNG，透明 = 划圈区域）、prompt、size
        """
        if not HAVE_PIL:
            self._json({"error": "区域重制需要 Pillow：请在命令行运行 pip install pillow 后重启"}, 500)
            return
        fields, files = self._read_form()
        if files is None:
            return
        base, marked, ref, mask = (files.get(k) for k in ("base", "marked", "reference", "mask"))
        prompt = (fields.get("prompt") or "").strip()
        if not (base and marked and ref and mask) or not prompt:
            self._json({"error": "缺少图片、划圈区域或提示词"}, 400)
            return
        try:
            base_im = _flat_rgb(Image.open(io.BytesIO(base[2])))
            region, soft = region_alpha(mask[2], base_im.size)
        except Exception as e:
            self._json({"error": "图片或划圈区域无法读取：%s" % e}, 400)
            return
        if not region.getbbox():
            self._json({"error": "没有划出区域，请先在图上圈出要重制的部分"}, 400)
            return

        buf = io.BytesIO()
        base_im.save(buf, "PNG")
        size = "%dx%d" % base_im.size
        if size not in API_SIZES:
            size = (fields.get("size") or "").strip() or "auto"
        refs = [("current.png", "image/png", buf.getvalue()),
                (marked[0] or "marked.png", marked[1], marked[2]),
                (ref[0] or "reference.jpg", ref[1], ref[2])]
        t0 = time.time()
        data, err, tries = call_edit(refs, prompt, size, api_mask(region))
        mask_used = True
        if err and "HTTP 400" in err:
            # 部分中转站不支持 mask 字段：改为只靠提示词和红圈定位，区域外仍由本服务保证不变
            log("上游拒绝了遮罩参数（%s），改为不带遮罩重试" % err)
            data, err, more = call_edit(refs, prompt, size)
            tries += more
            mask_used = False
        if err:
            self._json({"error": err}, 502)
            return
        items = (data or {}).get("data") or []
        if not items or not (items[0].get("b64_json") or items[0].get("url")):
            self._json({"error": "接口返回了空结果"}, 502)
            return
        try:
            if items[0].get("b64_json"):
                blob = base64.b64decode(items[0]["b64_json"])
            else:
                req = urllib.request.Request(items[0]["url"], headers={"User-Agent": "JinxiangStudio/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    blob = r.read()
            gen = _flat_rgb(Image.open(io.BytesIO(blob)))
        except Exception as e:
            self._json({"error": "生图结果无法读取：%s" % e}, 502)
            return
        if gen.size != base_im.size:
            gen = gen.resize(base_im.size, Image.LANCZOS)

        # 只取划圈区域（扩边 + 羽化）内的新像素，区域外一律用原图像素
        out = Image.composite(gen, base_im, soft)
        outside = soft.point(lambda a: 255 if a == 0 else 0)
        diff = ImageChops.difference(out, base_im).convert("L")
        changed_outside = ImageStat.Stat(diff, mask=outside).sum[0] > 0
        buf = io.BytesIO()
        out.save(buf, "PNG")
        x0, y0, x1, y1 = region.getbbox()
        log("区域重制完成，用时 %.0f 秒，区域 %dx%d，遮罩参数%s"
            % (time.time() - t0, x1 - x0, y1 - y0, "已使用" if mask_used else "未被接口支持"))
        self._json({
            "image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
            "retries": tries, "mask_used": mask_used,
            "outside_unchanged": not changed_outside,
            "region": [x0, y0, x1, y1],
        })

    def _read_form(self):
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^\s;]+)", ctype)
        if "multipart/form-data" not in ctype or not m:
            self._json({"error": "请求格式不对，需要 multipart"}, 400)
            return None, None
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        return parse_multipart(raw, m.group(1).strip('"'))

    def _handle_analyze(self):
        """读一张原图，写出「产品档案」：之后每个视图的提示词都会带上它，防止各视图各编各的"""
        fields, files = self._read_form()
        if files is None:
            return
        img = files.get("image")
        if not img:
            self._json({"error": "缺少图片"}, 400)
            return
        prompt = (
            "You are a jewelry product inspector. Look at the photo and write a factual product spec "
            "that a photographer will use to reproduce this exact piece from other angles. "
            "Use short bullet lines in English covering: item type; overall shape and proportions "
            "(e.g. ring/oval/round, thickness, width relative to size); metal color and finish "
            "(matte/polished/brushed); every decorative element with exact counts and positions; "
            "pattern or engraving description; gemstones (count, cut, color, setting) or 'none'; "
            "closure/clasp/bail/pin type if visible; sides that are visible and sides NOT visible. "
            "Describe ONLY what you can actually see. For anything not visible write 'not visible'. "
            "Never guess brand names. Maximum 170 words, no preamble.")
        text, err = call_vision(prompt, [(img[1], img[2])], 500)
        if err:
            self._json({"error": err}, 502)
            return
        self._json({"spec": text})

    def _handle_check(self):
        """质检：把原图和生成的视图对比，报告设计上的不一致（只做提示，不拦截）"""
        fields, files = self._read_form()
        if files is None:
            return
        ref, gen = files.get("image"), files.get("generated")
        if not ref or not gen:
            self._json({"error": "缺少图片"}, 400)
            return
        view = fields.get("view", "")
        prompt = (
            "Image 1 is the ORIGINAL photo of a jewelry piece. Image 2 is an AI-generated '%s' of the same piece. "
            "List only REAL design discrepancies: different shape or proportions, wrong number of stones or motifs, "
            "pattern or engraving changed/invented/missing, different metal color or finish, wrong or invented closure, "
            "extra or missing parts, deformed geometry, text/logo added. "
            "IGNORE differences caused only by the different camera angle, lighting or background. "
            "For parts not visible in Image 1, do not flag them unless they look implausible or contradict visible design. "
            "Reply with JSON only, no markdown: {\"match\": true|false, \"issues\": [\"short issue in Chinese\", ...]} "
            "(max 3 issues; match=true with empty issues if nothing real is wrong)." % view)
        text, err = call_vision(prompt, [(ref[1], ref[2]), (gen[1], gen[2])], 400)
        if err:
            self._json({"error": err}, 502)
            return
        try:
            j = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
            issues = [str(x) for x in (j.get("issues") or [])][:3]
            self._json({"match": bool(j.get("match", not issues)) and not issues, "issues": issues})
        except Exception:
            self._json({"error": "质检结果看不懂：" + text[:120]}, 502)

    def _handle_generate(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._json({"error": "请求格式不对，需要 multipart"}, 400)
            return
        m = re.search(r"boundary=([^\s;]+)", ctype)
        if not m:
            self._json({"error": "缺少 boundary"}, 400)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 60 * 1048576:
            self._json({"error": "请求太大：原图加参考图请控制在 60MB 内"}, 400)
            return
        raw = self.rfile.read(length)
        fields, files = parse_multipart(raw, m.group(1).strip('"'))

        prompt = (fields.get("prompt") or "").strip()
        size = (fields.get("size") or "").strip() or "1024x1024"
        img = files.get("image")
        anchor = files.get("anchor")      # 可选：已确认的前视图，用来对齐设计
        extra_refs = [r for r in (files.get("refs") or []) if r[2]][:4]   # 可选：其他视角实拍，最多 4 张
        for r in extra_refs:
            if len(r[2]) > 15 * 1048576:
                self._json({"error": "单张参考图超过 15MB，请先压缩"}, 400)
                return

        if not prompt:
            self._json({"error": "缺少提示词"}, 400)
            return

        t0 = time.time()
        total_tries = 0
        best = None                      # (url, flags)
        cur_prompt = prompt
        anchor_blob = anchor[2] if (anchor and anchor[2]) else None

        for content_try in range(1, CONTENT_TRIES + 1):
            tries = 0
            data = err = None

            # 上游偶尔返回空 data，这种也值得再要一次
            for attempt in range(1, TRIES + 1):
                if img and img[2]:
                    # 顺序：第一参考（权威）→ 其他视角实拍 → 已确认的前视图（只对齐设计）
                    refs = [(img[0], img[1], img[2])]
                    refs += [(r[0], r[1], r[2]) for r in extra_refs]
                    if anchor_blob:
                        refs.append((anchor[0], anchor[1], anchor[2]))
                    data, err, tries = call_edit(refs, cur_prompt, size)
                else:
                    data, err, tries = call_generate(cur_prompt, size)
                total_tries += tries

                items = (data or {}).get("data") or []
                if items and (items[0].get("b64_json") or items[0].get("url")):
                    break
                if err:
                    break                       # _send 已经重试过了，不再叠加
                err = "接口返回了空结果"
                if attempt < TRIES:
                    log("返回空结果，%d 秒后重试…" % RETRY_PAUSE)
                    time.sleep(RETRY_PAUSE)

            if err:
                if best:                         # 前面已经有可用的，就交付前面的
                    break
                log("生成失败：%s" % err)
                if "抽风" in err:
                    err += "。已自动重试 %d 次仍未成功，等 10 秒再点一次即可，不算你的问题。" % total_tries
                self._json({"error": err}, 502)
                return

            it = ((data or {}).get("data") or [])[0]
            blob = mime = None
            if it.get("b64_json"):
                blob, mime = base64.b64decode(it["b64_json"]), "image/png"
            else:
                # 上游只给了链接：由本服务下载后转成 data URL，
                # 这样「下载图片」不受跨域限制，链接过期也不影响
                try:
                    req = urllib.request.Request(it["url"], headers={"User-Agent": "JinxiangStudio/1.0"})
                    with urllib.request.urlopen(req, timeout=60) as r:
                        blob = r.read()
                        mime = r.headers.get_content_type() or "image/png"
                    if not mime.startswith("image/"):
                        mime = "image/png"
                except Exception as e:
                    log("下载上游图片失败，改为直接返回链接：%s" % e)

            if blob is None:                     # 只有链接，没法校验
                best = (it["url"], [])
                break

            blob, new_mime, flags = inspect_image(blob, anchor_blob)
            mime = new_mime or mime
            url = "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode())
            if best is None or len(flags) < len(best[1]):
                best = (url, flags)
            if not flags:
                break
            log("硬校验未通过（%s），第 %d/%d 次" % ("、".join(flags), content_try, CONTENT_TRIES))
            cur_prompt = prompt + "".join(RETRY_HINT[f] for f in flags)

        url, flags = best
        log("生成成功，用时 %.0f 秒，重试 %d 次，校验：%s"
            % (time.time() - t0, total_tries, "、".join(flags) or "通过"))
        self._json({"image": url, "size": size, "retries": total_tries,
                    "flags": [{"code": f, "text": FLAG_TEXT[f]} for f in flags]})


def main():
    port = PORT
    httpd = None
    for _ in range(12):
        try:
            httpd = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            port += 1
    if httpd is None:
        print("端口 8710-8722 都被占用了，起不来。")
        input("按回车退出...")
        return

    ok, detail, _ = upstream_health()
    print("=" * 58)
    print("   贝贝生图")
    print("=" * 58)
    print("   页面地址： http://%s:%d" % (HOST, port))
    print("   生图模型： %s" % MODEL)
    print("   接口状态： %s" % ("正常 ✓" if ok else "异常 ✗  " + detail))
    print("   失败重试： 最多 %d 次（上游抽风时自动重来）" % TRIES)
    print("-" * 58)
    print("   关掉这个黑窗口 = 停止服务")
    print("=" * 58)

    if OPEN_BROWSER:
        webbrowser.open("http://%s:%d/" % (HOST, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
