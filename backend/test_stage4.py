"""Интеграционный тест Этапа 4 (гоняется против uvicorn на :8010)."""
import json
import subprocess
import xml.etree.ElementTree as ET

BASE = "http://localhost:8010"


def curl(*args: str) -> str:
    return subprocess.run(["curl", "-s", *args], capture_output=True, text=True).stdout


def enrich(kind: str, value: str) -> dict:
    body = json.dumps({"source_node": {"kind": kind, "value": value}})
    return json.loads(curl("-X", "POST", f"{BASE}/api/enrich",
                           "-H", "content-type: application/json", "-d", body))


# 1. PHONE: mock + находка из локальной БД утечек
d = enrich("phone", "+79990000000")
srcs = ",".join(s for n in d["nodes"] for s in n["sources"])
print("PHONE engine:", d["engine"], "| nodes:", len(d["nodes"]))
assert "local:breach-db" in srcs, "нет находки breach-db"
assert d["engine"] == "live", "engine должен подняться до live при локальном сливе"

# 2. EMAIL: тоже breach + mock
d = enrich("email", "ivan.petrov@mail.ru")
print("EMAIL engine:", d["engine"], "| nodes:", len(d["nodes"]))
assert any(n["kind"] == "event" and "yandexfood" in n["value"] for n in d["nodes"])

# 3. IMAGE: live Yandex недоступен в песочнице -> mock-фолбэк reverse-image
d = enrich("image", "https://example.com/photo.jpg")
print("IMAGE engine:", d["engine"], "| nodes:", [(n["kind"], n["value"][:40]) for n in d["nodes"]])
assert d["nodes"], "IMAGE пустой результат"
for l in d["links"]:
    assert "from" in l and "to" in l, f"нет from/to: {list(l.keys())}"

# 4. EXPORT graphml (POST снимок -> валидный GraphML)
snapshot = {
    "nodes": [
        {"id": "n1", "kind": "person", "value": "Иванов И.И.", "conf": 2, "sources": ["t"], "meta": {}},
        {"id": "n2", "kind": "email", "value": "a@b.ru", "conf": 3, "sources": ["t"], "meta": {}},
    ],
    "links": [{"id": "l1", "from": "n1", "to": "n2", "kind": "owned_by", "weight": 0.8}],
}
out = curl("-X", "POST", f"{BASE}/api/export?format=graphml",
           "-H", "content-type: application/json", "-d", json.dumps(snapshot))
root = ET.fromstring(out)  # упадёт, если XML невалидный
NS = "{http://graphml.graphdrawing.org/xmlns}"
nnodes = root.findall(f".//{NS}node")
nedges = root.findall(f".//{NS}edge")
print("EXPORT POST: OK,", len(nnodes), "nodes,", len(nedges), "edges")
assert len(nnodes) == 2 and len(nedges) == 1

# 5. GET export — серверный кеш после POST
out2 = curl(f"{BASE}/api/export?format=graphml")
assert '<node id="n1">' in out2
print("EXPORT GET: OK")

# 6. kinds включает image
ks = json.loads(curl(f"{BASE}/api/kinds"))
assert "image" in ks, ks
print("KINDS:", ks)

print("ALL STAGE-4 BACKEND TESTS PASSED")
