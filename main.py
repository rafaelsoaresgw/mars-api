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

client = Groq(api_key=CHAVE_GROQ) if CHAVE_GROQ else None

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
        print(f"Erro Supabase: {e}")

class ChatRequest(BaseModel):
    texto: str
    nome_usuario: str = ""
    produto_identificado: str = ""
    preco_base: float = 0.0
    frete: float = 0.0

# --- PIX MERCADO PAGO ROBUSTO ---
def gerar_pix_mercadopago(valor, descricao, email):
    print(f"Tentando gerar PIX de R$ {valor}...") # Log no Render
    
    if not MP_ACCESS_TOKEN:
        print("❌ ERRO CRÍTICO: Token MP não encontrado nas Variáveis!")
        return None
        
    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
        
        # Garante email válido (O MP rejeita emails estranhos)
        email_final = email if "@" in email else "cliente_mars@gmail.com"
        
        payment_data = {
            "transaction_amount": round(float(valor), 2),
            "description": descricao,
            "payment_method_id": "pix",
            "payer": {
                "email": email_final,
                "first_name": "Atleta",
                "last_name": "Mars"
            },
            "installments": 1
        }
        
        print(f"Enviando dados pro MP: {payment_data}") # Log
        
        payment_response = sdk.payment().create(payment_data)
        payment = payment_response["response"]
        
        # Verifica se deu erro na API
        if "status" in payment and payment["status"] == 400:
            print(f"❌ O Mercado Pago rejeitou: {payment}")
            return None

        pix_copy_paste = payment["point_of_interaction"]["transaction_data"]["qr_code"]
        print("✅ PIX GERADO COM SUCESSO!")
        return pix_copy_paste

    except Exception as e:
        print("❌ ERRO NO PYTHON DO MP:")
        traceback.print_exc() # Imprime o erro detalhado
        return None

@app.post("/chat")
async def chat(data: ChatRequest):
    user = data.nome_usuario or "Atleta"
    txt_low = data.texto.lower()
    
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

    # --- MODO VENDA ---
    if tem_prod and tem_plano and tem_whats and tem_local:
        desconto = 0.90 if ("assinatura" in txt_low or "mensal" in txt_low) else 0.95
        plano_nome = "Mensal (10% OFF)" if desconto == 0.90 else "Único (5% OFF)"
        valor = (data.preco_base * desconto) + data.frete
        
        # Tenta gerar
        pix_code = gerar_pix_mercadopago(valor, f"{ref_pix}-{user}", "cliente_comprador@test.com")
        
        # Se falhar, avisa no chat em vez de mandar código quebrado
        if not pix_code:
            resposta_final = [
                "⚠️ Ops! Tive um erro técnico ao gerar o PIX.",
                "Já avisei o Rafael. Tente novamente em alguns minutos ou me chame no WhatsApp."
            ]
        else:
            resposta_final = [
                f"✅ Tudo certo, {user}!",
                f"Produto: {data.produto_identificado}",
                f"Plano: {plano_nome}",
                f"Total c/ frete: R$ {valor:.2f}",
                "---",
                "Gerei seu PIX oficial abaixo:"
            ]
            
            salvar_no_supabase("vendas", {
                "nome_cliente": user,
                "produto": ref_pix,
                "valor": valor,
                "status": "AGUARDANDO_PGTO"
            })

        return {"respostas": resposta_final, "imagem": img_url, "pix": pix_code}

    # --- MODO IA ---
    faltam = []
    if not tem_prod: faltam.append("qual produto deseja")
    if not tem_plano: faltam.append("o plano (Único ou Mensal)")
    if not tem_whats: faltam.append("o WhatsApp")
    if not tem_local: faltam.append("o Endereço de entrega")

    instrucao = f"""
    Você é Mars, vendedor da Rafael Suplementos.
    CARDÁPIO:
    1. Creatina (R$ 97,00)
    2. Whey Protein (R$ 149,00)
    3. BCAA (R$ 79,00)
    4. Psychotic (R$ 189,00)
    
    PLANOS: Compra Única (5% OFF) | Assinatura Mensal (10% OFF)
    
    ESTADO:
    - Produto: {data.produto_identificado if tem_prod else 'PENDENTE'}
    - Plano: {'OK' if tem_plano else 'PENDENTE'}
    - WhatsApp: {'OK' if tem_whats else 'PENDENTE'}
    - Endereço: {'OK' if tem_local else 'PENDENTE'}
    
    Peça APENAS o que falta: {', '.join(faltam)}.
    """

    try:
        completion = client.chat.completions.create(
            messages=[{"role": "system", "content": instrucao}, {"role": "user", "content": data.texto}],
            model="llama-3.3-70b-versatile",
            temperature=0.1
        )
        resposta_texto = completion.choices[0].message.content
        return {"respostas": [m.strip() for m in resposta_texto.split('---') if m.strip()], "imagem": img_url, "pix": None}
    except:
        return {"respostas": ["Mars reconectando..."]}

@app.post("/salvar_lead")
async def salvar_lead(data: dict):
    nome = data.get('nome', 'Cliente')
    fone = data.get('telefone', '')
    raw_produto = data.get('produto', '')
    interesse = raw_produto.split(" | ")[0] if " | " in raw_produto else raw_produto
    
    link_zap = f"https://wa.me/55{fone.replace(' ','').replace('-','')}" if fone else ""
    msg = f"🔥 *NOVO LEAD MARS*\n👤 *Cliente:* {nome}\n📱 *Zap:* [{fone}]({link_zap})\n🛒 *Pedido:* {interesse}"

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True})
        except: pass
    
    salvar_no_supabase("leads", {"nome": nome, "telefone": fone, "info_pedido": raw_produto})
    return {"status": "ok"}
