import os, requests
from groq import Groq
from fastapi import FastAPI
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
CHAVE_PIX_DESTINO = "49918768851"
CHAVE_GROQ = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

client = Groq(api_key=CHAVE_GROQ) if CHAVE_GROQ else None

# --- FUNÇÃO AUXILIAR SUPABASE ---
def salvar_no_supabase(tabela: str, dados: dict):
    if not SUPABASE_URL or not SUPABASE_KEY: return
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        url = f"{SUPABASE_URL}/rest/v1/{tabela}"
        requests.post(url, json=dados, headers=headers)
    except Exception as e:
        print(f"Erro ao salvar no banco: {e}")

class ChatRequest(BaseModel):
    texto: str
    nome_usuario: str = ""
    produto_identificado: str = ""
    preco_base: float = 0.0
    frete: float = 0.0

def gerar_pix_br_code(valor, produto_ref):
    valor_str = f"{valor:.2f}"
    payload = "00020126" + str(22 + len(CHAVE_PIX_DESTINO)) + "0014BR.GOV.BCB.PIX01" + str(len(CHAVE_PIX_DESTINO)) + CHAVE_PIX_DESTINO
    payload += "52040000530398654" + f"{len(valor_str):02}" + valor_str + "5802BR5915RAFAEL SUPLEMEN6009RIO CLARO"
    payload += "62" + str(len(produto_ref) + 7).zfill(2) + "05" + str(len(produto_ref)).zfill(2) + produto_ref + "6304"
    
    def crc16(data):
        crc = 0xFFFF
        for char in data:
            crc ^= ord(char) << 8
            for _ in range(8):
                if crc & 0x8000: crc = (crc << 1) ^ 0x1021
                else: crc <<= 1
                crc &= 0xFFFF
        return f"{crc:04X}"
    return payload + crc16(payload)

@app.post("/chat")
async def chat(data: ChatRequest):
    user = data.nome_usuario or "Atleta"
    txt_low = data.texto.lower()
    
    # --- CHECAGEM DE ESTADO ---
    tem_prod = data.produto_identificado != ""
    tem_plano = "plano_ok=sim" in txt_low
    tem_whats = "whatsapp_ok=sim" in txt_low
    tem_local = "local_ok=sim" in txt_low 
    
    pix_code = None
    img_url = None
    
    p_id = data.produto_identificado.lower()
    ref_pix = "SUPLEMENTO"
    if "creatina" in p_id: img_url, ref_pix = "https://m.media-amazon.com/images/I/71Hfi+W5eeL.jpg", "CREATINA"
    elif "whey" in p_id: img_url, ref_pix = "https://a-static.mlcdn.com.br/undefinedxundefined/whey-growth-concentrado-80-protein-supplements-1kg-sabores-growth-supplements/mindabraatzcosmeticos/663d6ede987211eea42f4201ac185040/15a3a5dcfb8da0785e2f5b79ebd4b4a4.jpeg", "WHEYGRWTH"

    # --- MODO AUTOMÁTICO (Prioridade Máxima) ---
    if tem_prod and tem_plano and tem_whats and tem_local:
        desconto = 0.90 if ("assinatura" in txt_low or "mensal" in txt_low) else 0.95
        plano_nome = "Mensal (10% OFF)" if desconto == 0.90 else "Único (5% OFF)"
        valor = (data.preco_base * desconto) + data.frete
        pix_code = gerar_pix_br_code(valor, ref_pix)
        
        resposta_final = [
            f"✅ Tudo certo, {user}!",
            f"Produto: {data.produto_identificado}",
            f"Plano: {plano_nome}",
            f"Total c/ frete: R$ {valor:.2f}",
            "---",
            "Gerei seu PIX abaixo para finalizar:"
        ]

        salvar_no_supabase("vendas", {
            "nome_cliente": user,
            "produto": ref_pix,
            "valor": valor,
            "status": "VENDA_AUTOMATICA"
        })

        return {"respostas": resposta_final, "imagem": img_url, "pix": pix_code}

    # --- MODO IA (Agora com o CARDÁPIO) ---
    faltam = []
    if not tem_prod: faltam.append("qual produto deseja")
    if not tem_plano: faltam.append("o plano (Único ou Mensal)")
    if not tem_whats: faltam.append("o WhatsApp")
    if not tem_local: faltam.append("o Endereço de entrega")

    instrucao = f"""
    Você é Mars, vendedor da Rafael Suplementos. Seja persuasivo e curto.
    
    NOSSO CARDÁPIO (Ofereça isso se o cliente perguntar):
    1. Creatina (R$ 97,00) - Essencial para força.
    2. Whey Protein (R$ 149,00) - Recuperação muscular.
    3. BCAA (R$ 79,00) - Energia intra-treino.
    4. Psychotic (R$ 189,00) - Pré-treino potente.
    
    PLANOS DE DESCONTO:
    - Compra Única (5% OFF)
    - Assinatura Mensal (10% OFF)
    
    ESTADO ATUAL DA VENDA:
    - Produto: {data.produto_identificado if tem_prod else 'PENDENTE'}
    - Plano: {'OK' if tem_plano else 'PENDENTE'}
    - WhatsApp: {'OK' if tem_whats else 'PENDENTE'}
    - Endereço: {'OK' if tem_local else 'PENDENTE'}
    
    SUA MISSÃO: 
    1. Se o cliente perguntar o que vendemos, mostre o cardápio.
    2. Se ele já escolheu, peça APENAS o que falta da lista: {', '.join(faltam)}.
    3. Não invente produtos que não estão na lista.
    """

    try:
        completion = client.chat.completions.create(
            messages=[{"role": "system", "content": instrucao}, {"role": "user", "content": data.texto}],
            model="llama-3.3-70b-versatile",
            temperature=0.1
        )
        resposta_texto = completion.choices[0].message.content
        return {"respostas": [m.strip() for m in resposta_texto.split('---') if m.strip()], "imagem": img_url, "pix": None}
    except Exception as e:
        return {"respostas": ["Mars reconectando..."]}

@app.post("/salvar_lead")
async def salvar_lead(data: dict):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        msg = f"🚀 LEAD NOVO:\n👤 {data.get('nome')}\n📱 {data.get('telefone')}\n📦 {data.get('produto')}"
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        except: pass
    
    salvar_no_supabase("leads", {
        "nome": data.get('nome'), 
        "telefone": data.get('telefone'), 
        "info_pedido": data.get('produto')
    })
        
    return {"status": "ok"}