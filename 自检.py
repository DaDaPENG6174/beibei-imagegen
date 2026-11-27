# -*- coding: utf-8 -*-
"""
贝贝生图  —  本地自检
================================================
用法：双击「自检.bat」，或在文件夹里运行  python 自检.py

它会依次检查：
  1. 三个文件是否齐全
  2. API Key / 模型 是否有效（/models）
  3. 短提示词能不能出图
  4. 页面里那段完整提示词能不能出图
最后给一句结论：能跑 / 哪里坏了。

不改任何文件，随便跑。
"""
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "自检结果")
CFG = {}


def say(msg=""):
    print(msg)
    sys.stdout.flush()


def read_cfg():
    """从 server.py 里读出配置，保证自检用的就是线上跑的 key"""
    p = os.path.join(HERE, "server.py")
    if not os.path.isfile(p):
        return False
    src = open(p, encoding="utf-8").read()
    for k in ("API_BASE", "API_KEY", "MODEL", "PORT", "HOST"):
        m = re.search(r'^%s\s*=\s*"([^"]*)"' % k, src, re.M)
        if m:
            CFG[k] = m.group(1)
        else:
            m = re.search(r'^%s\s*=\s*(\d+)' % k, src, re.M)
            if m:
                CFG[k] = m.group(1)
    return bool(CFG.get("API_BASE") and CFG.get("API_KEY"))


def headers(extra=None):
    h = {"Authorization": "Bearer " + CFG["API_KEY"],
         "Accept": "application/json",
         "User-Agent": "JinxiangSelftest/1.0"}
    if extra:
        h.update(extra)
    return h


def http(method, path, data=None, hdr=None, timeout=300):
    req = urllib.request.Request(CFG["API_BASE"] + path, data=data,
                                 headers=hdr or headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, repr(e).encode()


def step_files():
    need = ["server.py", "index.html", "启动.bat"]
    missing = [n for n in need if not os.path.isfile(os.path.join(HERE, n))]
    if missing:
        say("  [X] 缺文件：%s" % "、".join(missing))
        say("      这三个必须放在同一个文件夹里。")
        return False
    say("  [OK] server.py / index.html / 启动.bat 都在")
    return True


def step_models():
    t = time.time()
    code, raw = http("GET", "/models", timeout=45)
    if code != 200:
        say("  [X] /models 失败（%s）：%s" % (code, raw.decode("utf-8", "replace")[:200]))
        return None
    try:
        ids = [m.get("id") for m in json.loads(raw.decode()).get("data", [])]
    except Exception as e:
        say("  [X] /models 返回看不懂：%s" % e)
        return None
    ok = CFG.get("MODEL") in ids
    say("  [%s] key 有效，%d 个模型，用时 %.1f 秒"
        % ("OK" if ok else "X", len(ids), time.time() - t))
    if not ok:
        say("      配的 MODEL=%r 不在列表里。" % CFG.get("MODEL"))
        say("      可以用的图像模型：%s"
            % "、".join([i for i in ids if "image" in i]) or "（没找到）")
    return ids


def make_test_image():
    """自检用的合成参考图，避免依赖用户自己的照片"""
    try:
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (900, 1200), (232, 224, 210))
        d = ImageDraw.Draw(im)
        d.ellipse([250, 120, 650, 700], fill=(205, 168, 96), outline=(150, 116, 55), width=6)
        d.rectangle([400, 700, 500, 900], fill=(198, 160, 90))
        d.ellipse([300, 880, 600, 1000], fill=(205, 168, 96), outline=(150, 116, 55), width=6)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=95)
        return buf.getvalue(), "image/jpeg"
    except ImportError:
        # 没有 Pillow 就手写一个纯色 PNG
        import zlib, struct
        w = h = 512
        raw = b"".join(b"\x00" + bytes([235, 228, 215] * w) for _ in range(h))
        def chunk(tag, body):
            c = struct.pack(">I", len(body)) + tag + body
            return c + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw))
               + chunk(b"IEND", b""))
        return png, "image/png"


def multipart(fields, image_bytes, mime, name="reference.jpg"):
    bd = "----SelftestBoundary"
    out = []
    for k, v in fields.items():
        out.append(("--" + bd + "\r\n").encode())
        out.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode())
        out.append(str(v).encode("utf-8") + b"\r\n")
    out.append(("--" + bd + "\r\n").encode())
    out.append(('Content-Disposition: form-data; name="image"; filename="%s"\r\n' % name).encode())
    out.append(("Content-Type: %s\r\n\r\n" % mime).encode())
    out.append(image_bytes + b"\r\n")
    out.append(("--" + bd + "--\r\n").encode())
    return b"".join(out), "multipart/form-data; boundary=" + bd


def get_page_prompt():
    """从 index.html 里抓真实的 PROMPT，自检和线上同一份"""
    p = os.path.join(HERE, "index.html")
    if not os.path.isfile(p):
        return None
    src = open(p, encoding="utf-8").read()
    m = re.search(r"var PROMPT = \[(.*?)\]\.join\(\" \"\);", src, re.S)
    if not m:
        return None
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    return " ".join(json.loads('"' + x + '"') for x in parts)


def step_edit(label, prompt, img, mime, save_as=None):
    body, ctype = multipart(
        {"model": CFG["MODEL"], "prompt": prompt, "n": "1", "size": "1024x1024"},
        img, mime)
    t = time.time()
    code, raw = http("POST", "/images/edits", data=body,
                     hdr=headers({"Content-Type": ctype}), timeout=300)
    dt = time.time() - t

    if code != 200:
        try:
            e = json.loads(raw.decode()).get("error")
            em = e.get("message") if isinstance(e, dict) else str(e)
        except Exception:
            em = raw.decode("utf-8", "replace")[:160]
        say("  [X] %s 失败（%s，%.0f 秒）：%s" % (label, code, dt, em))
        return False

    try:
        it = (json.loads(raw.decode()).get("data") or [{}])[0]
    except Exception:
        say("  [X] %s 返回看不懂：%s" % (label, raw[:160]))
        return False

    b64 = it.get("b64_json")
    if b64:
        blob = base64.b64decode(b64)
    elif it.get("url"):
        try:
            with urllib.request.urlopen(it["url"], timeout=60) as r:
                blob = r.read()
        except Exception as e:
            say("  [X] %s 返回了链接但下载失败：%r" % (label, e))
            return False
    else:
        say("  [X] %s 没有返回图片" % label)
        return False

    say("  [OK] %s 出图，%.0f 秒，%d KB" % (label, dt, len(blob) // 1024))
    if save_as:
        os.makedirs(OUT, exist_ok=True)
        open(os.path.join(OUT, save_as), "wb").write(blob)
        say("       已存到 自检结果/%s" % save_as)
    return True


def main():
    say("=" * 56)
    say("   贝贝生图  —  自检")
    say("=" * 56)
    say()

    say("[1/4] 文件检查")
    if not step_files():
        return 1
    if not read_cfg():
        say("  [X] server.py 里读不到 API_BASE / API_KEY")
        return 1
    say("      模型 %s" % CFG.get("MODEL"))
    say()

    say("[2/4] 接口连通")
    ids = step_models()
    if not ids:
        say()
        say("结论：key 或网络有问题，先解决这个。")
        return 1
    say()

    say("[3/4] 短提示词出图")
    img, mime = make_test_image()
    short_ok = step_edit("短提示词", "place the item on a pure white background", img, mime)
    say()

    say("[4/4] 完整提示词出图（页面里那段）")
    prompt = get_page_prompt()
    if not prompt:
        say("  [!] index.html 里没找到 PROMPT，跳过")
        full_ok = None
    else:
        say("      提示词 %d 字符" % len(prompt))
        full_ok = step_edit("完整提示词", prompt, img, mime, "完整提示词结果.png")
    say()

    say("=" * 56)
    if short_ok and full_ok is not False:
        say("结论：全链路正常，页面上可以直接用。")
        if full_ok:
            say("      打开 自检结果/完整提示词结果.png 看看出图效果。")
        return 0
    if short_ok and full_ok is False:
        say("结论：短提示词能出图，完整提示词失败。")
        say("      多半是上游抽风，过一会儿再跑一次自检。")
        say("      连续几次都这样，就把提示词改短一点。")
        return 2
    say("结论：连短提示词都出不了图。")
    say("      看上面 [2/4] 的结果——通常是 key/余额/上游挂了。")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\n已中断。")
    except Exception as e:
        say("\n自检自己出错了：%r" % e)
    try:
        input("\n按回车关闭...")
    except Exception:
        pass
