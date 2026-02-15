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
    produto_identificado: str = ""
    plano_identificado: str = ""
    contato_ok: bool = False

# --- TELEGRAM ---
def enviar_telegram(msg):
    print(f"Enviando Telegram...")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = { "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown" }
        requests.post(url, json=payload)
    except: pass

# --- BANCO DE DADOS ---
def db_get_session(user_id):
    if not SUPABASE_URL: return None
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/sessoes_venda?user_id=eq.{user_id}", headers=headers)
        dados = r.json()
        if len(dados) > 0: return dados[0]
        return None
    except: return None

def db_upsert_session(user_id, dados):
    if not SUPABASE_URL: return
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}
    dados['user_id'] = user_id
    try: requests.post(f"{SUPABASE_URL}/rest/v1/sessoes_venda", json=dados, headers=headers)
    except: pass

# --- CÉREBRO DA CAPTURA DE DADOS ---
def analisar_contexto(texto_novo, estado_atual):
    novo_estado = estado_atual.copy() if estado_atual else {"produto": None, "plano": None, "whatsapp": None, "endereco": None}
    txt = texto_novo.lower()

    # 1. Produto
    if "whey" in txt: novo_estado["produto"] = "Whey Protein Gold"
    elif "creatina" in txt: novo_estado["produto"] = "Creatina Pura"
    elif "camiseta" in txt: novo_estado["produto"] = "Camiseta Mars"

    # 2. Plano
    if "mensal" in txt or "assinatura" in txt: novo_estado["plano"] = "Mensal"
    elif "unico" in txt or "único" in txt: novo_estado["plano"] = "Único"

    # 3. WhatsApp (Captura o número real)
    numeros = ''.join(filter(str.isdigit, txt))
    # Se tiver mais de 8 digitos e não for o preço (149 ou 99), assumimos que é telefone
    if len(numeros) >= 8 and "149" not in numeros and "99" not in numeros: 
        novo_estado["whatsapp"] = numeros 

    # 4. Endereço (Captura a mensagem toda se parecer endereço)
    palavras_chave_end = ["rua", "av", "avenida", "bairro", "casa", "apto", "bloco", "entrega", "número"]
    if len(txt) > 5 and any(p in txt for p in palavras_chave_end):
        novo_estado["endereco"] = texto_novo # Salva o texto original com maiúsculas
        
    return novo_estado

@app.post("/chat")
async def chat_endpoint(data: ChatInput):
    user = data.nome_usuario
    sessao_banco = db_get_session(user)
    
    # Atualiza memória
    estado_final = analisar_contexto(data.texto, sessao_banco)
    db_upsert_session(user, estado_final)

    prod = estado_final.get("produto")
    plan = estado_final.get("plano")
    zap = estado_final.get("whatsapp")
    end = estado_final.get("endereco")
    
    # Verifica validade dos dados (tem que ter conteúdo)
    dados_validos = zap and end and len(str(zap)) > 6 and len(str(end)) > 5

    # --- CHECKOUT E NOTIFICAÇÃO ---
    pix_code = None
    if prod and plan and dados_validos:
        preco = 149.90 if "Whey" in prod else (99.90 if "Creatina" in prod else 49.90)
        if plan == "Mensal": preco = preco * 0.9 
        
        pix_code = "00020126580014BR.GOV.BCB.PIX0136123e4567-e89b-12d3-a456-426614174000520400005303986540410.005802BR5913MARS AI6008BRASILIA62070503***6304ABCD"
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
                    
                    # --- MENSAGEM DETALHADA PARA O TELEGRAM ---
                    msg_telegram = (
                        f"🚀 *NOVA VENDA NO SITE!*\n"
                        f"👤 *Cliente:* {user}\n"
                        f"🛒 *Produto:* {prod}\n"
                        f"📄 *Plano:* {plan}\n"
                        f"💰 *Valor:* R$ {preco:.2f}\n"
                        f"📱 *WhatsApp:* `{zap}`\n"
                        f"📍 *Endereço:* {end}"
                    )
                    enviar_telegram(msg_telegram)
        except: pass

    # --- PROMPT INTELIGENTE (DIRETO) ---
    instrucoes = ""
    if not prod: instrucoes += "FALTA: Produto (Cardápio: Whey, Creatina, Camiseta). "
    elif not plan: instrucoes += f"TEMOS: {prod}. FALTA: Plano (Único ou Mensal?). "
    elif not dados_validos: instrucoes += f"TEMOS: {prod} ({plan}). FALTA: WhatsApp e Endereço. "
    else: instrucoes += "TEMOS TUDO. Diga: 'Perfeito! Segue o PIX abaixo.' "

    prompt = f"""
    Você é MARS, IA de vendas. Cliente: {user}.
    ESTADO DA VENDA: {instrucoes}
    
    REGRAS:
    1. Se o cliente pedir pra mudar, aceite e pergunte o novo.
    2. Responda em MAX 15 palavras.
    3. Se o PIX foi gerado, peça apenas o pagamento.
    """

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": data.texto}],
            temperature=0.1
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
async def ver(pid): return {"status": "pending"}
@app.post("/salvar_lead")
async def lead(d: dict): return {"status": "ok"}
