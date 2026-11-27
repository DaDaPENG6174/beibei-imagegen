# 区域重制服务端测试：用假的上游接口，不产生任何真实调用和费用
import base64, io, json, sys, threading, urllib.request
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
from PIL import Image, ImageChops, ImageDraw

W, H = 1024, 1536
base = Image.new("RGB", (W, H))
px = base.load()
for y in range(H):
    for x in range(0, W, 1):
        px[x, y] = ((x * 7 + y) % 256, (y * 3) % 256, (x + y * 5) % 256)
REGION = (300, 500, 600, 800)

def png(im):
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

mask = Image.new("RGBA", (W, H), (0, 0, 0, 255))
ImageDraw.Draw(mask).ellipse(REGION, fill=(0, 0, 0, 0))
calls = []

MODE = {"reject_mask": False}

def fake_edit(images, prompt, size, mask=None):
    calls.append({"n_images": len(images), "has_mask": mask is not None, "size": size, "prompt": prompt, "mask": mask})
    if mask is not None and MODE["reject_mask"]:
        return None, "上游参数被拒（HTTP 400）：unknown field mask", 0
    red = Image.new("RGB", (W // 2, H // 2), (255, 0, 0))      # 故意返回不同尺寸、整张都变了的图
    return {"data": [{"b64_json": base64.b64encode(png(red)).decode()}]}, None, 0

server.call_edit = fake_edit
httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d/api/inpaint" % httpd.server_address[1]

def post(mask_png, prompt="fix it"):
    fields = {"prompt": prompt, "size": "1024x1536"}
    files = [("base", "current.png", "image/png", png(base)), ("marked", "marked.png", "image/png", png(base)),
             ("reference", "reference.jpg", "image/png", png(base)), ("mask", "mask.png", "image/png", mask_png)]
    body, ctype = server.build_multipart(fields, files)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def decode(d):
    return Image.open(io.BytesIO(base64.b64decode(d["image"].split(",", 1)[1]))).convert("RGB")

ok = True
def check(name, cond):
    global ok
    ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name)

status, d = post(png(mask))
out = decode(d)
check("returns 200 with image", status == 200)
check("output same size as base", out.size == (W, H))
grow = server.MASK_GROW + server.MASK_FEATHER * 3
outside = Image.new("L", (W, H), 255)
ImageDraw.Draw(outside).rectangle((REGION[0] - grow, REGION[1] - grow, REGION[2] + grow, REGION[3] + grow), fill=0)
diff = ImageChops.difference(out, base).convert("L")
check("pixels outside region are identical to base", ImageChops.multiply(diff, outside).getbbox() is None)
cx, cy = (REGION[0] + REGION[2]) // 2, (REGION[1] + REGION[3]) // 2
check("pixels inside region come from model", out.getpixel((cx, cy)) == (255, 0, 0))
check("server reports outside unchanged", d["outside_unchanged"] is True)
check("mask used", d["mask_used"] is True)
m = Image.open(io.BytesIO(calls[-1]["mask"])).convert("RGBA")
check("api mask transparent inside, opaque outside", m.getpixel((cx, cy))[3] == 0 and m.getpixel((5, 5))[3] == 255)
check("3 images sent (current, marked, reference)", calls[-1]["n_images"] == 3)
check("size forwarded", calls[-1]["size"] == "1024x1536")

MODE["reject_mask"] = True
calls.clear()
status, d = post(png(mask))
check("falls back without mask on HTTP 400", status == 200 and d["mask_used"] is False and [c["has_mask"] for c in calls] == [True, False])
check("fallback still keeps outside identical", ImageChops.multiply(ImageChops.difference(decode(d), base).convert("L"), outside).getbbox() is None)

status, d = post(png(Image.new("RGBA", (W, H), (0, 0, 0, 255))))
check("empty region rejected with 400", status == 400 and "没有划出区域" in d["error"])
status, d = post(png(mask), prompt="")
check("missing prompt rejected", status == 400)
httpd.shutdown()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
