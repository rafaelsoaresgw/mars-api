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

# --- TELEGRAM (COM DEBUG NO LOG) ---
def enviar_telegram(msg):
    print(f"Tentando notificar Telegram: {msg}") # Log no Render
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ ERRO: TELEGRAM_TOKEN ou CHAT_ID não configurados no Render.")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = { "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown" }
        r = requests.post(url, json=payload)
        print(f"Status Telegram: {r.status_code}") 
    except Exception as e:
        print(f"❌ ERRO TELEGRAM: {e}")

# --- BANCO DE DADOS (CÉREBRO) ---
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
    if len(numeros) >= 8 and "149" not in numeros: novo_estado["whatsapp"] = "OK"

    if len(txt) > 10 and ("rua" in txt or "av" in txt or "bairro" in txt or "entrega" in txt):
        novo_estado["endereco"] = "OK"
        
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
    
    # Lógica de Checkout
    pix_code = None
    if prod and plan and (zap or end):
        preco = 149.90 if "Whey" in prod else (99.90 if "Creatina" in prod else 49.90)
        if plan == "Mensal": preco = preco * 0.9 
        
        # 1. Gera código padrão (Fallback)
        pix_code = "00020126580014BR.GOV.BCB.PIX0136123e4567-e89b-12d3-a456-426614174000520400005303986540410.005802BR5913MARS AI6008BRASILIA62070503***6304ABCD"
        
        # 2. Tenta gerar código Real (Mercado Pago)
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
        except: pass

        # 3. NOTIFICA O TELEGRAM (AGORA FORA DO TRY/EXCEPT) - VAI FUNCIONAR SEMPRE
        enviar_telegram(f"🚀 *CHECKOUT GERADO!*\n👤 {user}\n🛒 {prod}\n📄 {plan}\n💰 R$ {preco:.2f}")

    # IA Prompt
    instrucoes = ""
    if not prod: instrucoes += "FALTA: Produto (Cardápio: Whey, Creatina, Camiseta). "
    elif not plan: instrucoes += f"TEMOS: {prod}. FALTA: Plano (Único ou Mensal?). "
    elif not (zap or end): instrucoes += f"TEMOS: {prod} ({plan}). FALTA: WhatsApp e Endereço. "
    else: instrucoes += "TEMOS TUDO. Avise que o PIX foi gerado abaixo. "

    prompt = f"Você é MARS. Cliente: {user}. ESTADO: {instrucoes}. OBJETIVO: Pedir o que falta. Se tem tudo, finalize."
    
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

# Rota de teste
@app.get("/teste_telegram")
async def teste_telegram():
    enviar_telegram("🔔 TESTE DE SISTEMA MARS: Se recebeu isso, está funcionando!")
    return {"status": "Comando de envio disparado. Verifique o Telegram."}
