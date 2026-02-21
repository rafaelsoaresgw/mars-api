import os, requests, mercadopago, json, re
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
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                      json={ "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown" })
    except: pass

# --- BANCO DE DADOS ---
def db_get_session(user_id):
    uid = user_id.lower().strip()
    if not SUPABASE_URL: return None
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/sessoes_venda?user_id=eq.{uid}&order=id.desc&limit=1", headers=headers)
        dados = r.json()
        return dados[0] if len(dados) > 0 else None
    except: return None

def db_upsert_session(user_id, dados):
    uid = user_id.lower().strip()
    if not SUPABASE_URL: return
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}
    dados['user_id'] = uid
    try: requests.post(f"{SUPABASE_URL}/rest/v1/sessoes_venda", json=dados, headers=headers)
    except: pass

def db_reset_session(user_id):
    uid = user_id.lower().strip()
    if not SUPABASE_URL: return
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try: requests.delete(f"{SUPABASE_URL}/rest/v1/sessoes_venda?user_id=eq.{uid}", headers=headers)
    except: pass

# --- CÉREBRO PROFISSIONAL: EXTRAÇÃO SIMULTÂNEA (O SEGREDO ESTÁ AQUI) ---
def analisar_contexto(texto_novo, estado_atual):
    novo_estado = {
        "produto": estado_atual.get("produto") if estado_atual else None,
        "plano": estado_atual.get("plano") if estado_atual else None,
        "whatsapp": estado_atual.get("whatsapp") if estado_atual else None,
        "endereco": estado_atual.get("endereco") if estado_atual else None
    }

    # Limpeza de Segurança: Garante que um telefone corrompido do banco antigo seja deletado
    if novo_estado["whatsapp"]:
        zap_valida = re.sub(r'\D', '', str(novo_estado["whatsapp"]))
        if len(zap_valida) < 10 or len(zap_valida) > 11:
            novo_estado["whatsapp"] = None

    txt_lower = texto_novo.lower().strip()
    texto_restante = texto_novo # Variável dinâmica que será reduzida durante o processo

    # 1. PRODUTO
    if not novo_estado["produto"]:
        if "whey" in txt_lower: novo_estado["produto"] = "Whey Protein Gold"
        elif "creatina" in txt_lower: novo_estado["produto"] = "Creatina Pura"
        elif "camiseta" in txt_lower: novo_estado["produto"] = "Camiseta Mars"

    # 2. PLANO
    if not novo_estado["plano"]:
        if "mensal" in txt_lower or "assinatura" in txt_lower: novo_estado["plano"] = "Mensal"
        elif "unico" in txt_lower or "único" in txt_lower: novo_estado["plano"] = "Único"

    # 3. EXTRAÇÃO DO WHATSAPP E SUBTRAÇÃO
    if not novo_estado["whatsapp"]:
        # Regex focado em achar APENAS formato de celular/telefone do Brasil 
        # Aceita formatos: 19971683530, (19) 99716-8353, 19 99716 8353
        padrao_telefone = r'\(?\d{2}\)?[\s-]?\d{4,5}[\s-]?\d{4}'
        match = re.search(padrao_telefone, texto_restante)
        
        if match:
            # Extrai o telefone limpo
            novo_estado["whatsapp"] = re.sub(r'\D', '', match.group())
            # APAGA o telefone da string original, assim o endereço fica limpinho e isolado
            texto_restante = texto_restante.replace(match.group(), "").strip()
        else:
            # Fallback para string colada direta (ex: se o cara digitar zap e rua sem nenhum espaço)
            blocos = re.findall(r'\b\d{10,11}\b', re.sub(r'[^\w\s]', '', texto_restante))
            if blocos:
                novo_estado["whatsapp"] = blocos[0]
                texto_restante = texto_restante.replace(blocos[0], "").strip()

    # 4. EXTRAÇÃO DO ENDEREÇO (A PARTIR DA STRING SUBTRAÍDA)
    if not novo_estado["endereco"]:
        # Removemos inícios comuns que as pessoas digitam para o bot ler só o endereço
        txt_limpo = re.sub(r'(?i)^(meu whatsapp|whatsapp|meu telefone|telefone|endere[cç]o é|endere[cç]o|cep)[:\-\s]*', '', texto_restante).strip()
        
        # Só é válido como endereço se tiver mais de 8 letras e não for os comandos de ação
        if len(txt_limpo) > 8 and not any(cmd in txt_lower for cmd in ["reiniciar", "reset"]):
            # Proteção final: impede que o sistema grave "quero assinatura mensal" como se fosse endereço
            if txt_limpo.lower() not in ["quero whey protein", "quero assinatura mensal", "whey", "creatina"]:
                novo_estado["endereco"] = txt_limpo

    return novo_estado

@app.post("/chat")
async def chat_endpoint(data: ChatInput):
    user = data.nome_usuario
    txt_low = data.texto.lower()

    # Essa é a ÚNICA forma de limpar completamente o banco de dados via API do bot
    if "reiniciar" in txt_low or "reset" in txt_low:
        db_reset_session(user)
        return {"respostas": ["Tudo limpo! Vamos recomeçar. Qual produto você deseja?"], "imagem": None, "pix": None}

    sessao_banco = db_get_session(user)
    estado_final = analisar_contexto(data.texto, sessao_banco)
    db_upsert_session(user, estado_final)

    prod = estado_final.get("produto")
    plan = estado_final.get("plano")
    zap = estado_final.get("whatsapp")
    end = estado_final.get("endereco")
    
    pix_code = None
    payment_id = None

    # Lógica de Checkout (Gera o PIX instantaneamente se tiver os 4 campos)
    if prod and plan and zap and end:
        preco = 149.90 if "Whey" in prod else (99.90 if "Creatina" in prod else 49.90)
        if plan == "Mensal": preco = preco * 0.9 
        
        pix_code = "00020126580014BR.GOV.BCB.PIX0136123e4567-e89b-12d3-a456-426614174000520400005303986540410.005802BR5913MARS AI6008BRASILIA62070503***6304ABCD"

        try:
            if MP_ACCESS_TOKEN:
                sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
                res = sdk.payment().create({
                    "transaction_amount": round(preco, 2),
                    "description": f"{prod} ({plan})",
                    "payment_method_id": "pix",
                    "payer": {"email": "cliente@mars.com", "first_name": user}
                })
                if res["status"] == 201:
                    pix_code = res["response"]["point_of_interaction"]["transaction_data"]["qr_code"]
                    payment_id = str(res["response"]["id"])
                    
                    enviar_telegram(f"🟢 *NOVO PEDIDO:*\n👤 *Cliente:* {user.upper()}\n🛒 *Produto:* {prod} ({plan})\n💰 *Valor:* R$ {preco:.2f}\n📱 *Zap:* {zap}\n📍 *Endereço:* {end}")
        except: pass

    # --- INTELIGÊNCIA ARTIFICIAL ADAPTATIVA ---
    if pix_code:
        resposta_texto = "Perfeito! Todas as informações foram processadas com sucesso. O seu código PIX foi gerado abaixo. É só copiar e pagar para finalizarmos seu pedido!"
    else:
        instrucao = ""
        # Instruções adaptativas com base naquilo que faltar extrair
        if not prod: instrucao = "Pergunte gentilmente qual produto o cliente quer: Whey, Creatina ou Camiseta."
        elif not plan: instrucao = "Pergunte se o plano será Único ou Mensal."
        elif not zap and not end: instrucao = "Alerte que falta pouco! Peça o número do WhatsApp com DDD E também o endereço completo de entrega."
        elif not zap: instrucao = "Temos o endereço, mas faltou o WhatsApp. Peça APENAS o número do WhatsApp com DDD."
        elif not end: instrucao = f"Temos o WhatsApp ({zap}), mas falta o Endereço. Peça APENAS o endereço completo para entrega."

        prompt = f"""
        Você é MARS, um robô de vendas focado em conversão.
        MISSÃO ATUAL: {instrucao}
        
        REGRAS ABSOLUTAS:
        1. Siga EXATAMENTE a missão atual.
        2. Seja empático, conversacional e MUITO breve (máximo 20 palavras).
        """
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": data.texto}],
                temperature=0.0
            )
            resposta_texto = resp.choices[0].message.content
        except: resposta_texto = "Conexão instável. Mars reconectando..."

    img_url = None
    if prod and "Whey" in prod: img_url = "https://m.media-amazon.com/images/I/41sdCLWi29L._AC_SY300_SX300_QL70_ML2_.jpg"
    elif prod and "Creatina" in prod: img_url = "https://http2.mlstatic.com/D_NQ_NP_2X_942122-MLA99923169249_112025-F.webp"

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
                enviar_telegram(f"🟢 *PIX PAGO COM SUCESSO!* 💰 R$ {val}")
        return {"status": "ok"}
    except: return {"status": "error"}
    
@app.post("/salvar_lead")
async def lead(d: dict): return {"status": "ok"}
