# 多参考图（refs）服务端测试：假上游，不产生真实调用
import base64, io, json, sys, threading, urllib.request, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
from PIL import Image

W, H = 64, 96

def png(color):
    im = Image.new("RGB", (W, H), color)
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

MAIN, REF1, REF2, ANCHOR = png((200, 170, 90)), png((90, 140, 200)), png((90, 200, 140)), png((250, 250, 250))
calls = []

def fake_edit(images, prompt, size):
    calls.append({"images": [i[2] for i in images], "prompt": prompt, "size": size})
    return {"data": [{"b64_json": base64.b64encode(png((1, 2, 3))).decode()}]}, None, 0

server.call_edit = fake_edit
httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d/api/generate" % httpd.server_address[1]

def post(files, fields=None):
    body, ctype = server.build_multipart(fields or {"prompt": "p", "size": "1024x1536"}, files)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

ok = True
def check(name, cond):
    global ok
    ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name)

# 1) image + 2 refs + anchor → 上游按 [主, ref1, ref2, anchor] 顺序收到 4 张
status, d = post([("image", "m.png", "image/png", MAIN), ("refs", "a.png", "image/png", REF1),
                  ("refs", "b.png", "image/png", REF2), ("anchor", "f.png", "image/png", ANCHOR)])
check("generate ok", status == 200 and "image" in d)
check("upstream got 4 images in order", calls[-1]["images"] == [MAIN, REF1, REF2, ANCHOR])

# 2) 无 refs、无 anchor → 旧行为，只发 1 张
calls.clear()
status, d = post([("image", "m.png", "image/png", MAIN)])
check("no refs keeps single-image behavior", status == 200 and len(calls[-1]["images"]) == 1)

# 3) refs 超过 4 张 → 截断到 4（共 5 张含主图）
calls.clear()
files = [("image", "m.png", "image/png", MAIN)] + [("refs", "r%d.png" % i, "image/png", png((i, i, i))) for i in range(6)]
status, d = post(files)
check("refs capped at 4", status == 200 and len(calls[-1]["images"]) == 5)

# 4) 单张 refs 超 15MB → 400
huge = png((9, 9, 9)) + b"\0" * (16 * 1048576)
status, d = post([("image", "m.png", "image/png", MAIN), ("refs", "big.png", "image/png", huge)])
check("oversized ref rejected with 400", status == 400 and "15MB" in d["error"])

# 5) parse_multipart 重复字段收集、单字段后者覆盖
raw, boundary = server.build_multipart(
    {"prompt": "first", "extra": "x", "prompt": "second"},
    [("image", "a.png", "image/png", MAIN), ("refs", "r1.png", "image/png", REF1), ("refs", "r2.png", "image/png", REF2)])
fields, files = server.parse_multipart(raw, boundary.split("=")[1])
check("repeated refs collected", [f[2] for f in files["refs"]] == [REF1, REF2])
check("single field keeps last value", fields["prompt"] == "second")
check("image field intact", files["image"][2] == MAIN)

httpd.shutdown()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
