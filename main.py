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
        return dados[0] if len(dados) > 0 else {}
    except: return {}

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

# --- CÉREBRO (LÓGICA DE CONTEXTO) ---
def analisar_contexto(texto_novo, estado_atual):
    novo_estado = estado_atual.copy() if estado_atual else {}
    
    # Garante campos padrão
    for k in ["produto", "plano", "whatsapp", "endereco", "pix_gerado"]:
        if k not in novo_estado: novo_estado[k] = None

    txt = texto_novo.lower()

    # DETEÇÃO DE PRODUTO (Só muda se detetar um novo)
    if "whey" in txt: novo_estado["produto"] = "Whey Protein Gold"
    elif "creatina" in txt: novo_estado["produto"] = "Creatina Pura"
    elif "camiseta" in txt: novo_estado["produto"] = "Camiseta Mars"

    # DETEÇÃO DE PLANO
    if "mensal" in txt or "assinatura" in txt:
        novo_estado["plano"] = "Mensal"
    elif any(x in txt for x in ["unico", "único", "avista", "à vista", "uma vez"]):
        novo_estado["plano"] = "Único"

    # DETEÇÃO DE CONTACTO
    numeros = ''.join(filter(str.isdigit, txt))
    if 10 <= len(numeros) <= 11:
        novo_estado["whatsapp"] = numeros

    # DETEÇÃO DE ENDEREÇO
    keywords = ["rua", "av", "avenida", "bairro", "casa", "apto", "entrega", "cep", "nº", "numero"]
    if len(txt) > 8 and any(k in txt for k in keywords):
        novo_estado["endereco"] = texto_novo

    return novo_estado

@app.get("/")
def root():
    return {"sistema": "MARS AI", "status": "online"}

@app.post("/chat")
async def chat_endpoint(data: ChatInput):
    user = data.nome_usuario
    txt_low = data.texto.lower()

    if any(x in txt_low for x in ["reiniciar", "reset", "limpar"]):
        db_reset_session(user)
        return {"respostas": ["Memória resetada! O que vamos treinar hoje?"], "imagem": None, "pix": None}

    sessao = db_get_session(user)
    estado = analisar_contexto(data.texto, sessao)
    
    prod = estado.get("produto")
    plan = estado.get("plano")
    zap = estado.get("whatsapp")
    end = estado.get("endereco")
    pix_gerado = estado.get("pix_gerado", False)

    # Lógica de Status para a IA
    if pix_gerado:
        status_msg = f"PEDIDO CONCLUÍDO de {prod}. Ele já tem o PIX."
    elif prod and not plan:
        status_msg = f"O cliente ESCOLHEU {prod}. Agora OBRIGATORIAMENTE pergunte se ele quer o plano ÚNICO ou MENSAL (com 10% desc)."
    elif prod and plan and (not zap or not end):
        status_msg = f"Produto: {prod}, Plano: {plan}. Agora peça o WhatsApp e Endereço para entrega."
    elif not prod:
        status_msg = "Cliente ainda não escolheu. APRESENTE O CARDÁPIO: Whey (149,90), Creatina (99,90), Camiseta (49,90)."
    else:
        status_msg = "Tudo pronto. Gerando PIX."

    prompt = f"""
    Você é a MARS, IA da loja de suplementos. Seja motivadora e direta.
    Contexto Atual: {status_msg}
    Cliente: {user}
    
    REGRAS:
    1. Se o cliente já escolheu o produto, NÃO ofereça o cardápio de novo. Foque no próximo passo (Plano ou Dados).
    2. Use emojis de treino.
    3. Se o status diz que falta o Plano, insista apenas no Plano.
    """

    # Gerar resposta via Groq
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": data.texto}],
            temperature=0.4
        )
        resposta_texto = resp.choices[0].message.content
    except:
        resposta_texto = "Estou com um pequeno lag nos sensores. Podes repetir?"

    # Geração de PIX (se tiver tudo e ainda não gerou)
    pix_code = None
    payment_id = None
    if prod and plan and zap and end and not pix_gerado:
        # Preços
        precos = {"Whey Protein Gold": 149.90, "Creatina Pura": 99.90, "Camiseta Mars": 49.90}
        valor = precos.get(prod, 50.0)
        if plan == "Mensal": valor *= 0.9
        
        try:
            sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
            pay_res = sdk.payment().create({
                "transaction_amount": round(valor, 2),
                "description": f"{prod} - {plan}",
                "payment_method_id": "pix",
                "payer": {"email": "cliente@mars.com"}
            })
            if pay_res["status"] == 201:
                pix_code = pay_res["response"]["point_of_interaction"]["transaction_data"]["qr_code"]
                payment_id = str(pay_res["response"]["id"])
                estado["pix_gerado"] = True
                estado["payment_id"] = payment_id
                enviar_telegram(f"🔥 *NOVO PIX:* {user} - {prod} (R$ {valor:.2f})")
        except: pass

    db_upsert_session(user, estado)

    img_url = None
    if prod == "Whey Protein Gold": img_url = "https://m.media-amazon.com/images/I/41sdCLWi29L._AC_SY300_SX300_QL70_ML2_.jpg"
    elif prod == "Creatina Pura": img_url = "https://http2.mlstatic.com/D_NQ_NP_2X_942122-MLA99923169249_112025-F.webp"

    return {
        "respostas": [r.strip() for r in resposta_texto.split('---') if r.strip()],
        "imagem": img_url,
        "pix": pix_code,
        "payment_id": payment_id
    }

@app.get("/verificar_pagamento/{pid}")
async def verificar_pagamento(pid: str):
    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
        res = sdk.payment().get(pid)
        return {"status": res["response"]["status"]}
    except: return {"status": "error"}
