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

# --- FUNÇÃO DE NOTIFICAÇÃO ---
def enviar_telegram(msg):
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

    palavras_chave_end = ["rua", "av", "avenida", "bairro", "casa", "apto", "bloco", "entrega", "número"]
    if len(txt) > 5 and any(p in txt for p in palavras_chave_end):
        novo_estado["endereco"] = texto_novo
        
    return novo_estado

@app.post("/chat")
async def chat_endpoint(data: ChatInput):
    user = data.nome_usuario
    sessao_banco = db_get_session(user)
    
    estado_final = analisar_contexto(data.texto, sessao_banco)
    db_upsert_session(user, estado_final)

    prod = estado_final.get("produto")
    plan = estado_final.get("plano")
    zap = estado_final.get("whatsapp")
    end = estado_final.get("endereco")
    
    # Validação simples
    dados_validos = zap and end and len(str(zap)) > 6 and len(str(end)) > 5

    pix_code = None
    payment_id = None

    # --- CHECKOUT SEGURO ---
    if prod and plan and dados_validos:
        preco = 149.90 if "Whey" in prod else (99.90 if "Creatina" in prod else 49.90)
        if plan == "Mensal": preco = preco * 0.9 
        
        # TENTA GERAR PIX REAL
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
                
                # SÓ ACEITA SE O MERCADO PAGO DEVOLVER SUCESSO (201)
                if mp_res["status"] == 201:
                    pix_code = mp_res["response"]["point_of_interaction"]["transaction_data"]["qr_code"]
                    payment_id = str(mp_res["response"]["id"])
                    
                    # NOTIFICAÇÃO 1: LEAD QUENTE (Checkout Gerado)
                    msg_lead = (
                        f"🟡 *NOVO PEDIDO (Aguardando Pagamento)*\n"
                        f"👤 {user}\n"
                        f"🛒 {prod} ({plan})\n"
                        f"💰 R$ {preco:.2f}\n"
                        f"📱 `{zap}`\n"
                        f"📍 {end}"
                    )
                    enviar_telegram(msg_lead)
                else:
                    print("Erro MP:", mp_res) # Log interno
        except Exception as e:
            traceback.print_exc() # Log de erro real no console do Render

    # PROMPT
    instrucoes = ""
    if not prod: instrucoes += "FALTA: Produto (Cardápio: Whey, Creatina, Camiseta). "
    elif not plan: instrucoes += f"TEMOS: {prod}. FALTA: Plano (Único ou Mensal?). "
    elif not dados_validos: instrucoes += f"TEMOS: {prod} ({plan}). FALTA: WhatsApp e Endereço. "
    else: 
        if pix_code:
            instrucoes += "TUDO CERTO. PIX GERADO. Diga: 'Perfeito! Segue o PIX abaixo para finalizar.' "
        else:
            instrucoes += "ERRO NO PIX. Diga: 'Tive um erro técnico ao gerar o PIX. Tente novamente em instantes.' "

    prompt = f"""
    Você é MARS. Cliente: {user}. ESTADO: {instrucoes}
    REGRAS: 1. Aceite mudanças. 2. Max 15 palavras. 3. Se tem PIX, peça pagamento.
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
        "pix": pix_code,
        "payment_id": payment_id
    }

# --- ROTA DE STATUS (Site pergunta) ---
@app.get("/verificar_pagamento/{pid}")
async def verificar_pagamento(pid: str):
    if not MP_ACCESS_TOKEN: return {"status": "pending"}
    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
        res = sdk.payment().get(pid)
        return {"status": res["response"]["status"]}
    except: return {"status": "error"}

# --- ROTA DE WEBHOOK (Mercado Pago avisa aqui quando paga) ---
@app.post("/webhook")
async def webhook_mp(request: Request):
    try:
        data = await request.json()
        if data.get("type") == "payment":
            p_id = data["data"]["id"]
            sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
            info = sdk.payment().get(p_id)
            
            # NOTIFICAÇÃO 2: VENDA REALIZADA (Dinheiro na conta)
            if info["response"]["status"] == "approved":
                val = info["response"]["transaction_amount"]
                desc = info["response"]["description"]
                enviar_telegram(f"🟢 *VENDA APROVADA! (Despachar)*\n💰 Valor: R$ {val}\n📦 {desc}")
                
        return {"status": "ok"}
    except: return {"status": "error"}
    
@app.post("/salvar_lead")
async def lead(d: dict): return {"status": "ok"}
