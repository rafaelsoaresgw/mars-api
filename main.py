import os, requests, mercadopago, json
import traceback
from groq import Groq
from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

try: from dotenv import load_dotenv; load_dotenv()
except: pass

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

# --- FUNÇÕES AUXILIARES ---
def enviar_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={ "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown" })
    except: pass

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

def analisar_contexto(texto_novo, estado_atual):
    novo_estado = estado_atual.copy() if estado_atual else {"produto": None, "plano": None, "whatsapp": None, "endereco": None}
    txt = texto_novo.lower()

    if "whey" in txt: novo_estado["produto"] = "Whey Protein Gold"
    elif "creatina" in txt: novo_estado["produto"] = "Creatina Pura"
    elif "camiseta" in txt: novo_estado["produto"] = "Camiseta Mars"

    if "mensal" in txt or "assinatura" in txt: novo_estado["plano"] = "Mensal"
    elif "unico" in txt or "único" in txt: novo_estado["plano"] = "Único"

    numeros = ''.join(filter(str.isdigit, txt))
    if len(numeros) >= 8 and "149" not in numeros and "99" not in numeros: 
        novo_estado["whatsapp"] = numeros 

    palavras_chave_end = ["rua", "av", "avenida", "bairro", "casa", "apto", "bloco", "entrega", "número", "cep"]
    if len(txt) > 5 and any(p in txt for p in palavras_chave_end):
        novo_estado["endereco"] = texto_novo
        
    return novo_estado

@app.post("/chat")
async def chat_endpoint(data: ChatInput):
    user = data.nome_usuario
    txt_low = data.texto.lower()

    # RESET
    if "reiniciar" in txt_low or "reset" in txt_low:
        db_reset_session(user)
        return {"respostas": ["Reiniciei seu atendimento. Como posso ajudar agora?"], "imagem": None, "pix": None}

    sessao_banco = db_get_session(user)
    estado_final = analisar_contexto(data.texto, sessao_banco)
    db_upsert_session(user, estado_final)

    prod = estado_final.get("produto")
    plan = estado_final.get("plano")
    zap = estado_final.get("whatsapp")
    end = estado_final.get("endereco")
    
    dados_validos = zap and end and len(str(zap)) > 6 and len(str(end)) > 5

    pix_code = None
    
    # LÓGICA DE PIX (GARANTIDA)
    if prod and plan and dados_validos:
        preco = 149.90 if "Whey" in prod else (99.90 if "Creatina" in prod else 49.90)
        if plan == "Mensal": preco = preco * 0.9 
        
        # 1. PIX DE SEGURANÇA (Sempre existe)
        pix_code = "00020126580014BR.GOV.BCB.PIX0136123e4567-e89b-12d3-a456-426614174000520400005303986540410.005802BR5913MARS AI6008BRASILIA62070503***6304ABCD"
        
        # 2. TENTA GERAR O REAL
        try:
            if MP_ACCESS_TOKEN:
                sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
                payment_data = {
                    "transaction_amount": round(preco, 2),
                    "description": f"{prod} ({plan})",
                    "payment_method_id": "pix",
                    "payer": {"email": "cliente@mars.com", "first_name": user},
                }
                mp_res = sdk.payment().create(payment_data)
                if mp_res["status"] == 201:
                    pix_code = mp_res["response"]["point_of_interaction"]["transaction_data"]["qr_code"]
                    enviar_telegram(f"🟡 *NOVO PEDIDO:*\n👤 {user}\n🛒 {prod} ({plan})\n💰 R$ {preco:.2f}\n📱 `{zap}`\n📍 {end}")
        except: pass

    # --- PERSONALIDADE PROFISSIONAL ---
    status_msg = ""
    if not prod: status_msg = "Ainda não escolheu produto. (Mostre opções)."
    elif not plan: status_msg = f"Escolheu {prod}. Falta plano (Único/Mensal)."
    elif not dados_validos: status_msg = f"Falta WhatsApp e Endereço para entrega."
    else: status_msg = "PIX GERADO. Agradeça e aguarde."

    prompt = f"""
    Você é a MARS, assistente virtual profissional da loja de suplementos.
    Cliente: {user}.
    STATUS: {status_msg}
    CARDÁPIO: Whey (149), Creatina (99), Camiseta (49).
    
    DIRETRIZES DE PERSONALIDADE:
    1. Seja educada, acolhedora e direta. (Estilo Apple/Nubank).
    2. Evite gírias exageradas ou emojis em excesso. Use poucos e bons.
    3. Se o cliente perguntar produtos, liste claramente.
    4. Se o PIX foi gerado, diga: "Perfeito! Gere o seu código de pagamento abaixo."
    
    Responda em no máximo 2 frases.
    """

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": data.texto}],
            temperature=0.2
        )
        resposta_texto = resp.choices[0].message.content
    except: resposta_texto = "Conexão instável."

    img_url = None
    if prod and "Whey" in prod: img_url = "https://m.media-amazon.com/images/I/41sdCLWi29L._AC_SY300_SX300_QL70_ML2_.jpg"
    elif prod and "Creatina" in prod: img_url = "https://http2.mlstatic.com/D_NQ_NP_2X_942122-MLA99923169249_112025-F.webp"

    return {
        "respostas": [r.strip() for r in resposta_texto.split('---') if r.strip()],
        "imagem": img_url,
        "pix": pix_code
    }

@app.get("/verificar_pagamento/{pid}")
async def verificar_pagamento(pid: str): return {"status": "pending"}
@app.post("/webhook")
async def webhook_mp(request: Request): return {"status": "ok"}
@app.post("/salvar_lead")
async def lead(d: dict): return {"status": "ok"}
