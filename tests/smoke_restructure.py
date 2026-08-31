"""End-to-end smoke test after the restructure: auth, history, RAG, SQL path."""
import json, urllib.error, urllib.request
BASE = "http://127.0.0.1:8502"

def call(method, path, token=None, body=None, timeout=180):
    req = urllib.request.Request(BASE + path, method=method)
    if token: req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode(); req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}

ok = lambda c: "PASS" if c else "FAIL"
print("1. AUTH")
st, res = call("POST", "/api/auth/login", body={"username":"analyst-1","password":"changeme123"})
tok = res.get("token") or res.get("access_token")
print(f"   login -> {st}  {ok(st==200 and tok)}")

print("2. HISTORY (reads app.conversations)")
st, res = call("GET", "/api/history?limit=3", tok)
print(f"   GET /api/history -> {st}, {len(res.get('conversations',[]))} thread(s)  {ok(st==200)}")

print("3. RAG STATUS (Qdrant KB built from data/)")
st, res = call("GET", "/api/rag/status", tok)
print(f"   GET /api/rag/status -> {st}  points={res.get('points')}  {ok(st==200)}")

print("4. HEALTH")
st, res = call("GET", "/health", tok)
print(f"   GET /health -> {st}  {res.get('status')}  components={res.get('components')}")

print("5. LIVE DATA QUERY (entity resolver + SQL gen + DB)")
st, res = call("POST", "/api/query", tok, {"question": "total person days in 2023-24"})
ans = (res.get("answer") or "")[:90]
print(f"   POST /api/query -> {st}")
print(f"   route={res.get('route')}  rows={res.get('row_count')}")
print(f"   answer: {ans}")
print(f"   {ok(st==200 and res.get('answer'))}")

print("6. RAG QUERY (knowledge path)")
st, res = call("POST", "/api/query", tok, {"question": "who is eligible for PMAY-G?"})
print(f"   POST /api/query -> {st}  route={res.get('route')}")
print(f"   answer: {(res.get('answer') or '')[:90]}")
print(f"   {ok(st==200 and res.get('answer'))}")
