import os, requests, mercadopago, json
import traceback
from groq import Groq
from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

try: 
    from dotenv import load_dotenv
    load_dotenv()
except: 
    pass

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- CONFIGS ---
CHAVE_GROQ = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = Groq(api_key=CHAVE_GROQ) if CHAVE_GROQ else None

class ChatInput(BaseModel):
    texto: str
    nome_usuario: str
    produto_identificado: str = ""
    plano_identificado: str = ""
    contato_ok: bool = False

# --- TELEGRAM ---
def enviar_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = { "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown" }
        requests.post(url, json=payload)
    except: pass

# --- BANCO DE DADOS (SUPABASE) ---
def db_get_session(user_id):
    if not SUPABASE_URL: return None
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/sessoes_venda?user_id=eq.{user_id}", headers=headers)
        dados = r.json()
        return dados[0] if len(dados) > 0 else None
    except: return None

def db_upsert_session(user_id, dados):
    if not SUPABASE_URL: return
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}
    dados['user_id'] = user_id
    try: requests.post(f"{SUPABASE_URL}/rest/v1/sessoes_venda", json=dados, headers=headers)
    except: pass

def db_reset_session(user_id):
    if not SUPABASE_URL: return
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try: requests.delete(f"{SUPABASE_URL}/rest/v1/sessoes_venda?user_id=eq.{user_id}", headers=headers)
    except: pass

# --- ROTAS DE STATUS E PEDIDOS ---

@app.get("/")
def root():
    """Rota principal para evitar erro 404 e confirmar que a API está ativa"""
    return {
        "sistema": "MARS AI",
        "status": "online",
        "mensagem": "API rodando com sucesso! Acesse o frontend no Netlify."
    }

@app.get("/pedidos")
def listar_pedidos():
    """Retorna todos os pedidos que já geraram PIX (sessões com pix_gerado=True)"""
    if not SUPABASE_URL:
        return {"error": "Supabase não configurado"}
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/sessoes_venda?pix_gerado=eq.true", headers=headers)
        return r.json()
    except:
        return {"error": "Erro ao buscar pedidos"}

# --- CÉREBRO (LÓGICA) ---
def analisar_contexto(texto_novo, estado_atual):
    novo_estado = estado_atual.copy() if estado_atual else {}
    defaults = {"produto": None, "plano": None, "whatsapp": None, "endereco": None, "pix_gerado": False}
    for k, v in defaults.items():
        if k not in novo_estado:
            novo_estado[k] = v

    txt = texto_novo.lower()

    if "whey" in txt:
        novo_estado["produto"] = "Whey Protein Gold"
    elif "creatina" in txt:
        novo_estado["produto"] = "Creatina Pura"
    elif "camiseta" in txt:
        novo_estado["produto"] = "Camiseta Mars"

    if "mensal" in txt or "assinatura" in txt:
        novo_estado["plano"] = "Mensal"
    elif "unico" in txt or "único" in txt or "avista" in txt:
        novo_estado["plano"] = "Único"

    numeros = ''.join(filter(str.isdigit, txt))
    if len(numeros) >= 10 and len(numeros) <= 11:
        novo_estado["whatsapp"] = numeros

    palavras_chave_end = ["rua", "av", "avenida", "bairro", "casa", "apto", "bloco", "entrega", "número", "cep", "logradouro"]
    if len(txt) > 5 and any(p in txt for p in palavras_chave_end):
        novo_estado["endereco"] = texto_novo

    return novo_estado

@app.post("/chat")
async def chat_endpoint(data: ChatInput):
    user = data.nome_usuario
    txt_low = data.texto.lower()

    if "reiniciar" in txt_low or "reset" in txt_low:
        db_reset_session(user)
        return {"respostas": ["Beleza! Memória apagada. --- O que você manda hoje, atleta?"], "imagem": None, "pix": None}

    sessao_banco = db_get_session(user) or {}
    estado_final = analisar_contexto(data.texto, sessao_banco)
    
    prod = estado_final.get("produto")
    plan = estado_final.get("plano")
    zap = estado_final.get("whatsapp")
    end = estado_final.get("endereco")
    pix_gerado = estado_final.get("pix_gerado", False)

    dados_validos = zap and end and len(str(zap)) > 6 and len(str(end)) > 5

    pix_code = None
    payment_id = None

    if prod and plan and dados_validos and not pix_gerado:
        if "Whey" in prod: preco = 149.90
        elif "Creatina" in prod: preco = 99.90
        else: preco = 49.90

        if plan == "Mensal": preco = round(preco * 0.9, 2)

        try:
            if MP_ACCESS_TOKEN:
                sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
                payment_data = {
                    "transaction_amount": preco,
                    "description": f"{prod} ({plan})",
                    "payment_method_id": "pix",
                    "payer": {"email": "cliente@mars.com", "first_name": user},
                }
                mp_res = sdk.payment().create(payment_data)
                if mp_res["status"] == 201:
                    pix_code = mp_res["response"]["point_of_interaction"]["transaction_data"]["qr_code"]
                    payment_id = str(mp_res["response"]["id"])
                    estado_final["pix_gerado"] = True
                    estado_final["payment_id"] = payment_id
                    enviar_telegram(f"🟡 *NOVO PEDIDO:*\n👤 {user}\n🛒 {prod} ({plan})\n💰 R$ {preco:.2f}\n📱 `{zap}`\n📍 {end}")
        except Exception as e:
            print("Erro ao gerar PIX:", e)

    db_upsert_session(user, estado_final)

    if pix_gerado:
        status_msg = f"Pedido de {prod} ({plan}) já gerou PIX. Cliente pode perguntar sobre outros produtos ou status do pagamento."
    elif not prod:
        status_msg = "Cliente ainda não escolheu produto. OFEREÇA O CARDÁPIO COMPLETO."
    elif not plan:
        status_msg = f"Cliente escolheu {prod}. Falta definir o plano (Único ou Mensal)."
    elif not dados_validos:
        status_msg = f"Cliente vai levar {prod} ({plan}). Falta WhatsApp e Endereço."
    else:
        status_msg = f"Todos os dados coletados. PIX será gerado."

    prompt = f"""
    Você é a MARS, IA da loja de suplementos.
    Cliente: {user}.
    STATUS: {status_msg}
    Pedido já finalizado? {"sim" if pix_gerado else "não"}

    CARDÁPIO:
    - Whey Protein Gold (R$ 149,90)
    - Creatina Pura (R$ 99,90)
    - Camiseta Mars (R$ 49,90)

    SUAS REGRAS:
    1. Se perguntarem "quais produtos", liste todas as opções.
    2. Se escolher um produto, comemore e pergunte o Plano (Único ou Mensal).
    3. Responda com energia e emojis.
    """

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": data.texto}],
            temperature=0.3
        )
        resposta_texto = resp.choices[0].message.content
    except Exception as e:
        resposta_texto = "Conexão instável. Tente novamente em instantes."

    img_url = None
    if prod and "Whey" in prod:
        img_url = "https://m.media-amazon.com/images/I/41sdCLWi29L._AC_SY300_SX300_QL70_ML2_.jpg"
    elif prod and "Creatina" in prod:
        img_url = "https://http2.mlstatic.com/D_NQ_NP_2X_942122-MLA99923169249_112025-F.webp"

    return {
        "respostas": [r.strip() for r in resposta_texto.split('---') if r.strip()],
        "imagem": img_url,
        "pix": pix_code,
        "payment_id": payment_id
    }

@app.get("/verificar_pagamento/{pid}")
async def verificar_pagamento(pid: str):
    if not MP_ACCESS_TOKEN: return {"status": "pending"}
    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
        res = sdk.payment().get(pid)
        return {"status": res["response"]["status"]}
    except: return {"status": "error"}

@app.post("/webhook")
async def webhook_mp(request: Request):
    try:
        data = await request.json()
        if data.get("type") == "payment":
            p_id = data["data"]["id"]
            sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
            info = sdk.payment().get(p_id)
            if info["response"]["status"] == "approved":
                val = info["response"]["transaction_amount"]
                enviar_telegram(f"🟢 *VENDA APROVADA!* R$ {val}")
        return {"status": "ok"}
    except: return {"status": "error"}
