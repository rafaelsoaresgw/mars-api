import os, requests
import mercadopago
import traceback
from groq import Groq
from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

# Carrega chaves do .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except: pass

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURAÇÕES ---
CHAVE_GROQ = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN") 
ZAP_ADMIN = "5519999999999" # SEU NUMERO AQUI (PARA O BOTÃO DE AJUDA)

client = Groq(api_key=CHAVE_GROQ) if CHAVE_GROQ else None

def salvar_no_supabase(tabela: str, dados: dict):
    if not SUPABASE_URL or not SUPABASE_KEY: return
    try:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal"}
        url = f"{SUPABASE_URL}/rest/v1/{tabela}"
        requests.post(url, json=dados, headers=headers)
    except Exception as e: print(f"Erro Supabase: {e}")

def enviar_telegram(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True})
        except: pass

class ChatRequest(BaseModel):
    texto: str
    nome_usuario: str = ""
    produto_identificado: str = ""
    preco_base: float = 0.0
    frete: float = 0.0

# --- GERADOR DE PIX RETORNANDO ID ---
def gerar_pix_mercadopago(valor, descricao, email):
    if not MP_ACCESS_TOKEN: return None, None
    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
        payment_data = {
            "transaction_amount": round(float(valor), 2),
            "description": descricao,
            "payment_method_id": "pix",
            "payer": {"email": "cliente@mars.com", "first_name": "Atleta", "last_name": "Mars"},
            "notification_url": "https://mars-api1.onrender.com/webhook" # Seu link do render
        }
        payment_response = sdk.payment().create(payment_data)
        payment = payment_response["response"]
        
        pix_copia = payment["point_of_interaction"]["transaction_data"]["qr_code"]
        payment_id = payment["id"] # Captura o ID para monitorar
        
        return pix_copia, str(payment_id)
    except:
        traceback.print_exc()
        return None, None

@app.post("/chat")
async def chat(data: ChatRequest):
    user = data.nome_usuario or "Atleta"
    txt_low = data.texto.lower()
    
    # --- 1. TRANSBORDO (HUMANO) ---
    if "humano" in txt_low or "ajuda" in txt_low or "atendente" in txt_low:
        enviar_telegram(f"🚨 *ALERTA:* {user} pediu ajuda humana no chat!")
        return {
            "respostas": [
                f"Entendi, {user}. Às vezes a tecnologia tem limites.",
                "---",
                "Chame o Rafael no WhatsApp Pessoal clicando abaixo:",
                f"👉 [Falar com Rafael](https://wa.me/{ZAP_ADMIN})"
            ],
            "imagem": None, "pix": None
        }

    # --- 2. LÓGICA DE VENDAS ---
    tem_prod = data.produto_identificado != ""
    tem_plano = "plano_ok=sim" in txt_low
    tem_whats = "whatsapp_ok=sim" in txt_low
    tem_local = "local_ok=sim" in txt_low 
    
    pix_code = None
    payment_id = None
    img_url = None
    
    p_id = data.produto_identificado.lower()
    ref_pix = "SUPLEMENTO"
    if "creatina" in p_id: img_url, ref_pix = "https://m.media-amazon.com/images/I/71Hfi+W5eeL.jpg", "CREATINA"
    elif "whey" in p_id: img_url, ref_pix = "https://a-static.mlcdn.com.br/undefinedxundefined/whey-growth-concentrado-80-protein-supplements-1kg-sabores-growth-supplements/mindabraatzcosmeticos/663d6ede987211eea42f4201ac185040/15a3a5dcfb8da0785e2f5b79ebd4b4a4.jpeg", "WHEY"

    if tem_prod and tem_plano and tem_whats and tem_local:
        desconto = 0.90 if ("assinatura" in txt_low or "mensal" in txt_low) else 0.95
        valor = (data.preco_base * desconto) + data.frete
        
        pix_code, payment_id = gerar_pix_mercadopago(valor, f"{ref_pix}-{user}", "email@teste.com")
        
        if pix_code:
            resposta_final = [f"✅ Tudo certo, {user}! Gerando seu pedido...", "---", "Aqui está o PIX com desconto aplicado:"]
            salvar_no_supabase("vendas", {"nome_cliente": user, "produto": ref_pix, "valor": valor, "status": "AGUARDANDO_PGTO", "payment_id": payment_id})
            return {"respostas": resposta_final, "imagem": img_url, "pix": pix_code, "payment_id": payment_id}

    # --- 3. MODO IA ---
    faltam = []
    if not tem_prod: faltam.append("qual produto deseja")
    if not tem_plano: faltam.append("o plano (Único ou Mensal)")
    if not tem_whats: faltam.append("o WhatsApp")
    if not tem_local: faltam.append("o Endereço")

    instrucao = f"""
    Você é Mars. Venda curta e direta.
    CARDÁPIO: Creatina (97), Whey (149).
    Peça APENAS o que falta: {', '.join(faltam)}.
    """
    try:
        completion = client.chat.completions.create(messages=[{"role": "system", "content": instrucao}, {"role": "user", "content": data.texto}], model="llama-3.3-70b-versatile", temperature=0.1)
        return {"respostas": [m.strip() for m in completion.choices[0].message.content.split('---') if m.strip()], "imagem": img_url}
    except: return {"respostas": ["Reconectando..."]}

@app.post("/salvar_lead")
async def salvar_lead(data: dict):
    enviar_telegram(f"🔥 *LEAD:* {data.get('nome')}\n📱 {data.get('telefone')}\n🛒 {data.get('produto')}")
    salvar_no_supabase("leads", data)
    return {"status": "ok"}

# --- NOVAS ROTAS DE PAGAMENTO ---

# 1. O site pergunta aqui se já pagou
@app.get("/verificar_pagamento/{pid}")
async def verificar_pagamento(pid: str):
    if not MP_ACCESS_TOKEN: return {"status": "pending"}
    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
        res = sdk.payment().get(pid)
        status = res["response"]["status"] # approved, pending, rejected
        return {"status": status}
    except:
        return {"status": "error"}

# 2. O Mercado Pago avisa aqui (Webhook)
@app.post("/webhook")
async def webhook_mp(request: Request):
    try:
        data = await request.json()
        if data.get("type") == "payment":
            p_id = data["data"]["id"]
            sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
            payment_info = sdk.payment().get(p_id)
            status = payment_info["response"]["status"]
            
            if status == "approved":
                valor = payment_info["response"]["transaction_amount"]
                enviar_telegram(f"💰 *PAGAMENTO APROVADO!* \nID: {p_id}\nValor: R$ {valor}")
                
        return {"status": "ok"}
    except:
        return {"status": "error"}
