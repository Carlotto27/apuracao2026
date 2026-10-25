#!/usr/bin/env python3
"""
Apuração 2026 ao vivo — site local com mapa.
Rode:  python3 apuracao2026.py      (abre http://localhost:8026, 2º turno de Presidente)
1º turno: http://localhost:8026/?turno=1

Puxa os arquivos JSON públicos do TSE (resultados.tse.jus.br) e atualiza sozinho a cada 5 segundos.
Nada além do Python 3 é necessário.
"""
import base64, hashlib, hmac, json, os, ssl, uuid, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_ssl_context():
    """Usa os certificados padrão; se faltarem (Python do python.org no Mac), pega os do macOS."""
    ctx = ssl.create_default_context()
    try:
        import certifi  # se estiver instalado, é o mais simples
        ctx.load_verify_locations(certifi.where())
        return ctx
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            pem = subprocess.run(
                ["security", "find-certificate", "-a", "-p",
                 "/System/Library/Keychains/SystemRootCertificates.keychain"],
                capture_output=True, timeout=20).stdout
            if pem:
                f = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
                f.write(pem); f.close()
                ctx.load_verify_locations(f.name)
        except Exception as e:
            print("Aviso: não consegui carregar os certificados do macOS:", e)
    return ctx


SSL_CTX = make_ssl_context()

NA_NUVEM = bool(os.environ.get("PORT"))     # o Render define PORT; no seu Mac fica local
PORT = int(os.environ.get("PORT", 8026))
HOST = "0.0.0.0" if NA_NUVEM else "127.0.0.1"

# ---------- cobrança (só liga se houver o token do Mercado Pago) ----------
MP_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "").strip()
PRECO = float(os.environ.get("PRECO", "20"))
HORAS_ACESSO = int(os.environ.get("HORAS_ACESSO", "24"))
CODIGO_LIVRE = os.environ.get("CODIGO_LIVRE", "").strip()      # código para você entrar sem pagar
SEGREDO = (os.environ.get("SEGREDO") or uuid.uuid4().hex).encode()
COBRAR = bool(MP_TOKEN)
TSE = "https://resultados.tse.jus.br"
GEO = "https://raw.githubusercontent.com/codeforamerica/click_that_hood/master/public/data/brazil-states.geojson"
TTL = 4             # segundos de cache por arquivo do TSE (o site consulta a cada 5 s)
_cache, _lock = {}, threading.Lock()


def fetch(url, ttl):
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1], hit[2]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "apuracao-local/1.0"})
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, b""
    except Exception as e:
        print(f"Erro ao baixar {url}\n  -> {e}")
        status, body = 502, str(e).encode()
    with _lock:
        _cache[url] = (now, status, body)
    return status, body


# ---------- histórico da apuração de Presidente (o TSE só mostra o momento atual) ----------
PASTA = os.path.dirname(os.path.abspath(__file__))
ELEICOES_PRES = ["6257", "6258"]          # 1º e 2º turno
_hist, _hist_lock = {}, threading.Lock()


def _num(v):
    if v is None or v == "" or isinstance(v, (dict, list)):
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    try:
        return float(s.replace(".", "").replace(",", ".")) if "," in s else float(s)
    except ValueError:
        return None


def _resumo(j):
    cands, nomes, info = {}, {}, {}
    def rec(o):
        if isinstance(o, list):
            for x in o:
                rec(x)
        elif isinstance(o, dict):
            if "vap" in o and (o.get("nm") or o.get("nmu")):
                n = str(o.get("n", ""))
                cands[n] = int(_num(o.get("vap")) or 0)
                nomes[n] = o.get("nmu") or o.get("nm")
                return
            if "pst" not in info and o.get("pst") is not None:
                info["pst"] = _num(o["pst"])
            if "ht" not in info and (o.get("ht") or o.get("hg")):
                info["ht"] = o.get("ht") or o.get("hg")
                info["dt"] = o.get("dt") or o.get("dg")
            for v in o.values():
                rec(v)
    rec(j)
    return cands, nomes, info


def _arq(e):
    return os.path.join(PASTA, f"historico-presidente-{e}.json")


def _carregar_hist():
    for e in ELEICOES_PRES:
        try:
            with open(_arq(e), encoding="utf-8") as fh:
                _hist[e] = json.load(fh)
        except Exception:
            _hist[e] = {"nomes": {}, "pontos": []}


def _gravador():
    while True:
        for e in ELEICOES_PRES:
            url = f"{TSE}/oficial/ele2026/{e}/dados/br/br-c0001-e{e.zfill(6)}-u.json"
            st, body = fetch(url, TTL)
            if st != 200:
                st, body = fetch(url.replace("/dados/", "/dados-simplificados/").replace("-u.json", "-r.json"), TTL)
            if st != 200:
                continue
            try:
                cands, nomes, info = _resumo(json.loads(body))
            except Exception:
                continue
            if not cands or not sum(cands.values()):
                continue
            with _hist_lock:
                h = _hist.setdefault(e, {"nomes": {}, "pontos": []})
                ult = h["pontos"][-1] if h["pontos"] else None
                if ult and ult["c"] == cands and ult.get("pst") == info.get("pst"):
                    continue
                h["nomes"].update(nomes)
                h["pontos"].append({"pst": info.get("pst"), "ht": info.get("ht"), "dt": info.get("dt"),
                                    "local": time.strftime("%H:%M:%S"), "c": cands})
                tmp = _arq(e) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(h, fh, ensure_ascii=False)
                os.replace(tmp, _arq(e))
        time.sleep(5)


# ---------- acesso pago ----------
def _assinar(txt):
    return hmac.new(SEGREDO, txt.encode(), hashlib.sha256).hexdigest()[:32]


def gerar_cookie(ref):
    exp = int(time.time()) + HORAS_ACESSO * 3600
    corpo = f"{exp}.{ref}"
    return f"{corpo}.{_assinar(corpo)}"


def cookie_valido(valor):
    try:
        exp, ref, sig = valor.rsplit(".", 2)
        return hmac.compare_digest(sig, _assinar(f"{exp}.{ref}")) and int(exp) > time.time()
    except Exception:
        return False


def mp(metodo, caminho, dados=None):
    req = urllib.request.Request("https://api.mercadopago.com" + caminho, method=metodo,
                                 data=json.dumps(dados).encode() if dados is not None else None,
                                 headers={"Authorization": f"Bearer {MP_TOKEN}", "Content-Type": "application/json",
                                          "X-Idempotency-Key": uuid.uuid4().hex})
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 502, {"message": str(e)}


def criar_pix(email):
    from datetime import datetime, timedelta, timezone
    vence = (datetime.now(timezone(timedelta(hours=-3))) + timedelta(minutes=30)).isoformat(timespec="milliseconds")
    st, r = mp("POST", "/v1/payments", {
        "transaction_amount": PRECO, "description": "Acesso Apuração 2026 ao vivo",
        "payment_method_id": "pix", "payer": {"email": email}, "date_of_expiration": vence})
    if st not in (200, 201):
        print("Erro ao criar Pix no Mercado Pago:", st, r.get("message"), r.get("cause"))
        return None
    tx = (r.get("point_of_interaction") or {}).get("transaction_data") or {}
    return {"id": r.get("id"), "qr": tx.get("qr_code"), "img": tx.get("qr_code_base64")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def liberado(self):
        if not COBRAR:
            return True
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == "acesso" and cookie_valido(v):
                return True
        return False

    def send_cookie(self, body, ref):
        seguro = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", f"acesso={gerar_cookie(ref)}; Max-Age={HORAS_ACESSO*3600}; Path=/; HttpOnly; SameSite=Lax{seguro}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            dados = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
        except Exception:
            dados = {}
        if path == "/pagar" and COBRAR:
            email = str(dados.get("email", "")).strip()[:120]
            if "@" not in email or "." not in email.split("@")[-1]:
                return self.send(400, json.dumps({"erro": "Digite um e-mail válido."}).encode())
            pix = criar_pix(email)
            if not pix or not pix["qr"]:
                return self.send(502, json.dumps({"erro": "Não consegui gerar o Pix agora. Tente de novo em instantes."}).encode())
            return self.send(200, json.dumps(pix).encode())
        if path == "/entrar" and COBRAR:
            cod = str(dados.get("codigo", "")).strip()
            if CODIGO_LIVRE and hmac.compare_digest(cod, CODIGO_LIVRE):
                return self.send_cookie(b'{"ok":true}', "livre")
            time.sleep(1)
            return self.send(403, json.dumps({"erro": "Código inválido."}).encode())
        self.send(404, b"{}")

    def do_GET(self):
        path = self.path.split("#")[0]
        if path.startswith("/status") and COBRAR:
            pid = "".join(ch for ch in path.split("id=")[-1] if ch.isdigit())[:20]
            if not pid:
                return self.send(400, b"{}")
            st, r = mp("GET", f"/v1/payments/{pid}")
            if st == 200 and r.get("status") == "approved":
                return self.send_cookie(b'{"status":"approved"}', pid)
            return self.send(200, json.dumps({"status": r.get("status", "pending")}).encode())
        if path == "/geo":
            s, b = fetch(GEO, 86400)
            return self.send(s, b)
        if not self.liberado():
            if path == "/" or path.startswith("/?") or path == "/index.html":
                return self.send(200, PAYWALL.replace("{{PRECO}}", f"{PRECO:.2f}".replace(".", ","))
                                 .replace("{{HORAS}}", str(HORAS_ACESSO)).encode("utf-8"), "text/html; charset=utf-8")
            return self.send(401, b'{"erro":"acesso"}')
        if path == "/" or path.startswith("/?") or path == "/index.html":
            return self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path.startswith("/tse/"):
            s, b = fetch(TSE + path[4:].split("?")[0], TTL)
            return self.send(s, b)
        if path.startswith("/historico"):
            e = path.split("e=")[-1][:4] if "e=" in path else "6257"
            with _hist_lock:
                body = json.dumps(_hist.get(e, {"nomes": {}, "pontos": []}), ensure_ascii=False).encode("utf-8")
            return self.send(200, body)
        if path == "/geo":
            s, b = fetch(GEO, 86400)
            return self.send(s, b)
        self.send(404, b"{}")


PAYWALL = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Apuração 2026 ao vivo</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@75..100,400..800&family=Public+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#E9ECE8;--panel:#F8F9F6;--ink:#161B22;--muted:#5B6470;--line:#C8CEC6;--confirma:#1F8A4C;--corrige:#D9692A;--focus:#1D4ED8;
  --display:"Archivo","Arial Narrow",system-ui,sans-serif;--body:"Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#141920;--panel:#1C232C;--ink:#E8ECEF;--muted:#9AA4AF;--line:#323B46}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body)}
body{padding:env(safe-area-inset-top,0) 0 env(safe-area-inset-bottom,0);min-height:100vh;display:grid;place-items:center}
.card{width:min(440px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:26px 24px;margin:24px 0}
h1{font-family:var(--display);font-stretch:78%;font-weight:800;font-size:34px;margin:0 0 4px;line-height:1.05}
.live{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;color:var(--confirma);margin-bottom:10px}
.live i{width:9px;height:9px;border-radius:50%;background:var(--confirma)}
p{line-height:1.5;margin:8px 0;font-size:15px}
ul{margin:12px 0 18px;padding-left:20px;line-height:1.7;font-size:15px}
.preco{font-family:var(--display);font-stretch:80%;font-weight:800;font-size:40px}
.preco small{font:500 14px var(--body);color:var(--muted);margin-left:6px}
label{display:block;font-size:14px;font-weight:600;margin:16px 0 6px}
input{font:16px var(--body);padding:11px 12px;border-radius:7px;border:1.5px solid var(--line);background:var(--bg);color:var(--ink);width:100%}
button{font:600 16px var(--body);padding:12px 16px;border-radius:7px;border:0;background:var(--confirma);color:#fff;width:100%;margin-top:10px;cursor:pointer}
button.sec{background:transparent;color:var(--ink);border:1.5px solid var(--line);font-size:14px;padding:9px}
button:disabled{opacity:.6;cursor:wait}
button:focus-visible,input:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.qr{display:block;width:220px;max-width:100%;margin:14px auto;border-radius:8px;background:#fff;padding:8px}
textarea{width:100%;font:12px ui-monospace,Menlo,monospace;padding:8px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--ink);resize:none}
.msg{font-size:14px;color:var(--corrige);min-height:20px;margin-top:8px}
.esp{display:flex;align-items:center;gap:10px;font-size:14px;color:var(--muted);margin-top:12px}
.spin{width:16px;height:16px;border:2px solid var(--line);border-top-color:var(--confirma);border-radius:50%;animation:g 1s linear infinite}
@keyframes g{to{transform:rotate(360deg)}}
details{margin-top:18px;font-size:14px}
summary{cursor:pointer;color:var(--muted)}
.mini{font-size:12px;color:var(--muted);margin-top:18px}
[hidden]{display:none!important}
</style>
</head>
<body>
<main class="card">
  <span class="live"><i></i>Ao vivo · 2º turno</span>
  <h1>Apuração 2026 ao vivo</h1>
  <p>Acompanhe Lula x Flávio Bolsonaro em tempo real, atualizado a cada 5 segundos.</p>
  <ul>
    <li>Mapa de quem lidera em cada estado</li>
    <li>Soma de estados e regiões, com um toque</li>
    <li>Comparação estado a estado e gráfico da virada</li>
    <li>Quantos votos ainda faltam e quanto precisa para virar</li>
  </ul>
  <div class="preco">R$ {{PRECO}}<small>acesso por {{HORAS}} horas, via Pix</small></div>

  <section id="passo1">
    <label for="email">Seu e-mail (para o comprovante do Pix)</label>
    <input id="email" type="email" autocomplete="email" placeholder="voce@email.com">
    <button id="gerar">Gerar Pix</button>
    <div class="msg" id="msg1" role="alert"></div>
  </section>

  <section id="passo2" hidden>
    <p><b>Pague com o app do seu banco</b>: leia o QR Code ou use o Pix copia e cola.</p>
    <img class="qr" id="qr" alt="QR Code do Pix">
    <textarea id="copia" rows="3" readonly aria-label="Pix copia e cola"></textarea>
    <button class="sec" id="copiar">Copiar código Pix</button>
    <div class="esp"><span class="spin"></span>Aguardando o pagamento. O acesso libera sozinho.</div>
    <button class="sec" id="novo">Gerar outro Pix</button>
  </section>

  <details><summary>Tenho um código de acesso</summary>
    <input id="codigo" type="password" placeholder="Código" style="margin-top:8px" aria-label="Código de acesso">
    <button class="sec" id="entrar">Entrar</button>
    <div class="msg" id="msg2" role="alert"></div>
  </details>
  <p class="mini">Site independente, sem vínculo com o TSE ou com a Justiça Eleitoral. Os dados são os oficiais, publicados em resultados.tse.jus.br.</p>
</main>
<script>
const $=id=>document.getElementById(id);let timer=null;
const guardar=(k,v)=>{try{v==null?localStorage.removeItem(k):localStorage.setItem(k,v)}catch(e){}};
const ler=k=>{try{return localStorage.getItem(k)}catch(e){return null}};
function mostrarPix(p){$('passo1').hidden=true;$('passo2').hidden=false;
  if(p.img)$('qr').src='data:image/png;base64,'+p.img;else $('qr').hidden=true;
  $('copia').value=p.qr||'';esperar(p.id)}
function esperar(id){clearInterval(timer);timer=setInterval(async()=>{
  try{const r=await fetch('/status?id='+id,{cache:'no-store'});const j=await r.json();
    if(j.status==='approved'){clearInterval(timer);guardar('pix',null);location.reload()}
    else if(['rejected','cancelled','expired'].includes(j.status)){clearInterval(timer);guardar('pix',null);
      $('passo2').hidden=true;$('passo1').hidden=false;$('msg1').textContent='Esse Pix expirou. Gere outro para continuar.'}
  }catch(e){}},3000)}
$('gerar').onclick=async()=>{const b=$('gerar');$('msg1').textContent='';b.disabled=true;b.textContent='Gerando…';
  try{const r=await fetch('/pagar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:$('email').value})});
    const j=await r.json();if(!r.ok)throw new Error(j.erro||'Erro');guardar('pix',JSON.stringify(j));mostrarPix(j)}
  catch(e){$('msg1').textContent=e.message}finally{b.disabled=false;b.textContent='Gerar Pix'}};
$('copiar').onclick=async()=>{try{await navigator.clipboard.writeText($('copia').value)}catch(e){$('copia').select();document.execCommand('copy')}
  $('copiar').textContent='Copiado!';setTimeout(()=>$('copiar').textContent='Copiar código Pix',2000)};
$('novo').onclick=()=>{clearInterval(timer);guardar('pix',null);$('passo2').hidden=true;$('passo1').hidden=false};
$('entrar').onclick=async()=>{$('msg2').textContent='';
  const r=await fetch('/entrar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({codigo:$('codigo').value})});
  if(r.ok)location.reload();else $('msg2').textContent='Código inválido.'};
const pend=ler('pix');if(pend){try{mostrarPix(JSON.parse(pend))}catch(e){guardar('pix',null)}}   // voltou depois de pagar
</script>
</body>
</html>
"""


PAGE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Apuração 2026 · Presidente</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@75..100,400..800&family=Public+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<style>
:root{
  --bg:#E9ECE8; --panel:#F8F9F6; --ink:#161B22; --muted:#5B6470; --line:#C8CEC6;
  --land:#D3D8D0; --confirma:#1F8A4C; --corrige:#D9692A; --erro:#C0392B; --focus:#1D4ED8;
  --display:"Archivo","Arial Narrow",system-ui,sans-serif;
  --body:"Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#141920; --panel:#1C232C; --ink:#E8ECEF; --muted:#9AA4AF; --line:#323B46; --land:#2B343F;
}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body)}
body{padding:env(safe-area-inset-top,0) 0 env(safe-area-inset-bottom,0)}
.wrap{max-width:1280px;margin:0 auto;padding:18px clamp(14px,3vw,32px) 40px}
header{display:grid;gap:14px;margin-bottom:18px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px 20px;flex-wrap:wrap}
h1{font-family:var(--display);font-stretch:78%;font-weight:800;font-size:clamp(28px,4vw,40px);margin:0;letter-spacing:-.01em}

/* status ao vivo */
.status{display:flex;align-items:center;gap:10px 16px;flex-wrap:wrap;font-size:14px;color:var(--muted)}
.live{display:inline-flex;align-items:center;gap:8px;font-weight:600;color:var(--ink);padding:6px 12px;border-radius:99px;background:var(--panel);border:1px solid var(--line)}
.dot{width:10px;height:10px;border-radius:50%;background:var(--muted)}
.live.ok .dot{background:var(--confirma);animation:pulse 2s infinite}
.live.espera .dot{background:var(--corrige)}
.live.erro .dot{background:var(--erro)}
.live.pausa .dot{background:var(--muted)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(31,138,76,.55)}70%{box-shadow:0 0 0 9px rgba(31,138,76,0)}100%{box-shadow:0 0 0 0 rgba(31,138,76,0)}}
.status b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.linkbtn{font:500 14px var(--body);background:none;border:0;color:var(--focus);cursor:pointer;padding:4px;text-decoration:underline}

.turno{font-stretch:90%;font-weight:600;color:var(--muted);font-size:.6em;vertical-align:.2em}
.tabs,.seg,.regioes{display:flex;gap:6px;flex-wrap:wrap}
.tabs button,.seg button,.regioes button{font:600 14px var(--body);padding:9px 14px;border-radius:6px;border:1.5px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
.tabs button[aria-pressed=true],.seg button[aria-pressed=true]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.regioes button{font-weight:500;font-size:13px;padding:6px 11px}
.regioes .sep{flex-basis:100%;height:0}
.regioes button.acao{font-weight:600;border-color:var(--ink)}
.regioes button.on{border-color:var(--ink);box-shadow:inset 0 0 0 1px var(--ink)}
button:focus-visible,select:focus-visible,input:focus-visible,path:focus-visible{outline:3px solid var(--focus);outline-offset:2px}

main{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:22px;align-items:start}
@media (max-width:900px){main{grid-template-columns:1fr}}
.mapbox{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;position:sticky;top:12px;display:grid;gap:10px}
@media (max-width:900px){.mapbox{position:static}}
.dica{font-size:13px;color:var(--muted);margin:0}
#mapa{width:100%;height:auto;display:block}
#mapa path{stroke:var(--panel);stroke-width:1;cursor:pointer;transition:fill .4s,opacity .25s}
#mapa path.sel{stroke:var(--ink);stroke-width:2.5}
#mapa path.fora{opacity:.3}
#mapa text{font:600 11px var(--body);fill:var(--ink);pointer-events:none;paint-order:stroke;stroke:var(--panel);stroke-width:3px}
#legenda{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;color:var(--muted);min-height:20px}
#legenda i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;vertical-align:-1px}

.painel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px clamp(14px,2vw,22px)}
.escopo{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.escopo h2{font-family:var(--display);font-stretch:85%;font-size:24px;margin:0;font-weight:700}
.sub{color:var(--muted);font-size:14px;margin:4px 0 0}
.chip{font:500 13px var(--body);border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:99px;padding:4px 10px;cursor:pointer}
select,input[type=search]{font:15px var(--body);padding:9px 10px;border-radius:6px;border:1.5px solid var(--line);background:var(--bg);color:var(--ink);width:100%}
.ctrl{display:grid;gap:8px;margin:12px 0 0}
.ctrl:empty{display:none}

.prog{margin:16px 0 4px}
.prog .num{font-family:var(--display);font-stretch:75%;font-weight:800;font-size:46px;line-height:1;font-variant-numeric:tabular-nums}
.prog .lbl{color:var(--muted);font-size:14px;margin-left:6px}
.bar{height:8px;background:var(--line);border-radius:99px;overflow:hidden;margin-top:8px}
.bar>b{display:block;height:100%;background:var(--confirma);transition:width .6s}
.urnas{font-size:14px;color:var(--muted);margin:8px 0 0;font-variant-numeric:tabular-nums}
.urnas b{color:var(--ink)}
.extras{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin:14px 0 4px}
.extras div{background:var(--bg);border-radius:6px;padding:8px 10px}
.extras span{display:block;font-size:12px;color:var(--muted)}
.extras strong{font-family:var(--display);font-stretch:85%;font-size:18px;font-variant-numeric:tabular-nums}
.vantagem{font-size:14px;margin:12px 0 0;padding:9px 12px;border-radius:6px;background:var(--bg)}
.vantagem:empty{display:none}
#busca{margin:16px 0 4px}

ol{list-style:none;margin:10px 0 0;padding:0}
li.c{display:grid;grid-template-columns:6px 1fr auto;gap:4px 12px;padding:11px 0;border-top:1px solid var(--line)}
li.c .cor{grid-row:1/3;border-radius:3px}
li.c .nm{font-weight:600;font-size:15px}
li.c .pt{color:var(--muted);font-size:13px;font-weight:400}
li.c .pc{font-family:var(--display);font-stretch:80%;font-weight:700;font-size:22px;text-align:right;font-variant-numeric:tabular-nums}
li.c .vt{grid-column:2/4;display:flex;align-items:center;gap:10px;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
li.c .vt .bar{flex:1;margin:0;height:6px}
.tag{font:600 12px var(--body);color:#fff;background:var(--confirma);border-radius:4px;padding:2px 6px;margin-left:6px;vertical-align:1px}
.tag.t2{background:var(--corrige)}
.sit-ok{background:var(--confirma)}
.tag.sit-ap{background:transparent;color:var(--confirma);box-shadow:inset 0 0 0 1.5px var(--confirma)}
.tag.sit-br{background:var(--corrige)}
.tag.sit-su{background:transparent;color:var(--muted);box-shadow:inset 0 0 0 1.5px var(--line)}
.tag.sit-fo{background:transparent;color:var(--muted)}
.cmpSels{display:flex;align-items:center;gap:8px;margin:6px 0 2px}
.cmpSels select{flex:1;min-width:0}
.cmpSels span{color:var(--muted);font-size:14px}
.cmp{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:10px}
@media (max-width:560px){.cmp{grid-template-columns:1fr}}
.cmpHead{border-top:5px solid;padding-top:8px;font-size:15px;line-height:1.35}
.cmpHead strong{font-family:var(--display);font-stretch:80%;font-size:30px;display:block;line-height:1}
.cmpHead span{display:block;color:var(--muted);font-size:12px;margin-top:4px}
.cmpCol ol{margin-top:8px}
.cmpCol li{border-top:1px solid var(--line)}
.cmpRow{all:unset;box-sizing:border-box;width:100%;cursor:pointer;display:grid;grid-template-columns:auto 1fr;gap:2px 10px;padding:8px 0;font-size:14px}
.cmpRow .mg{text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
.cmpRow .det{grid-column:1/3;color:var(--muted);font-size:12px}
.cmpRow:hover .det{color:var(--focus)}
.cmpRow:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:4px}
.cmpCol li.vazio{color:var(--muted);font-size:13px;padding:8px 0}
.graf{margin:6px 0 4px;position:relative}
.grafTop{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px}
.grafTop h3{margin:16px auto 0 0}
.gTog button{font-size:12px;padding:5px 9px}
.grafTop .gTog{margin-top:14px}
#grafico{position:relative;margin-top:8px}
.grafSvg{width:100%;height:auto;display:block;touch-action:pan-y}
.grafSvg .eixo text{font:11px var(--body);fill:var(--muted)}
.grafSvg .eixo path{display:none}
.grafSvg .eixo line{stroke:var(--line)}
.grafSvg .meio{stroke:var(--muted);stroke-dasharray:3 3}
.grafSvg .rot{font:700 12px var(--body)}
.grafSvg .rotv{font:600 12px var(--body);fill:var(--ink)}
.grafSvg .guia{stroke:var(--muted);stroke-dasharray:2 2}
.tip{position:absolute;top:4px;transform:translateX(-50%);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12px;line-height:1.5;pointer-events:none;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.12)}
details.como{margin:14px 0 0;background:var(--bg);border-radius:6px;padding:10px 12px;font-size:14px;line-height:1.55}
details.como summary{cursor:pointer;font-weight:600}
ol.passos{list-style:decimal;padding-left:20px;margin:10px 0 0;display:grid;gap:8px}
.filtroSit{margin:8px 0 2px}
.filtroSit button{font-size:13px;padding:6px 11px}
.tag.vaga{background:transparent;color:var(--confirma);box-shadow:inset 0 0 0 1.5px var(--confirma)}
li.corte{border-top:2px dashed var(--corrige);color:var(--corrige);font-size:12px;font-weight:600;padding:6px 0 2px}
.regra{font-size:14px;line-height:1.5;margin:14px 0 0;padding:10px 12px;border-radius:6px;background:var(--bg)}
.painel h3{font-family:var(--display);font-stretch:85%;font-size:19px;margin:22px 0 2px}
.porUF li{border-top:1px solid var(--line);padding:10px 0;display:grid;gap:6px}
.ufh{all:unset;cursor:pointer;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:15px}
.ufh span{color:var(--muted);font-size:13px}
.ufh:hover span{color:var(--focus)}
.ufh:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:4px}
.ufc{display:grid;grid-template-columns:6px 1fr auto;gap:10px;align-items:center;font-size:14px}
.ufc .cor{height:18px;border-radius:3px}
.ufc .pt{color:var(--muted);font-size:13px}
.ufc .pcs{font-variant-numeric:tabular-nums;font-weight:600}
.ufc.fora1{color:var(--muted)}
.ufc.fora1 .cor{background:var(--line)}
.aviso{border-left:4px solid var(--corrige);padding:10px 14px;background:var(--bg);border-radius:4px;font-size:14px;line-height:1.5;margin:14px 0 0}
.mais{color:var(--muted);font-size:13px;padding-top:10px}
footer{color:var(--muted);font-size:12px;margin-top:20px;line-height:1.5}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="top">
    <h1>Apuração 2026 <span class="turno" id="turnoTag"></span></h1>
    <div class="status">
      <span class="live" id="live"><span class="dot"></span><span id="liveTxt">Conectando…</span></span>
      <span>Dados do TSE: <b id="horaTse">—</b></span>
      <span>Conferido <b id="conferido">—</b></span>
      <button class="linkbtn" id="pausar">Pausar</button>
    </div>
  </div>
  <div class="tabs" id="tabs" role="group" aria-label="Cargo" hidden></div>
</header>
<main>
  <section class="mapbox" aria-label="Mapa por estado">
    <div class="seg" role="group" aria-label="Modo do mapa">
      <button id="mUm" aria-pressed="true">Ver um estado</button>
      <button id="mSoma" aria-pressed="false">Somar estados</button>
      <button id="mCmp" aria-pressed="false">Comparar candidatos</button>
    </div>
    <div class="regioes" id="regioes" hidden></div>
    <p class="dica" id="dica">Toque num estado para ver o resultado dele.</p>
    <svg id="mapa" viewBox="0 0 600 600" role="img" aria-label="Mapa do Brasil por estado"></svg>
    <div id="legenda"></div>
  </section>
  <section class="painel">
    <div class="escopo"><h2 id="titulo">Brasil</h2><button class="chip" id="voltar" hidden>Voltar para o Brasil</button></div>
    <p class="sub" id="subtitulo"></p>
    <div class="ctrl" id="ctrl"></div>
    <div id="resumo" aria-live="polite"></div>
    <input type="search" id="busca" placeholder="Buscar candidato pelo nome, partido ou número" hidden>
    <div id="lista"></div>
  </section>
</main>
<footer>Dados oficiais do TSE (resultados.tse.jus.br). Projeto pessoal, sem vínculo com a Justiça Eleitoral. Na soma de estados, os votos do exterior só entram na visão "Brasil". Para rever o 1º turno, abra localhost:8026/?turno=1.</footer>
</div>
<script>
/* ================= configuração ================= */
const INTERVALO=5000;                 // painel e estados selecionados
const INTERVALO_MAPA_DEP=30000;       // mapa nas abas de deputado (arquivos grandes)
const Q=new URLSearchParams(location.search), T2=Q.get('turno')!=='1';   // padrão: 2º turno (25/10)
const EL={fed:T2?'6258':'6257',est:T2?'6260':'6259'};
const pad=e=>e.padStart(6,'0');
const CARGOS=[
 {id:'pres',nome:'Presidente',cd:'0001',el:'fed',mapa:true}];
const UFS={ac:'Acre',al:'Alagoas',ap:'Amapá',am:'Amazonas',ba:'Bahia',ce:'Ceará',df:'Distrito Federal',es:'Espírito Santo',go:'Goiás',ma:'Maranhão',mt:'Mato Grosso',ms:'Mato Grosso do Sul',mg:'Minas Gerais',pa:'Pará',pb:'Paraíba',pr:'Paraná',pe:'Pernambuco',pi:'Piauí',rj:'Rio de Janeiro',rn:'Rio Grande do Norte',rs:'Rio Grande do Sul',ro:'Rondônia',rr:'Roraima',sc:'Santa Catarina',sp:'São Paulo',se:'Sergipe',to:'Tocantins'};
const REGIOES={'Norte':['ac','ap','am','pa','ro','rr','to'],'Nordeste':['al','ba','ce','ma','pb','pe','pi','rn','se'],'Centro-Oeste':['df','go','mt','ms'],'Sudeste':['es','mg','rj','sp'],'Sul':['pr','rs','sc']};
const norm=s=>String(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase().trim();
const NAME2UF=Object.fromEntries(Object.entries(UFS).map(([k,v])=>[norm(v),k]));
const PCOR={13:'#D62839',22:'#1D4ED8',15:'#15924A',45:'#0EA5E9',55:'#E39B0B',44:'#7C3AED',12:'#EA580C',40:'#DB2777',50:'#C9A800',30:'#F97316',10:'#3B5BDB',11:'#0E8FA8',20:'#65A30D',65:'#BE123C',43:'#22C55E',77:'#A16207',18:'#14B8A6',16:'#B91C1C',80:'#9F1239',35:'#64748B',25:'#0F766E',36:'#8B5CF6',70:'#4D7C0F',33:'#B45309',28:'#6D28D9',27:'#0369A1',29:'#BE185D',21:'#991B1B',19:'#C2410C'};
const corDe=n=>{const p=String(n).slice(0,2);if(PCOR[p])return PCOR[p];let h=0;for(const ch of p)h=h*31+ch.charCodeAt(0);return `hsl(${h%360} 55% 45%)`};
const PARTIDOS={10:'REPUBLICANOS',11:'PP',12:'PDT',13:'PT',15:'MDB',16:'PSTU',18:'REDE',20:'PODE',21:'PCB',22:'PL',23:'CIDADANIA',25:'PRD',27:'DC',28:'PRTB',29:'PCO',30:'NOVO',33:'MOBILIZA',35:'DEMOCRATA',36:'AGIR',40:'PSB',43:'PV',44:'UNIÃO',45:'PSDB',50:'PSOL',55:'PSD',65:'PCdoB',70:'AVANTE',77:'SOLIDARIEDADE',80:'UP'};
const partidoDe=c=>c.partido||PARTIDOS[String(c.n).slice(0,2)]||('Partido '+String(c.n).slice(0,2));
function isEleito(x){const st=norm(x.st);return x.eleito||st==='eleito'||st.startsWith('eleito por')||st==='eleito por qp'||st==='eleito por media'}
const fmt=new Intl.NumberFormat('pt-BR');
const fpc=v=>v==null||isNaN(v)?'—':v.toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2});
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const sel={cargo:'pres',modo:'um',uf:'br',mun:'',soma:new Set()};
const cache={};            // cache[cargo][chave] = dados
let muns={},filtro='',pausado=false,geracao=0,ultimoOk=null,statusRede='conectando';
const cargo=()=>CARGOS.find(c=>c.id===sel.cargo);
const cdFor=(c,uf)=>(c.id==='depe'&&uf==='df')?'0008':c.cd;
const store=()=>cache[sel.cargo]||(cache[sel.cargo]={});

/* ================= leitura dos arquivos do TSE ================= */
function num(v){if(v==null||v===''||typeof v==='object')return null;if(typeof v==='number')return v;const s=String(v);const n=s.includes(',')?parseFloat(s.replace(/\./g,'').replace(',','.')):parseFloat(s);return isNaN(n)?null:n}
function parse(j){
  const cands=[],seen=new Set();const r={prog:null,st:null,s:null,hora:null,data:null,el:null,comp:null,abst:null,vb:null,vn:null,pComp:null,pAbst:null,pBr:null,pNu:null};
  const G={};const pk=(o,ks)=>{for(const k of ks){const v=num(o[k]);if(v!=null)return v}return null};
  const reg=id=>G[id]||(G[id]={id,nome:null,sgs:[],total:null,legAgr:null,legPar:0});
  (function rec(o,party,agr){
    if(Array.isArray(o)){o.forEach(x=>rec(x,party,agr));return}
    if(!o||typeof o!=='object')return;
    const isCand='vap' in o&&(o.nm||o.nmu);
    if(!isCand){
      if(r.prog==null&&o.pst!=null){r.prog=num(o.pst);r.st=num(o.st);r.s=num(o.ts)??num(o.s)}
      if(!r.hora&&(o.ht||o.hg)){r.hora=o.ht||o.hg;r.data=o.dt||o.dg}
      if(r.pBr==null&&o.pvb!=null){r.pBr=num(o.pvb);r.vb=num(o.vb);r.pNu=num(o.ptvn)??num(o.pvn);r.vn=num(o.tvn)??num(o.vn)}
      if(r.pAbst==null&&o.pa!=null){r.pAbst=num(o.pa);r.abst=num(o.a);r.pComp=num(o.pc);r.comp=num(o.c);r.el=num(o.e)}
    }
    let p=party,ag=agr;if(typeof o.sg==='string'&&!isCand)p=o.sg;
    if(!isCand&&Array.isArray(o.par)){                       // federação ou partido isolado
      ag='A:'+(o.n??o.nm??o.sg);const g=reg(ag);g.nome=g.nome||o.nm||o.sg||null;
      const t=pk(o,['tvag','tva','vag','tvap','vap','tvtn']);if(t!=null)g.total=t;
      const l=pk(o,['tvl','vl','tval','vlg','tvlg']);if(l!=null)g.legAgr=l;
    }else if(!isCand&&Array.isArray(o.cand)){                // partido
      if(!ag)ag='P:'+(o.sg||o.n);const g=reg(ag);
      if(typeof o.sg==='string'&&!g.sgs.includes(o.sg))g.sgs.push(o.sg);
      const l=pk(o,['vl','tvl','tval','vlg','tvlg']);if(l!=null)g.legPar+=l;
    }
    if(isCand){
      const k=(o.n||'')+'|'+(o.nmu||o.nm);
      if(!seen.has(k)){seen.add(k);cands.push({n:String(o.n??''),nome:o.nmu||o.nm,partido:o.sgp||p||'',votos:num(o.vap)||0,pct:num(o.pvap),st:o.st||'',eleito:o.e==='s'||o.e===true,
        agr:ag||(o.cc?'C:'+o.cc:null),cc:o.cc||null,valido:!o.dvt||norm(o.dvt).startsWith('valido')})}
      return}
    for(const k in o)rec(o[k],p,ag);
  })(j,'',null);
  r.grupos=G;
  const tot=cands.reduce((a,c)=>a+c.votos,0);
  cands.forEach(c=>{if(c.pct==null)c.pct=tot?c.votos/tot*100:0});
  cands.sort((a,b)=>b.votos-a.votos);
  if(r.prog==null&&r.st!=null&&r.s)r.prog=r.st/r.s*100;
  r.cands=cands;return r;
}
async function getResult(c,uf,mun=''){
  const e=EL[c.el],cd=cdFor(c,uf),base=`/tse/oficial/ele2026/${e}`;
  const urls=[`${base}/dados/${uf}/${uf}${mun}-c${cd}-e${pad(e)}-u.json`,`${base}/dados-simplificados/${uf}/${uf}${mun}-c${cd}-e${pad(e)}-r.json`];
  let last=0;
  for(const u of urls){
    let r;try{r=await fetch(u,{cache:'no-store'})}catch(err){const x=new Error('rede');x.rede=true;throw x}
    if(r.status===401){location.reload();throw new Error('acesso')}
    if(r.ok){try{const p=parse(await r.json());if(p.cands.length||p.prog!=null)return p}catch(e){}}
    last=r.status;
  }
  const x=new Error(String(last));x.rede=last>=500;throw x;
}

/* ================= soma de estados ================= */
function somar(lista,porPartido){
  const m=new Map(),r={st:0,s:0,el:0,comp:0,abst:0,vb:0,vn:0,hora:null,data:null,temUrnas:false,temComp:false,temBr:false};
  for(const d of lista){
    if(d.st!=null&&d.s!=null){r.st+=d.st;r.s+=d.s;r.temUrnas=true}
    if(d.comp!=null&&d.el!=null){r.comp+=d.comp;r.el+=d.el;r.abst+=d.abst||0;r.temComp=true}
    if(d.vb!=null&&d.vn!=null){r.vb+=d.vb;r.vn+=d.vn;r.temBr=true}
    if(d.hora&&(!r.hora||(d.data||'')+d.hora>(r.data||'')+r.hora)){r.hora=d.hora;r.data=d.data}
    for(const c of d.cands){
      const key=porPartido?partidoDe(c):c.n;
      const a=m.get(key)||{n:porPartido?c.n.slice(0,2):c.n,nome:porPartido?partidoDe(c):c.nome,partido:porPartido?'':c.partido,votos:0,st:'',eleito:false,ufs:0};
      a.votos+=c.votos;a.ufs++;if(d.cands[0]===c&&c.votos)a.primeiro=(a.primeiro||0)+1;m.set(key,a);
    }
  }
  const cands=[...m.values()],tot=cands.reduce((a,c)=>a+c.votos,0);
  cands.forEach(c=>c.pct=tot?c.votos/tot*100:0);cands.sort((a,b)=>b.votos-a.votos);
  const prog=r.temUrnas&&r.s?r.st/r.s*100:(lista.length?lista.reduce((a,d)=>a+(d.prog||0),0)/lista.length:null);
  return {cands,prog,st:r.temUrnas?r.st:null,s:r.temUrnas?r.s:null,hora:r.hora,data:r.data,
    el:r.temComp?r.el:null,comp:r.temComp?r.comp:null,abst:r.temComp?r.abst:null,
    pComp:r.temComp&&r.el?r.comp/r.el*100:null,pAbst:r.temComp&&r.el?r.abst/r.el*100:null,
    vb:r.temBr?r.vb:null,vn:r.temBr?r.vn:null,pBr:r.temBr&&r.comp?r.vb/r.comp*100:null,pNu:r.temBr&&r.comp?r.vn/r.comp*100:null,soma:true,porPartido};
}

/* ================= votos que faltam (estimativa) ================= */
// Supõe que as urnas que faltam têm, em média, o mesmo tamanho das que já foram apuradas.
function faltam(d){if(!d||d.prog==null||d.prog<1||d.prog>=100)return null;
  const cont=d.cands.reduce((s,x)=>s+x.votos,0);return cont?Math.round(cont*(100/d.prog-1)):null}
// fatia que o perdedor precisaria levar, na disputa entre os dois, para empatar
const virada=(gap,R)=>R?(R+gap)/(2*R)*100:null;
function textoVirada(lider,seg,R){
  if(!R||!lider||!seg)return '';
  const v=virada(lider.votos-seg.votos,R);
  if(v>100)return ` Faltam cerca de <b>${fmt.format(R)}</b> votos: nem levando todos eles ${esc(seg.nome)} alcançaria.`;
  return ` Faltam cerca de <b>${fmt.format(R)}</b> votos. Para virar, ${esc(seg.nome)} precisaria vencer ${esc(lider.nome)} por uns <b>${Math.ceil(v)}% a ${100-Math.ceil(v)}%</b> no que falta.`;
}

/* ================= gráfico da evolução (Presidente, Brasil) ================= */
let hist={nomes:{},pontos:[]},gY='votos',gX='pst';
async function carregarHist(){try{const r=await fetch(`/historico?e=${EL.fed}`,{cache:'no-store'});if(r.ok)hist=await r.json()}catch(e){}}
function duplaGrafico(dBR){
  const top=(dBR?dBR.cands:[]).filter(x=>x.votos).slice(0,6);
  if(top.length<2)return null;
  const A=top.some(x=>x.n===cmpA)?cmpA:top[0].n;
  const B=top.some(x=>x.n===cmpB)&&cmpB!==A?cmpB:top.find(x=>x.n!==A).n;
  return [A,B];
}
function htmlGrafico(){
  return `<div class="graf">
    <div class="grafTop"><h3>Evolução da apuração</h3>
      <div class="seg gTog" role="group" aria-label="Mostrar">
        <button data-y="votos" aria-pressed="${gY==='votos'}">Votos</button><button data-y="pct" aria-pressed="${gY==='pct'}">%</button></div>
      <div class="seg gTog" role="group" aria-label="Eixo horizontal">
        <button data-x="pst" aria-pressed="${gX==='pst'}">Por % apurado</button><button data-x="hora" aria-pressed="${gX==='hora'}">Por horário</button></div>
    </div>
    <div id="grafico"></div>
    <p class="sub" id="grafNota"></p></div>`;
}
function desenharGrafico(dBR){
  const box=document.getElementById('grafico');if(!box)return;
  const dupla=duplaGrafico(dBR),nota=document.getElementById('grafNota');
  const P=(hist.pontos||[]).filter(p=>p.c);
  if(!dupla||P.length<2){box.innerHTML='';
    nota.textContent='O gráfico aparece a partir da 2ª atualização do TSE gravada. Deixe o site aberto (ou só o Terminal rodando) e ele vai se completando.';return}
  const [A,B]=dupla,nome=n=>hist.nomes[n]||n;
  const tempo=p=>{const [dd,mm,yy]=(p.dt||'').split('/');const t=p.ht||p.local;return dd?new Date(`${yy}-${mm}-${dd}T${t}`):new Date(`1970-01-01T${t}`)};
  const val=(p,n)=>{const v=p.c[n]||0;if(gY==='votos')return v;const t=Object.values(p.c).reduce((s,x)=>s+x,0);return t?v/t*100:0};
  const xv=p=>gX==='pst'?(p.pst??0):tempo(p);
  const W=600,H=280,m={t:16,r:96,b:30,l:gY==='votos'?62:40};
  const x=(gX==='pst'?d3.scaleLinear():d3.scaleTime()).domain(d3.extent(P,xv)).range([m.l,W-m.r]);
  const ys=P.flatMap(p=>[val(p,A),val(p,B)]);
  let lo=d3.min(ys),hi=d3.max(ys);if(gY==='pct'){const pad=Math.max(2,(hi-lo)*.15);lo=Math.max(0,lo-pad);hi=Math.min(100,hi+pad)}else lo=0;
  const y=d3.scaleLinear().domain([lo,hi||1]).nice().range([H-m.b,m.t]);
  const fy=v=>gY==='votos'?(v>=1e6?fpc(v/1e6).replace(/,00$/,'')+' mi':fmt.format(Math.round(v))):fpc(v)+'%';
  box.innerHTML='';
  const svg=d3.select(box).append('svg').attr('viewBox',`0 0 ${W} ${H}`).attr('class','grafSvg').attr('role','img')
    .attr('aria-label',`Evolução de ${nome(A)} e ${nome(B)}`);
  svg.append('g').attr('class','eixo').attr('transform',`translate(0,${H-m.b})`)
    .call(d3.axisBottom(x).ticks(6).tickFormat(gX==='pst'?(d=>d+'%'):d3.timeFormat('%H:%M')));
  svg.append('g').attr('class','eixo').attr('transform',`translate(${m.l},0)`)
    .call(d3.axisLeft(y).ticks(5).tickFormat(fy).tickSize(-(W-m.l-m.r)));
  if(gY==='pct'&&lo<50&&hi>50)svg.append('line').attr('class','meio').attr('x1',m.l).attr('x2',W-m.r).attr('y1',y(50)).attr('y2',y(50));
  const lastY=[];
  [A,B].forEach(n=>{
    svg.append('path').datum(P).attr('fill','none').attr('stroke',corDe(n)).attr('stroke-width',2.5)
      .attr('d',d3.line().x(p=>x(xv(p))).y(p=>y(val(p,n))));
    const u=P[P.length-1];let ly=y(val(u,n));
    if(lastY.length&&Math.abs(ly-lastY[0])<30)ly=lastY[0]+(ly>=lastY[0]?30:-30);lastY.push(ly);
    const g=svg.append('g').attr('transform',`translate(${W-m.r+8},${ly})`);
    g.append('text').attr('class','rot').attr('fill',corDe(n)).attr('y',-3).text(nome(n).split(' ')[0]);
    g.append('text').attr('class','rotv').attr('y',12).text(fy(val(u,n)));
  });
  // marcador ao passar o mouse/dedo
  const guia=svg.append('g').style('display','none');
  guia.append('line').attr('class','guia').attr('y1',m.t).attr('y2',H-m.b);
  const pts=[A,B].map(n=>guia.append('circle').attr('r',4).attr('fill',corDe(n)));
  const tip=d3.select(box).append('div').attr('class','tip').style('display','none');
  svg.append('rect').attr('x',m.l).attr('y',m.t).attr('width',W-m.l-m.r).attr('height',H-m.t-m.b).attr('fill','transparent')
    .on('pointermove',ev=>{
      const [mx]=d3.pointer(ev);const xi=x.invert(mx);
      const i=d3.leastIndex(P,p=>Math.abs(+xv(p)-+xi));const p=P[i];
      guia.style('display',null).select('line').attr('x1',x(xv(p))).attr('x2',x(xv(p)));
      [A,B].forEach((n,k)=>pts[k].attr('cx',x(xv(p))).attr('cy',y(val(p,n))));
      const dif=(p.c[A]||0)-(p.c[B]||0);
      tip.style('display',null).style('left',(x(xv(p))/W*100)+'%')
        .html(`<b>${p.pst!=null?fpc(p.pst)+'% apurado':''}</b>${p.ht?' · '+p.ht:''}<br>
          ${[A,B].map(n=>`<span style="color:${corDe(n)}">●</span> ${esc(nome(n))}: ${fmt.format(p.c[n]||0)}`).join('<br>')}
          <br>Diferença: ${fmt.format(Math.abs(dif))} para ${esc(nome(dif>=0?A:B))}`);
    })
    .on('pointerleave',()=>{guia.style('display','none');tip.style('display','none')});
  const pri=P[0];
  nota.textContent=`${P.length} atualizações gravadas, desde ${pri.pst!=null?fpc(pri.pst)+'% das urnas':''}${pri.ht?' ('+pri.ht+')':''}. Passe o mouse ou o dedo no gráfico para ver cada momento.`;
}
document.getElementById('lista').addEventListener('click',e=>{const b=e.target.closest('.gTog button');if(!b)return;
  if(b.dataset.y)gY=b.dataset.y;if(b.dataset.x)gX=b.dataset.x;renderPainel()});

/* ================= comparação entre dois candidatos (Presidente) ================= */
let cmpA=null,cmpB=null,cmpAtual=[];
function renderComparacao(dBR){
  const D=store(),ufs=Object.keys(UFS).filter(u=>D[u]&&D[u].cands.length&&D[u].cands[0].votos);
  const top=dBR.cands.filter(x=>x.votos).slice(0,6);
  if(!ufs.length||top.length<2)return '';
  const A=top.some(x=>x.n===cmpA)?cmpA:top[0].n;
  const B=top.some(x=>x.n===cmpB)&&cmpB!==A?cmpB:top.find(x=>x.n!==A).n;
  const nomeDe=n=>top.find(x=>x.n===n).nome;cmpAtual=[A,B];
  const rows=ufs.map(u=>{const d=D[u],a=d.cands.find(x=>x.n===A),b=d.cands.find(x=>x.n===B);
    const pos=n=>d.cands.findIndex(x=>x.n===n)+1;
    return {u,d,a,b,lider:d.cands[0],posA:pos(A),posB:pos(B),margem:(a?.pct||0)-(b?.pct||0),R:faltam(d)}});
  const gA=rows.filter(r=>r.lider.n===A).sort((x,y)=>y.margem-x.margem);
  const gB=rows.filter(r=>r.lider.n===B).sort((x,y)=>x.margem-y.margem);
  const gO=rows.filter(r=>r.lider.n!==A&&r.lider.n!==B);
  const soma=(g,k)=>g.reduce((s,r)=>s+(r[k]?.votos||0),0);
  const opts=sel=>top.map(x=>`<option value="${esc(x.n)}"${x.n===sel?' selected':''}>${esc(x.nome)}</option>`).join('');
  const linha=(r,quem)=>{
    const lid=quem==='A'?r.a:r.b,seg=quem==='A'?r.b:r.a,outroPos=quem==='A'?r.posB:r.posA;
    const pts=Math.abs(r.margem);let chance='';
    if(r.R&&lid&&seg){const v=virada(lid.votos-seg.votos,r.R);chance=v>100?'não dá mais para virar':`para virar, ${esc(seg.nome)} precisa de ${Math.ceil(v)}% do que falta`}
    else if(r.d.prog>=100)chance='apuração encerrada';
    return `<li><button class="cmpRow" data-uf="${r.u}"><b>${r.u.toUpperCase()}</b>
      <span class="mg">+${fpc(pts)} pts</span>
      <span class="det">${fpc(r.d.prog)}% apurado · ${esc(nomeDe(quem==='A'?B:A))} em ${outroPos||'—'}º${chance?' · '+chance:''}</span></button></li>`};
  const col=(g,quem,n)=>{const outro=quem==='A'?B:A;const em2=g.filter(r=>(quem==='A'?r.posB:r.posA)===2).length;
    return `<div class="cmpCol"><div class="cmpHead" style="border-color:${corDe(n)}">
      <strong>${g.length}</strong> estado${g.length===1?'':'s'} com ${esc(nomeDe(n))} em 1º
      <span>${fmt.format(soma(g,quem==='A'?'a':'b'))} votos dele nesses estados · ${esc(nomeDe(outro))} em 2º em ${em2}</span></div>
      <ol>${g.map(r=>linha(r,quem)).join('')||'<li class="vazio">Nenhum ainda</li>'}</ol></div>`};
  return `<h3>Estado a estado</h3>
    <div class="cmpSels"><select class="cmpSel" data-q="A" aria-label="Primeiro candidato">${opts(A)}</select>
      <span>contra</span><select class="cmpSel" data-q="B" aria-label="Segundo candidato">${opts(B)}</select></div>
    <p class="sub">Ordenado da maior para a menor vantagem. Toque num estado para abrir o detalhe.</p>
    <div class="cmp">${col(gA,'A',A)}${col(gB,'B',B)}</div>
    ${gO.length?`<p class="sub">Outros candidatos lideram em: ${gO.map(r=>`${r.u.toUpperCase()} (${esc(r.lider.nome)})`).join(', ')}.</p>`:''}`;
}
document.getElementById('lista').addEventListener('change',e=>{const s=e.target.closest('.cmpSel');if(!s)return;
  if(s.dataset.q==='A')cmpA=s.value;else cmpB=s.value;s.blur();pintarMapa();renderPainel()});
document.getElementById('lista').addEventListener('click',e=>{const b=e.target.closest('.cmpRow');if(!b)return;sel.modo='um';sel.uf=b.dataset.uf;sel.mun='';tudo();window.scrollTo({top:0,behavior:'smooth'})});

/* ================= deputados: sistema proporcional ================= */
// Vagas de 2026 = as mesmas de 2022 (decisão do STF). Estaduais: 3x os federais até 12; acima disso, 36 + (federais - 12). DF: 24 distritais.
const VF={sp:70,mg:53,rj:46,ba:39,rs:31,pr:30,pe:25,ce:22,ma:18,go:17,pa:17,sc:16,pb:12,es:10,pi:10,al:9,ac:8,ap:8,am:8,df:8,mt:8,ms:8,rn:8,ro:8,rr:8,se:8,to:8};
const vagasDep=(c,uf)=>c.id==='depf'?VF[uf]:(uf==='df'?24:(VF[uf]<=12?3*VF[uf]:36+VF[uf]-12));
function nomeGrupo(d,id,ex){
  const g=d.grupos&&d.grupos[id];
  if(g){if(g.sgs.length>1)return g.sgs.join('/');if(g.sgs.length===1&&!(g.nome&&/federa/i.test(g.nome)))return g.sgs[0];if(g.nome)return g.nome}
  if(ex&&ex.cc){const m=String(ex.cc).match(/\(([^)]+)\)\s*$/);return m?m[1]:ex.cc}
  return ex?partidoDe(ex):id;
}
function projetar(d,vagas){
  const G=new Map();
  for(const x of d.cands){if(!x.valido)continue;const id=x.agr||('P:'+partidoDe(x));
    const a=G.get(id)||{id,nome:nomeGrupo(d,id,x),n:String(x.n).slice(0,2),nominal:0,cands:[],seats:0,qp:0,ult:null};
    a.nominal+=x.votos;a.cands.push(x);G.set(id,a)}
  const A=[...G.values()];
  for(const a of A){const g=d.grupos&&d.grupos[a.id];const leg=g?(g.legAgr??g.legPar??0):0;
    a.legenda=leg;a.votos=Math.max(g&&g.total||0,a.nominal+leg);a.cands.sort((x,y)=>y.votos-x.votos)}
  const V=A.reduce((s,a)=>s+a.votos,0);if(!V||!vagas)return null;
  const q=V/vagas;let QE=(q-Math.floor(q))>.5?Math.ceil(q):Math.floor(q);QE=Math.max(1,QE);
  const media=a=>a.votos/(a.seats+1);let livres=vagas;const sobras=[];
  if(!A.some(a=>a.votos>=QE)){                       // ninguém atingiu o quociente: entram os mais votados
    A.flatMap(a=>a.cands.map(x=>({a,x}))).sort((p,q)=>q.x.votos-p.x.votos).slice(0,vagas).forEach(({a})=>a.seats++);livres=0;
  }else{
    for(const a of A){a.qp=Math.min(Math.floor(a.votos/QE),a.cands.filter(x=>x.votos>=.1*QE).length);a.seats=a.qp;livres-=a.qp}
    const melhor=el=>el.reduce((m,a)=>media(a)>media(m)?a:m);
    while(livres>0){const el=A.filter(a=>a.votos>=.8*QE&&a.cands[a.seats]&&a.cands[a.seats].votos>=.2*QE);if(!el.length)break;
      const w=melhor(el),m=media(w);sobras.push({a:w,m,fase:2});w.ult=m;w.seats++;livres--}
    while(livres>0){const el=A.filter(a=>a.cands[a.seats]);if(!el.length)break;
      const w=melhor(el),m=media(w);sobras.push({a:w,m,fase:3});w.ult=m;w.seats++;livres--}
  }
  const corte=sobras.length?Math.min(...sobras.map(s=>s.m)):QE;
  A.forEach(a=>a.prox=a.cands[a.seats]?media(a):0);
  const vice=Math.max(0,...A.map(a=>a.prox));        // quem pegaria a próxima vaga se houvesse uma a mais
  // situação de cada candidato
  const sit=new Map();
  for(const a of A){a.perto=a.prox&&a.prox>=.9*corte;
    a.cands.forEach((x,i)=>{let s,motivo='';
      if(i<a.seats){s='dentro';
        if(i===a.seats-1){
          const prox=a.cands[i+1];
          if(prox&&prox.votos>=.9*x.votos){s='apertado';motivo=`${prox.nome}, do mesmo partido, está colado`}
          else if(a.ult!=null&&a.ult<vice*1.1){s='apertado';motivo='a última vaga do partido está ameaçada por outro partido'}
        }
      }else if(i===a.seats){
        const ult=a.cands[i-1];
        if(a.perto){s='briga';motivo='o partido está perto de ganhar mais uma vaga'}
        else if(ult&&x.votos>=.9*ult.votos){s='briga';motivo=`perto de passar ${ult.nome}, do mesmo partido`}
        else{s='suplente';motivo='1º suplente do partido'}
      }else{s='fora';motivo=`${i-a.seats+1}º suplente do partido`}
      sit.set(x,{s,motivo,grupo:a})})}
  A.sort((x,y)=>y.seats-x.seats||y.votos-x.votos);
  return {A,QE,V,vagas,corte,sit,sobras,semQE:!A.some(a=>a.qp>0)};
}
const SIT={dentro:['Dentro','sit-ok'],apertado:['Dentro, apertado','sit-ap'],briga:['Na briga','sit-br'],suplente:['1º suplente','sit-su'],fora:['Fora','sit-fo']};
let fSit='todos';
function tagTSE(x){const st=norm(x.st);if(isEleito(x))return `<span class="tag">${esc(x.st||'Eleito')}</span>`;if(st.startsWith('suplente'))return '<span class="tag sit-su">Suplente</span>';if(st==='nao eleito')return '<span class="tag sit-fo">Não eleito</span>';return ''}
function renderDeputados(d,c){
  const uf=sel.uf,vagas=vagasDep(c,uf);
  if(sel.mun)return `<p class="aviso">A projeção de cadeiras vale para o estado inteiro. Escolha "Estado inteiro" no seletor acima para ver quem tem chance de entrar.</p>`+listaSimples(d,c);
  const P=projetar(d,vagas);if(!P)return '<p class="aviso">Ainda não há votos suficientes para projetar as cadeiras.</p>';
  const final=(d.prog||0)>=99.99&&d.cands.some(isEleito);   // TSE já marcou eleitos
  const maxS=P.A[0]?.seats||1,f=norm(filtro);
  const lista=d.cands.filter(x=>{const st=P.sit.get(x);
    if(fSit!=='todos'&&(!st||(fSit==='dentro'?!['dentro','apertado'].includes(st.s):st.s!=='briga')))return false;
    return !f||norm(x.nome).includes(f)||norm(partidoDe(x)).includes(f)||x.n.startsWith(filtro.trim())});
  const lim=fSit==='todos'?60:120;
  const contagem=s=>[...P.sit.values()].filter(v=>s.includes(v.s)).length;
  return `
  ${d.prog!=null&&d.prog<60?`<p class="aviso">Projeção com ${fpc(d.prog)}% das urnas apuradas. Ainda pode mudar bastante.</p>`:''}
  <div class="extras">
    <div><span>Cadeiras em jogo</span><strong>${vagas}</strong></div>
    <div><span>Quociente eleitoral</span><strong>${fmt.format(P.QE)}</strong></div>
    <div><span>Partido precisa (80%)</span><strong>${fmt.format(Math.ceil(.8*P.QE))}</strong></div>
    <div><span>Candidato precisa (20%)</span><strong>${fmt.format(Math.ceil(.2*P.QE))}</strong></div>
  </div>
  <details class="como"><summary>Como funciona a conta</summary>
    <ol class="passos">
      <li><b>Quociente eleitoral:</b> soma todos os votos válidos (em candidatos e na legenda) e divide pelas ${vagas} cadeiras. Hoje dá ${fmt.format(P.QE)} votos.</li>
      <li><b>Cadeiras por partido:</b> cada partido ou federação ganha uma cadeira a cada ${fmt.format(P.QE)} votos que somar. Os votos de todos os candidatos dele contam juntos.</li>
      <li><b>Sobras:</b> as cadeiras que restam vão, uma a uma, para o partido com a maior média (votos ÷ cadeiras já ganhas + 1). Só entram partidos com 80% do quociente e candidatos com 20% dele; se ainda sobrar cadeira, todos participam.</li>
      <li><b>Quem senta:</b> dentro de cada partido, as cadeiras vão para os candidatos mais votados dele. Por isso alguém com muito voto pode ficar de fora, e alguém com menos pode entrar puxado pelo partido.</li>
    </ol></details>
  <h3>Cadeiras por partido ou federação</h3>
  <p class="sub">Projeção com os votos apurados até agora.</p>
  <ol>${P.A.filter(a=>a.seats||a.votos>=.5*P.QE).map(a=>`<li class="c"><span class="cor" style="background:${corDe(a.n)}"></span>
    <span class="nm">${esc(a.nome)}<br><span class="pt">${fmt.format(a.votos)} votos${a.sobras?'':''}${a.seats?` · ${a.qp} pelo quociente${a.seats-a.qp?`, ${a.seats-a.qp} nas sobras`:''}`:''}${a.perto?' · perto de mais uma':''}</span></span>
    <span class="pc">${a.seats}</span>
    <span class="vt"><span class="bar"><b style="width:${Math.max(.5,a.seats/maxS*100)}%;background:${corDe(a.n)}"></b></span>${a.seats} cadeira${a.seats===1?'':'s'}</span></li>`).join('')}</ol>
  <h3>Candidatos: quem tem chance de entrar</h3>
  <div class="seg filtroSit" role="group" aria-label="Filtrar candidatos">
    <button data-f="todos" aria-pressed="${fSit==='todos'}">Todos</button>
    <button data-f="dentro" aria-pressed="${fSit==='dentro'}">Dentro (${contagem(['dentro','apertado'])})</button>
    <button data-f="briga" aria-pressed="${fSit==='briga'}">Na briga (${contagem(['briga'])})</button>
  </div>
  <ol>${lista.slice(0,lim).map(x=>{const st=P.sit.get(x),[lbl,cls]=st?SIT[st.s]:['Voto não válido','sit-fo'];
    return `<li class="c"><span class="cor" style="background:${corDe(x.n)}"></span>
    <span class="nm">${esc(x.nome)}${final?tagTSE(x):`<span class="tag ${cls}">${lbl}</span>`}<br><span class="pt">${esc(partidoDe(x))} · ${esc(x.n)}${st&&st.grupo.nome!==partidoDe(x)?` · ${esc(st.grupo.nome)}`:''}${st&&st.motivo&&!final?` · ${esc(st.motivo)}`:''}</span></span>
    <span class="pc">${fmt.format(x.votos)}</span>
    <span class="vt">${fpc(x.pct)}% dos votos</span></li>`}).join('')}</ol>
  ${lista.length>lim?`<p class="mais">Mostrando ${lim} de ${lista.length}. Use a busca para achar um candidato.</p>`:''}
  ${!lista.length?'<p class="mais">Nenhum candidato com esse filtro.</p>':''}`;
}
function listaSimples(d,c){
  const f=norm(filtro),l=d.cands.filter(x=>!f||norm(x.nome).includes(f)||x.n.startsWith(filtro.trim()));
  return `<ol>${l.slice(0,60).map(x=>`<li class="c"><span class="cor" style="background:${corDe(x.n)}"></span><span class="nm">${esc(x.nome)}<br><span class="pt">${esc(partidoDe(x))} · ${esc(x.n)}</span></span><span class="pc">${fmt.format(x.votos)}</span><span class="vt">${fpc(x.pct)}% dos votos neste município</span></li>`).join('')}</ol>`;
}
function renderBancadaDep(c){
  const tot=new Map();let estados=0,cad=0;
  for(const uf of sel.soma){const d=store()[uf];if(!d)continue;const P=projetar(d,vagasDep(c,uf));if(!P)continue;estados++;
    for(const a of P.A){if(!a.seats)continue;cad+=a.seats;
      const k=a.nome,t=tot.get(k)||{nome:k,n:a.n,seats:0,ufs:[]};t.seats+=a.seats;t.ufs.push(uf.toUpperCase()+' '+a.seats);tot.set(k,t)}}
  if(!estados)return '<p class="aviso">Ainda sem resultados para os estados escolhidos.</p>';
  const L=[...tot.values()].sort((a,b)=>b.seats-a.seats),max=L[0]?.seats||1;
  const totalVagas=[...sel.soma].reduce((s,u)=>s+vagasDep(c,u),0);
  return `<h3>Bancada projetada por partido ou federação</h3>
   <p class="sub">${cad} de ${totalVagas} cadeiras projetadas em ${estados} estado${estados>1?'s':''}, com os votos apurados até agora. Toque num estado no modo "Ver um estado" para ver os candidatos.</p>
   <ol>${L.map(t=>`<li class="c"><span class="cor" style="background:${corDe(t.n)}"></span>
     <span class="nm">${esc(t.nome)}<br><span class="pt">${t.ufs.join(', ')}</span></span><span class="pc">${t.seats}</span>
     <span class="vt"><span class="bar"><b style="width:${t.seats/max*100}%;background:${corDe(t.n)}"></b></span>${t.seats} cadeira${t.seats===1?'':'s'}</span></li>`).join('')}</ol>`;
}
document.getElementById('lista').addEventListener('click',e=>{const b=e.target.closest('.filtroSit button');if(!b)return;fSit=b.dataset.f;renderPainel()});

/* ================= vagas por partido (Senado e Governo) ================= */
function bancada(ufs,vagas){
  const d=store(),porP=new Map(),porUF=[];let ocupadas=0;
  for(const uf of ufs){const x=d[uf];if(!x)continue;
    const top=x.cands.filter(c=>c.votos>0).slice(0,vagas);ocupadas+=top.length;
    porUF.push({uf,top,prog:x.prog,prox:x.cands[vagas]});
    top.forEach(c=>{const p=partidoDe(c);const a=porP.get(p)||{p,n:String(c.n).slice(0,2),vagas:0,eleitos:0,ufs:[]};
      a.vagas++;if(isEleito(c))a.eleitos++;if(!a.ufs.includes(uf.toUpperCase()))a.ufs.push(uf.toUpperCase());porP.set(p,a)})}
  return {partidos:[...porP.values()].sort((a,b)=>b.vagas-a.vagas||a.p.localeCompare(b.p)),
          porUF:porUF.sort((a,b)=>UFS[a.uf].localeCompare(UFS[b.uf],'pt')),ocupadas};
}
const unid=(c,n)=>c.vagas===1?(n===1?'governo':'governos'):(n===1?'vaga':'vagas');
function renderBancada(c){
  const b=bancada([...sel.soma],c.vagas),total=sel.soma.size*c.vagas;
  if(!b.porUF.length)return '<p class="aviso">Ainda sem resultados para os estados escolhidos.</p>';
  const max=b.partidos[0]?.vagas||1;
  return `<h3>${c.id==='sen'?'Vagas no Senado por partido':'Governos por partido'}</h3>
   <p class="sub">${c.id==='sen'
     ?`Conta os 2 mais votados de cada estado. ${b.ocupadas} de ${total} vagas já têm alguém na frente.`
     :`Conta quem está em 1º lugar em cada estado. ${b.ocupadas} de ${total} estados com resultado.`}</p>
   <ol>${b.partidos.map(p=>`<li class="c"><span class="cor" style="background:${corDe(p.n)}"></span>
     <span class="nm">${esc(p.p)}<br><span class="pt">${p.ufs.join(', ')}</span></span>
     <span class="pc">${p.vagas}</span>
     <span class="vt"><span class="bar"><b style="width:${p.vagas/max*100}%;background:${corDe(p.n)}"></b></span>${p.vagas} ${unid(c,p.vagas)}${p.eleitos?` · ${p.eleitos} já eleito${p.eleitos>1?'s':''}`:''}</span></li>`).join('')}</ol>
   <h3>Por estado</h3>
   <ol class="porUF">${b.porUF.map(u=>`<li><button class="ufh" data-uf="${u.uf}"><b>${UFS[u.uf]}</b><span>${fpc(u.prog)}% apurado · ver detalhes</span></button>
     ${u.top.map((x,i)=>`<div class="ufc"><span class="cor" style="background:${corDe(x.n)}"></span>
       <span>${esc(x.nome)} <span class="pt">${esc(partidoDe(x))}</span>${isEleito(x)?'<span class="tag">Eleito</span>':''}</span>
       <span class="pcs">${fpc(x.pct)}%</span></div>`).join('')}
     ${u.prox&&c.id==='sen'?`<div class="ufc fora1"><span class="cor"></span><span>Próximo: ${esc(u.prox.nome)} <span class="pt">${esc(partidoDe(u.prox))}</span></span><span class="pcs">${fpc(u.prox.pct)}%</span></div>`:''}
     </li>`).join('')}</ol>`;
}
document.getElementById('lista').addEventListener('click',e=>{const b=e.target.closest('.ufh');if(!b)return;sel.modo='um';sel.uf=b.dataset.uf;sel.mun='';tudo()});

/* ================= abas e modo ================= */
const tabs=document.getElementById('tabs');
CARGOS.forEach(c=>{const b=document.createElement('button');b.textContent=c.nome;b.dataset.id=c.id;
  b.onclick=()=>{if(sel.cargo===c.id)return;sel.cargo=c.id;if(sel.modo==='cmp')sel.modo='um';filtro='';fSit='todos';document.getElementById('busca').value='';tMapa=0;tudo()};tabs.appendChild(b)});
const regDiv=document.getElementById('regioes');
function renderRegioes(){
  const btns=Object.entries(REGIOES).map(([nome,ufs])=>{const on=ufs.every(u=>sel.soma.has(u));return `<button data-r="${nome}" class="${on?'on':''}" aria-pressed="${on}">${nome}</button>`});
  regDiv.innerHTML=btns.join('')+`<span class="sep"></span><button data-r="inverter" class="acao">⇄ Inverter seleção</button><button data-r="todos">Todos</button><button data-r="limpar">Limpar</button>`;
}
regDiv.onclick=e=>{const r=e.target.dataset.r;if(!r)return;
  if(r==='todos')Object.keys(UFS).forEach(u=>sel.soma.add(u));
  else if(r==='limpar')sel.soma.clear();
  else if(r==='inverter')Object.keys(UFS).forEach(u=>sel.soma.has(u)?sel.soma.delete(u):sel.soma.add(u));
  else{const ufs=REGIOES[r];const on=ufs.every(u=>sel.soma.has(u));ufs.forEach(u=>on?sel.soma.delete(u):sel.soma.add(u))}
  tudo()};
function setModo(m){sel.modo=m;sel.mun='';if(m==='cmp'){sel.cargo='pres';sel.uf='br'}if(m==='soma'&&!sel.soma.size&&sel.uf!=='br')sel.soma.add(sel.uf);tudo()}
document.getElementById('mUm').onclick=()=>setModo('um');
document.getElementById('mSoma').onclick=()=>setModo('soma');
document.getElementById('mCmp').onclick=()=>setModo('cmp');
document.getElementById('voltar').onclick=()=>{sel.uf='br';sel.mun='';tudo()};
document.getElementById('pausar').onclick=e=>{pausado=!pausado;e.target.textContent=pausado?'Retomar':'Pausar';if(!pausado)ciclo();renderStatus()};
document.getElementById('busca').oninput=e=>{filtro=e.target.value;renderPainel()};

/* ================= mapa ================= */
const svg=d3.select('#mapa');let paths=null;
function ufOf(f){const p=f.properties||{};return (p.sigla||p.SIGLA||p.uf||'').toLowerCase()||NAME2UF[norm(p.name||p.nome||p.NOME)]}
async function initMapa(){
  try{
    const geo=await (await fetch('/geo')).json();
    const proj=d3.geoMercator().fitExtent([[8,8],[592,592]],geo),path=d3.geoPath(proj);
    paths=svg.append('g').selectAll('path').data(geo.features).join('path').attr('d',path).attr('fill','var(--land)')
      .attr('tabindex',0).attr('role','button').attr('aria-label',f=>UFS[ufOf(f)]||'')
      .on('click',(e,f)=>clicarUF(ufOf(f))).on('keydown',(e,f)=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();clicarUF(ufOf(f))}});
    paths.append('title');
    svg.append('g').selectAll('text').data(geo.features).join('text')
      .attr('x',f=>path.centroid(f)[0]).attr('y',f=>path.centroid(f)[1]+4).attr('text-anchor','middle')
      .text(f=>(ufOf(f)||'').toUpperCase());
  }catch(e){document.getElementById('legenda').textContent='Não consegui baixar o contorno dos estados. Confira a internet e recarregue a página.'}
}
function clicarUF(uf){
  if(!uf)return;
  if(sel.modo==='cmp'){sel.modo='um';sel.uf=uf;sel.mun=''}
  else if(sel.modo==='soma'){sel.soma.has(uf)?sel.soma.delete(uf):sel.soma.add(uf)}
  else{sel.uf=sel.uf===uf?'br':uf;sel.mun=''}
  tudo();
}
function pintarMapa(){
  if(!paths)return;
  const c=cargo(),dados=store(),lideres={},leg=document.getElementById('legenda');
  paths.attr('fill',f=>{
    const d=dados[ufOf(f)];if(!d)return 'var(--land)';
    if(!c.mapa)return d3.interpolateRgb('#D3D8D0','#1F8A4C')((d.prog||0)/100);
    const l=d.cands[0];if(!l||!l.votos)return 'var(--land)';
    const k=l.n;lideres[k]=lideres[k]||{nome:c.id==='pres'?l.nome:partidoDe(l),ufs:0};lideres[k].ufs++;
    const a=Math.min(1,.45+Math.max(0,l.pct-30)/45*.55);
    return d3.color(corDe(l.n)).copy({opacity:a}).formatRgb();
  })
  .classed('sel',f=>sel.modo==='cmp'?false:sel.modo==='soma'?sel.soma.has(ufOf(f)):ufOf(f)===sel.uf)
  .classed('fora',f=>{const u=ufOf(f);
    if(sel.modo==='cmp'){const l=dados[u]&&dados[u].cands[0];return !l||!cmpAtual.includes(l.n)}
    return sel.modo==='soma'&&sel.soma.size>0&&!sel.soma.has(u)});
  paths.select('title').text(f=>{const u=ufOf(f),d=dados[u];if(!d)return UFS[u];const l=d.cands[0];const top=d.cands.slice(0,c.vagas||1).map(x=>x.nome+' '+fpc(x.pct)+'%').join(' e ');return `${UFS[u]}: ${top} · ${fpc(d.prog)}% apurado`});
  if(c.vagas){
    const b=bancada(Object.keys(UFS),c.vagas);
    leg.innerHTML=b.partidos.length?b.partidos.map(p=>`<span><i style="background:${corDe(p.n)}"></i>${esc(p.p)} ${p.vagas} ${unid(c,p.vagas)}</span>`).join('')
      +(c.id==='sen'?'<span>A cor do estado mostra o 1º colocado; a contagem inclui as 2 vagas.</span>':'')
      :'O mapa ganha cor quando os primeiros votos forem totalizados.';
  }else if(c.mapa){
    // agrupa líderes por candidato
    const g={};Object.values(lideres).forEach(v=>{g[v.nome]=g[v.nome]||{ufs:0,n:''};g[v.nome].ufs+=v.ufs});
    Object.entries(lideres).forEach(([n,v])=>g[v.nome].n=n);
    const ls=Object.entries(g).sort((a,b)=>b[1].ufs-a[1].ufs);
    leg.innerHTML=ls.length?ls.map(([nome,v])=>`<span><i style="background:${corDe(v.n)}"></i>${esc(nome)} lidera em ${v.ufs}</span>`).join(''):'O mapa ganha cor quando os primeiros votos forem totalizados.';
  }else leg.textContent='Cor mais forte = mais urnas apuradas no estado. Toque num estado para ver quem tem chance de entrar.';
}

/* ================= municípios ================= */
async function initMuns(){
  try{const r=await fetch(`/tse/oficial/ele2026/${EL.fed}/config/mun-e${pad(EL.fed)}-cm.json`);if(!r.ok)return;
    (function rec(o){if(Array.isArray(o))return o.forEach(rec);if(!o||typeof o!=='object')return;
      if(typeof o.cd==='string'&&o.cd.length===2&&Array.isArray(o.mu)){muns[o.cd.toLowerCase()]=o.mu.map(m=>({cd:String(m.cd).padStart(5,'0'),nm:m.nm})).sort((a,b)=>a.nm.localeCompare(b.nm,'pt'));return}
      for(const k in o)rec(o[k])})(await r.json());
  }catch(e){}
}

/* ================= o que está sendo visto ================= */
function chaveAtual(){return sel.mun?sel.uf+sel.mun:sel.uf}
function dadosAtuais(){
  const c=cargo(),d=store();
  if(sel.modo==='cmp')return d.br||null;
  if(sel.modo==='soma'){const l=[...sel.soma].map(u=>d[u]).filter(Boolean);return l.length?somar(l,c.id!=='pres'):null}
  if(c.uf&&sel.uf==='br')return null;
  return d[chaveAtual()]||null;
}

/* ================= atualização ================= */
let tMapa=0,rodando=false;
async function ciclo(){
  if(pausado||rodando)return;rodando=true;
  const g=geracao,c=cargo(),d=store(),agora=Date.now();
  const alvos=new Set();
  if(sel.modo==='cmp')alvos.add('br');
  else if(sel.modo==='soma')sel.soma.forEach(u=>alvos.add(u));
  else if(!(c.uf&&sel.uf==='br'))alvos.add(chaveAtual());
  const intMapa=c.pesado?INTERVALO_MAPA_DEP:INTERVALO;
  const mapaAgora=agora-tMapa>=intMapa-500;
  if(mapaAgora){Object.keys(UFS).forEach(u=>alvos.add(u));tMapa=agora}
  let ok=0,rede=0,espera=0;
  if(c.id==='pres'&&(sel.modo==='cmp'||(sel.modo==='um'&&sel.uf==='br')))await carregarHist();
  await Promise.all([...alvos].map(async k=>{
    const uf=k.slice(0,2),mun=k.slice(2);
    try{d[k]=await getResult(c,uf,mun);ok++}catch(e){e.rede?rede++:espera++}
  }));
  rodando=false;
  if(g!==geracao)return;                           // mudou de aba no meio: descarta
  if(ok){ultimoOk=Date.now();statusRede='ok'}else if(rede)statusRede='erro';else if(espera)statusRede='espera';
  pintarMapa();renderPainel();renderStatus();
}
function tudo(){geracao++;rodando=false;renderEstrutura();pintarMapa();renderPainel();ciclo()}

/* ================= renderização ================= */
function renderStatus(){
  const live=document.getElementById('live'),t=document.getElementById('liveTxt');
  const s=pausado?'pausa':statusRede;
  live.className='live '+s;
  t.textContent={ok:'Ao vivo · a cada 5 s',espera:'Aguardando o TSE',erro:'Sem conexão',pausa:'Pausado',conectando:'Conectando…'}[s];
  const d=dadosAtuais();
  document.getElementById('horaTse').textContent=d&&d.hora?`${d.hora}${d.data?' de '+d.data:''}`:'—';
}
setInterval(()=>{const el=document.getElementById('conferido');
  if(!ultimoOk){el.textContent='—';return}
  const s=Math.round((Date.now()-ultimoOk)/1000);el.textContent=s<2?'agora':`há ${s} s`;
  if(!pausado&&statusRede==='ok'&&s>30){statusRede='erro';renderStatus()}},1000);

function renderEstrutura(){
  const c=cargo();
  [...tabs.children].forEach(b=>b.setAttribute('aria-pressed',b.dataset.id===sel.cargo));
  document.getElementById('mUm').setAttribute('aria-pressed',sel.modo==='um');
  document.getElementById('mSoma').setAttribute('aria-pressed',sel.modo==='soma');
  document.getElementById('mCmp').setAttribute('aria-pressed',sel.modo==='cmp');
  regDiv.hidden=sel.modo!=='soma';if(sel.modo==='soma')renderRegioes();
  document.getElementById('dica').textContent=sel.modo==='cmp'
    ?'O mapa destaca os estados onde um dos dois candidatos escolhidos está em 1º. Toque num estado para ver o detalhe.'
    :sel.modo==='soma'
    ?'Toque nos estados para incluir ou tirar da soma, ou escolha uma região.'
    :'Toque num estado para ver o resultado dele. Toque de novo para voltar ao Brasil.';
  // título
  const t=document.getElementById('titulo'),v=document.getElementById('voltar'),sub=document.getElementById('subtitulo');
  if(sel.modo==='cmp'){t.textContent='Comparar candidatos';v.hidden=true;sub.textContent='Presidente · onde cada um está em 1º'}
  else if(sel.modo==='soma'){
    const n=sel.soma.size;t.textContent=n===27?'Todos os estados':n?`Soma de ${n} estado${n>1?'s':''}`:'Somar estados';v.hidden=true;
    const nomeReg=Object.entries(REGIOES).find(([,u])=>u.length===n&&u.every(x=>sel.soma.has(x)));
    sub.textContent=n?(nomeReg?'Região '+nomeReg[0]:[...sel.soma].map(u=>u.toUpperCase()).sort().join(', '))+` · ${c.nome}`:'';
  }else if(sel.uf==='br'){t.textContent='Brasil';v.hidden=true;sub.textContent=c.nome}
  else{const m=sel.mun&&(muns[sel.uf]||[]).find(x=>x.cd===sel.mun);t.textContent=m?`${m.nm} (${sel.uf.toUpperCase()})`:UFS[sel.uf];v.hidden=false;sub.textContent=c.pesado&&!m?`${c.nome} · ${vagasDep(c,sel.uf)} cadeiras`:c.nome}
  // município
  const ctrl=document.getElementById('ctrl');ctrl.innerHTML='';
  if(sel.modo==='um'&&sel.uf!=='br'&&muns[sel.uf]){
    const s=document.createElement('select');s.setAttribute('aria-label','Município');
    s.innerHTML=`<option value="">Estado inteiro</option>`+muns[sel.uf].map(m=>`<option value="${m.cd}">${esc(m.nm)}</option>`).join('');
    s.value=sel.mun;s.onchange=()=>{sel.mun=s.value;tudo()};ctrl.appendChild(s);
  }
}

function renderPainel(){
  const c=cargo(),res=document.getElementById('resumo'),lst=document.getElementById('lista'),busca=document.getElementById('busca');
  renderStatus();
  if(document.activeElement&&document.activeElement.classList.contains('cmpSel'))return;   // não fecha o seletor aberto
  if(document.querySelector('#grafico .tip')?.style.display===''){renderStatus();return}     // usuário olhando o gráfico
  if(sel.modo==='soma'&&!sel.soma.size){res.innerHTML=`<p class="aviso">Toque nos estados no mapa ou escolha uma região acima dele para somar os votos.</p>`;lst.innerHTML='';busca.hidden=true;return}
  if(sel.modo==='um'&&c.uf&&sel.uf==='br'){
    res.innerHTML=`<p class="aviso">${c.nome} é eleito por estado. Toque num estado no mapa, ou <button class="linkbtn" id="somaTodos">some todos os estados</button> para ver os votos por partido no país.</p>`;
    document.getElementById('somaTodos').onclick=()=>{Object.keys(UFS).forEach(u=>sel.soma.add(u));setModo('soma')};
    lst.innerHTML='';busca.hidden=true;return}
  const d=dadosAtuais();
  if(!d){res.innerHTML=statusRede==='erro'
      ?`<p class="aviso">Não consegui falar com o TSE. Confira a internet; a página tenta de novo sozinha.</p>`
      :`<p class="aviso">O TSE ainda não publicou este resultado. A página tenta de novo sozinha a cada 5 segundos.</p>`;
    lst.innerHTML='';busca.hidden=true;return}

  const ex=[];
  if(d.pComp!=null)ex.push(['Comparecimento',fpc(d.pComp)+'%']);
  if(d.pAbst!=null)ex.push(['Abstenção',fpc(d.pAbst)+'%']);
  if(d.pBr!=null)ex.push(['Brancos',fpc(d.pBr)+'%']);
  if(d.pNu!=null)ex.push(['Nulos',fpc(d.pNu)+'%']);
  const R=faltam(d);
  if(R)ex.unshift(['Votos que faltam (estim.)','≈ '+fmt.format(R)]);
  else if(d.prog>=100)ex.unshift(['Votos que faltam','0']);
  let vant='',regra='';
  const [a,b]=d.cands;
  if(c.id==='sen'&&sel.modo==='um'){
    const v=c.vagas,x2=d.cands[v-1],x3=d.cands[v];
    regra=`<p class="regra">Este estado elege <b>${v} senadores</b>. Cada eleitor vota em ${v} candidatos e os ${v} mais votados ficam com as vagas. Partido não entra na conta.</p>`;
    if(x2&&x3&&x2.votos)vant=`Disputa pela ${v}ª vaga: <b>${esc(x2.nome)}</b> tem <b>${fmt.format(x2.votos-x3.votos)}</b> votos a mais que ${esc(x3.nome)}.`+textoVirada(x2,x3,R);
  }else if(sel.modo==='soma'&&c.vagas){}
  else if(a&&b&&a.votos&&!c.pesado)vant=`<b>${esc(a.nome)}</b> está à frente de ${esc(b.nome)} por <b>${fmt.format(a.votos-b.votos)}</b> votos (${fpc(a.pct-b.pct)} pontos).`+textoVirada(a,b,R);
  res.innerHTML=`${regra}
    <div class="prog"><span class="num">${fpc(d.prog)}%</span><span class="lbl">das urnas apuradas</span>
    <div class="bar"><b style="width:${Math.min(100,d.prog||0)}%"></b></div></div>
    ${d.st!=null&&d.s!=null?`<p class="urnas"><b>${fmt.format(d.st)}</b> de ${fmt.format(d.s)} urnas apuradas · faltam ${fmt.format(Math.max(0,d.s-d.st))}</p>`:''}
    ${ex.length?`<div class="extras">${ex.map(([k,v])=>`<div><span>${k}</span><strong>${v}</strong></div>`).join('')}</div>`:''}
    <p class="vantagem">${vant}</p>`;

  if(sel.modo==='cmp'){busca.hidden=true;lst.innerHTML=htmlGrafico()+(renderComparacao(d)||'<p class="aviso">A comparação aparece assim que os estados tiverem votos apurados.</p>');desenharGrafico(d);return}
  if(sel.modo==='soma'&&c.vagas){busca.hidden=true;lst.innerHTML=renderBancada(c);return}
  if(c.pesado){
    if(sel.modo==='soma'){busca.hidden=true;lst.innerHTML=renderBancadaDep(c);return}
    busca.hidden=false;const op=lst.querySelector('details.como')?.open;lst.innerHTML=renderDeputados(d,c);
    if(op)lst.querySelector('details.como').open=true;return}
  busca.hidden=d.cands.length<=12;
  const f=norm(filtro);
  const lista=d.cands.filter(x=>!f||norm(x.nome).includes(f)||norm(x.partido).includes(f)||x.n.startsWith(filtro.trim()));
  const lim=c.pesado&&!d.porPartido?60:40,max=d.cands[0]?.pct||1;
  const sen=c.id==='sen'&&!d.soma&&!filtro;
  const tag=(x,i)=>{const st=norm(x.st);if(isEleito(x))return '<span class="tag">Eleito</span>';if(st.includes('2'))return '<span class="tag t2">2º turno</span>';if(sen&&i<c.vagas&&x.votos)return '<span class="tag vaga">Na vaga</span>';return ''};
  lst.innerHTML=`<ol>${lista.slice(0,lim).map((x,i)=>`${sen&&i===c.vagas?'<li class="corte">Linha de corte · abaixo daqui, fora das 2 vagas</li>':''}<li class="c"><span class="cor" style="background:${corDe(x.n)}"></span>
     <span class="nm">${esc(x.nome)}${tag(x,i)}<br><span class="pt">${d.porPartido?`Número ${esc(x.n)} · candidatos em ${x.ufs} estado${x.ufs>1?'s':''}`:`${esc(partidoDe(x))} · ${esc(x.n)}${d.soma&&x.primeiro?` · 1º em ${x.primeiro} estado${x.primeiro>1?'s':''}`:''}`}</span></span>
     <span class="pc">${fpc(x.pct)}%</span>
     <span class="vt"><span class="bar"><b style="width:${Math.max(.5,x.pct/max*100)}%;background:${corDe(x.n)}"></b></span>${fmt.format(x.votos)} votos</span></li>`).join('')}</ol>
   ${lista.length>lim?`<p class="mais">Mostrando ${lim} de ${lista.length}. Use a busca para achar outros.</p>`:''}
   ${!lista.length&&filtro?'<p class="mais">Nenhum resultado para essa busca.</p>':''}`
   +(c.id==='pres'&&sel.modo==='um'&&sel.uf==='br'&&!filtro?'<p class="mais"><button class="linkbtn" id="irCmp">Comparar os candidatos estado a estado</button></p>':'');
  const ir=document.getElementById('irCmp');if(ir)ir.onclick=()=>setModo('cmp');
  if(c.id==='pres'&&sel.modo==='um'&&sel.uf==='br'&&!filtro){lst.insertAdjacentHTML('afterbegin',htmlGrafico());desenharGrafico(d)}
}

/* ================= início ================= */
document.getElementById('turnoTag').textContent=T2?'· 2º turno':'· 1º turno';
(async()=>{renderEstrutura();await Promise.all([initMapa(),initMuns()]);tudo();setInterval(ciclo,INTERVALO)})();
</script>
</body>
</html>
"""


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Apuração 2026 rodando em {url}  (Ctrl+C para parar)")
    print("Cobrança via Pix: LIGADA" if COBRAR else "Cobrança via Pix: desligada (sem MP_ACCESS_TOKEN)")
    if COBRAR and not os.environ.get("SEGREDO"):
        print("Aviso: defina SEGREDO para que os acessos pagos não se percam quando o servidor reiniciar.")
    _carregar_hist()
    threading.Thread(target=_gravador, daemon=True).start()
    print("Gravando o histórico da apuração de Presidente nesta pasta (historico-presidente-*.json).")
    if not NA_NUVEM:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
