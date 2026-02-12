import os, requests
import mercadopago
import traceback
from groq import Groq
from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

# Tenta carregar variáveis locais (apenas para teste local)
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

# --- CONFIGURAÇÕES E CHAVES ---
CHAVE_GROQ = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN") 
ZAP_ADMIN = "5519999999999" # ⚠️ COLOQUE SEU WHATSAPP AQUI PARA O BOTÃO DE AJUDA

client = Groq(api_key=CHAVE_GROQ) if CHAVE_GROQ else None

# --- FUNÇÃO QUE CONECTA NO SUPABASE (BANCO DE DADOS) ---
def supabase_request(endpoint, method="POST", data=None):
    if not SUPABASE_URL or not SUPABASE_KEY: return None
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    try:
        if method == "POST":
            response = requests.post(url, json=data, headers=headers)
        elif method == "GET":
            response = requests.get(url, headers=headers)
        return response.json() if method == "GET" else response
    except: return None

def buscar_produtos_db():
    # Vai no Supabase e pega a lista de produtos atualizada
    dados = supabase_request("produtos?select=*", method="GET")
    if isinstance(dados, list):
        return dados
    return [] # Retorna lista vazia se der erro

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

# --- GERADOR DE PIX (MERCADO PAGO) ---
def gerar_pix_mercadopago(valor, descricao, email):
    if not MP_ACCESS_TOKEN: return None, None
    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
        payment_data = {
            "transaction_amount": round(float(valor), 2),
            "description": descricao,
            "payment_method_id": "pix",
            "payer": {"email": "cliente@mars.com", "first_name": "Atleta", "last_name": "Mars"},
            "notification_url": "https://mars-api1.onrender.com/webhook"
        }
        payment_response = sdk.payment().create(payment_data)
        payment = payment_response["response"]
        
        pix_copia = payment["point_of_interaction"]["transaction_data"]["qr_code"]
        payment_id = payment["id"]
        return pix_copia, str(payment_id)
    except:
        traceback.print_exc()
        return None, None

@app.post("/chat")
async def chat(data: ChatRequest):
    user = data.nome_usuario or "Atleta"
    txt_low = data.texto.lower()
    
    # 1. TRANSBORDO HUMANO (Botão de Pânico)
    if "humano" in txt_low or "ajuda" in txt_low or "atendente" in txt_low:
        enviar_telegram(f"🚨 *ALERTA:* {user} solicitou ajuda humana no chat!")
        return {
            "respostas": [
                f"Entendido, {user}. Estou acionando o suporte humano.",
                "---",
                "Clique abaixo para falar direto com o Rafael:",
                f"👉 [Chamar no WhatsApp](https://wa.me/{ZAP_ADMIN})"
            ],
            "imagem": None, "pix": None
        }

    # 2. CARREGA PRODUTOS DO BANCO (Dinâmico)
    produtos_disponiveis = buscar_produtos_db()
    
    produto_selecionado = None
    
    # Tenta achar o produto pelo botão clicado (Frontend)
    if data.produto_identificado:
        for p in produtos_disponiveis:
            if p['nome'] in data.produto_identificado: # Ex: "Creatina" in "Quero Creatina"
                produto_selecionado = p
                break
    
    # Se não achou pelo botão, tenta achar pelo texto digitado
    # Se não achou pelo botão, tenta achar pelo texto digitado
    if not produto_selecionado:
        for p in produtos_disponiveis:
            # --- CORREÇÃO (A Mágica acontece aqui) ---
            # O .get() tenta pegar 'gatilhos'. Se não existir, ele traz vazio e não dá erro.
            raw_gatilhos = p.get('gatilhos', '') 
            
            if raw_gatilhos: 
                lista_gatilhos = raw_gatilhos.split(',')
                for g in lista_gatilhos:
                    if g.strip() and g.strip() in txt_low:
                        produto_selecionado = p
                        break
            
            if produto_selecionado: break

    # Estado da Conversa
    tem_prod = produto_selecionado is not None
    tem_plano = "plano_ok=sim" in txt_low
    tem_whats = "whatsapp_ok=sim" in txt_low
    tem_local = "local_ok=sim" in txt_low 

    img_url = produto_selecionado['imagem_url'] if tem_prod else None

    # --- MODO VENDA (CHECKOUT) ---
    if tem_prod and tem_plano and tem_whats and tem_local:
        desconto = 0.90 if ("assinatura" in txt_low or "mensal" in txt_low) else 0.95
        valor_final = (produto_selecionado['preco'] * desconto) + data.frete
        
        pix_code, p_id = gerar_pix_mercadopago(valor_final, f"{produto_selecionado['nome']}-{user}", "email@teste.com")
        
        if pix_code:
            salvar_no_supabase("vendas", {
                "nome_cliente": user, 
                "produto": produto_selecionado['nome'], 
                "valor": valor_final, 
                "status": "AGUARDANDO_PGTO", 
                "payment_id": p_id
            })
            return {
                "respostas": [
                    f"✅ Tudo certo! {produto_selecionado['nome']} reservado.", 
                    "---", 
                    f"Valor com desconto: R$ {valor_final:.2f}. Segue o PIX:"
                ],
                "imagem": img_url, "pix": pix_code, "payment_id": p_id
            }

    # --- MODO IA (NEGOCIAÇÃO) ---
    # Cria o cardápio baseado no que tem no banco agora
    cardapio_txt = "\n".join([f"- {p['nome']} (R$ {p['preco']})" for p in produtos_disponiveis])
    
    faltam = []
    if not tem_prod: faltam.append("qual produto deseja")
    if not tem_plano: faltam.append("o plano (Único ou Mensal)")
    if not tem_whats: faltam.append("o WhatsApp")
    if not tem_local: faltam.append("o Endereço")

    instrucao = f"""
    Você é Mars. Vendedor direto e eficiente.
    
    CARDÁPIO ATUAL (Lido do Sistema):
    {cardapio_txt}
    
    ESTADO ATUAL:
    - Produto: {produto_selecionado['nome'] if tem_prod else 'PENDENTE'}
    
    Peça APENAS o que falta: {', '.join(faltam)}.
    Se o cliente perguntar preço, use EXATAMENTE os valores do cardápio acima.
    """
    
    try:
        completion = client.chat.completions.create(
            messages=[{"role": "system", "content": instrucao}, {"role": "user", "content": data.texto}],
            model="llama-3.3-70b-versatile",
            temperature=0.1
        )
        resposta_texto = completion.choices[0].message.content
        return {"respostas": [m.strip() for m in resposta_texto.split('---') if m.strip()], "imagem": img_url}
    except: 
        return {"respostas": ["Mars reconectando sistema..."]}

# --- ROTAS DE SUPORTE ---
@app.post("/salvar_lead")
async def salvar_lead(data: dict):
    enviar_telegram(f"🔥 *LEAD:* {data.get('nome')}\n📱 {data.get('telefone')}\n🛒 {data.get('produto')}")
    salvar_no_supabase("leads", data)
    return {"status": "ok"}

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
                enviar_telegram(f"💰 *VENDA CONFIRMADA!* R$ {val}")
        return {"status": "ok"}
    except: return {"status": "error"}

# Dashboard Simulado (Dados Reais viriam do Supabase no futuro)
@app.get("/dashboard_stats")
async def dashboard_stats():
    return {
        "faturamento_hoje": 1450.00,
        "leads_hoje": 12,
        "vendas_hoje": 5,
        "ultimos_pedidos": [{"cliente": "Teste", "produto": "Whey", "valor": "R$ 149", "status": "PAGO"}]
    }
