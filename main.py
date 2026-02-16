import os, requests, mercadopago, json
from groq import Groq
from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

# Carrega variáveis de ambiente
try: from dotenv import load_dotenv; load_dotenv()
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
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
client = Groq(api_key=CHAVE_GROQ) if CHAVE_GROQ else None

class ChatInput(BaseModel):
    texto: str
    nome_usuario: str
    # Os campos de memória do frontend são ignorados propositalmente
    # agora confiamos apenas no Banco de Dados.
    produto_identificado: str = ""
    plano_identificado: str = ""
    contato_ok: bool = False

# --- FUNÇÕES DE MEMÓRIA (SUPABASE) ---
def db_get_session(user_id):
    """Lê o cérebro do cliente no banco"""
    if not SUPABASE_URL: return None
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        # Busca sessão existente
        r = requests.get(f"{SUPABASE_URL}/rest/v1/sessoes_venda?user_id=eq.{user_id}", headers=headers)
        dados = r.json()
        if len(dados) > 0: return dados[0]
        return None
    except: return None

def db_upsert_session(user_id, dados):
    """Salva/Atualiza o cérebro do cliente"""
    if not SUPABASE_URL: return
    headers = {
        "apikey": SUPABASE_KEY, 
        "Authorization": f"Bearer {SUPABASE_KEY}", 
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates" # Importante: Atualiza se já existir
    }
    dados['user_id'] = user_id
    try: requests.post(f"{SUPABASE_URL}/rest/v1/sessoes_venda", json=dados, headers=headers)
    except: pass

# --- LÓGICA DE INTELIGÊNCIA ---
def analisar_contexto(texto_novo, estado_atual):
    """
    Analisa o texto novo e mistura com o que já sabemos do banco.
    """
    # Se não tem estado anterior, cria um vazio
    novo_estado = estado_atual.copy() if estado_atual else {"produto": None, "plano": None, "whatsapp": None, "endereco": None}
    
    txt = texto_novo.lower()

    # 1. Detecta Produto (se o cliente mudar de ideia, atualiza)
    if "whey" in txt: novo_estado["produto"] = "Whey Protein Gold"
    elif "creatina" in txt: novo_estado["produto"] = "Creatina Pura"
    elif "camiseta" in txt: novo_estado["produto"] = "Camiseta Mars"

    # 2. Detecta Plano
    if "mensal" in txt or "assinatura" in txt: novo_estado["plano"] = "Mensal"
    elif "unico" in txt or "único" in txt: novo_estado["plano"] = "Único"

    # 3. Detecta Contato (Lógica simples: tem mais de 8 números)
    numeros = ''.join(filter(str.isdigit, txt))
    if len(numeros) >= 8 and "149" not in numeros: # Evita confundir com o preço
        novo_estado["whatsapp"] = "OK"

    # 4. Detecta Endereço
    if len(txt) > 10 and ("rua" in txt or "av" in txt or "bairro" in txt or "entrega" in txt):
        novo_estado["endereco"] = "OK"
        
    return novo_estado

@app.post("/chat")
async def chat_endpoint(data: ChatInput):
    user = data.nome_usuario
    
    # 1. LER MEMÓRIA (O Robô "lembra" quem é você)
    sessao_banco = db_get_session(user)
    
    # 2. ATUALIZAR MEMÓRIA (Com o que você disse agora)
    estado_final = analisar_contexto(data.texto, sessao_banco)
    
    # 3. SALVAR MEMÓRIA (Para não esquecer na próxima mensagem)
    db_upsert_session(user, estado_final)

    # 4. DECIDIR RESPOSTA COM BASE NO ESTADO
    prod = estado_final.get("produto")
    plan = estado_final.get("plano")
    zap = estado_final.get("whatsapp")
    end = estado_final.get("endereco")

    # Verifica se temos tudo para o PIX
    tem_tudo = prod and plan and (zap or end)
    
    pix_code = None
    if tem_tudo:
        # Lógica de PIX
        preco = 149.90 if "Whey" in prod else (99.90 if "Creatina" in prod else 49.90)
        if plan == "Mensal": preco = preco * 0.9 # 10% desconto
        
        # Simulação Mercado Pago (para não travar se sua chave der erro)
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
        except: pass

    # 5. INSTRUIR A IA (O "Cérebro" falando o que falta)
    instrucoes = ""
    if not prod: instrucoes += "FALTA: Produto. Apresente o cardápio: Whey (149), Creatina (99), Camiseta (49). "
    elif not plan: instrucoes += f"TEMOS: {prod}. FALTA: Plano (Único ou Mensal?). "
    elif not (zap or end): instrucoes += f"TEMOS: {prod} no plano {plan}. FALTA: WhatsApp e Endereço para entrega. "
    else: instrucoes += "TEMOS TUDO. Avise que o PIX foi gerado abaixo e agradeça. "

    prompt = f"""
    Você é MARS, assistente de vendas da loja de suplementos.
    Cliente: {user}.
    
    ESTADO DA VENDA (MEMÓRIA DO BANCO DE DADOS):
    {instrucoes}
    
    Seu trabalho é APENAS pedir o que está faltando na lista acima.
    Se já tivermos tudo, finalize a venda.
    Seja curto e amigável.
    """

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": data.texto}],
            temperature=0.1
        )
        resposta_texto = resp.choices[0].message.content
    except: resposta_texto = "Conexão instável. Pode repetir?"

    img_url = None
    if prod and "Whey" in prod: img_url = "https://m.media-amazon.com/images/I/41sdCLWi29L._AC_SY300_SX300_QL70_ML2_.jpg"
    elif prod and "Creatina" in prod: img_url = "https://http2.mlstatic.com/D_NQ_NP_2X_942122-MLA99923169249_112025-F.webp"

    return {
        "respostas": [r.strip() for r in resposta_texto.split('---') if r.strip()],
        "imagem": img_url,
        "pix": pix_code
    }

# Rotas auxiliares para manter compatibilidade
@app.get("/verificar_pagamento/{pid}")
async def ver(pid): return {"status": "pending"}
@app.post("/salvar_lead")
async def lead(d: dict): return {"status": "ok"}
