import os, requests, mercadopago, json
import traceback
from groq import Groq
from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

try: 
    from dotenv import load_dotenv
    load_dotenv()
except: 
    pass

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

# --- FALLBACK EM MEMÓRIA (caso Supabase não esteja disponível) ---
sessoes_memoria = {}

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
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = { "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown" }
        requests.post(url, json=payload)
    except: pass

# --- BANCO DE DADOS (SUPABASE) COM FALLBACK EM MEMÓRIA ---
def db_get_session(user_id):
    # Tenta buscar no Supabase primeiro
    if SUPABASE_URL and SUPABASE_KEY:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        try:
            r = requests.get(f"{SUPABASE_URL}/rest/v1/sessoes_venda?user_id=eq.{user_id}", headers=headers)
            if r.status_code == 200:
                dados = r.json()
                if len(dados) > 0:
                    print(f"✅ Supabase: sessão encontrada para {user_id}")
                    return dados[0]
                else:
                    print(f"⚠️ Supabase: nenhuma sessão para {user_id}")
            else:
                print(f"❌ Supabase: erro {r.status_code} ao buscar {user_id}")
        except Exception as e:
            print(f"❌ Supabase: exceção ao buscar {user_id}: {e}")
    
    # Fallback para memória
    print(f"💾 Usando fallback em memória para {user_id}")
    return sessoes_memoria.get(user_id, {})

def db_upsert_session(user_id, dados):
    # Tenta salvar no Supabase primeiro
    if SUPABASE_URL and SUPABASE_KEY:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", 
                   "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}
        dados['user_id'] = user_id
        try:
            r = requests.post(f"{SUPABASE_URL}/rest/v1/sessoes_venda", json=dados, headers=headers)
            if r.status_code in [200, 201, 204]:
                print(f"✅ Supabase: sessão salva para {user_id}")
            else:
                print(f"❌ Supabase: erro {r.status_code} ao salvar {user_id}")
        except Exception as e:
            print(f"❌ Supabase: exceção ao salvar {user_id}: {e}")
    
    # Sempre salva na memória também
    sessoes_memoria[user_id] = {**sessoes_memoria.get(user_id, {}), **dados}
    print(f"💾 Memória atualizada para {user_id}: {sessoes_memoria[user_id]}")

def db_reset_session(user_id):
    if SUPABASE_URL and SUPABASE_KEY:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        try:
            requests.delete(f"{SUPABASE_URL}/rest/v1/sessoes_venda?user_id=eq.{user_id}", headers=headers)
        except: pass
    # Remove da memória
    if user_id in sessoes_memoria:
        del sessoes_memoria[user_id]

# --- ROTAS DE STATUS E PEDIDOS ---
@app.get("/")
def root():
    return {
        "sistema": "MARS AI",
        "status": "online",
        "mensagem": "API rodando com sucesso! Acesse o frontend no Netlify."
    }

@app.get("/pedidos")
def listar_pedidos():
    if not SUPABASE_URL:
        return {"error": "Supabase não configurado"}
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/sessoes_venda?pix_gerado=eq.true", headers=headers)
        return r.json()
    except:
        return {"error": "Erro ao buscar pedidos"}

# --- CÉREBRO (LÓGICA) ---
def analisar_contexto(texto_novo, estado_atual):
    # Inicializa com o estado atual ou padrões
    novo_estado = estado_atual.copy() if estado_atual else {}
    defaults = {"produto": None, "plano": None, "whatsapp": None, "endereco": None, "pix_gerado": False}
    for k, v in defaults.items():
        if k not in novo_estado:
            novo_estado[k] = v

    txt = texto_novo.lower()

    # Detecção de produto
    if "whey" in txt:
        novo_estado["produto"] = "Whey Protein Gold"
    elif "creatina" in txt:
        novo_estado["produto"] = "Creatina Pura"
    elif "camiseta" in txt:
        novo_estado["produto"] = "Camiseta Mars"

    # Detecção de plano
    if "mensal" in txt or "assinatura" in txt:
        novo_estado["plano"] = "Mensal"
    elif "unico" in txt or "único" in txt or "avista" in txt or "à vista" in txt:
        novo_estado["plano"] = "Único"

    # Detecção de WhatsApp (números)
    numeros = ''.join(filter(str.isdigit, txt))
    if len(numeros) >= 10 and len(numeros) <= 11:
        novo_estado["whatsapp"] = numeros

    # Detecção de endereço
    palavras_chave_end = ["rua", "av", "avenida", "bairro", "casa", "apto", "bloco", "entrega", "número", "cep", "logradouro"]
    if len(txt) > 5 and any(p in txt for p in palavras_chave_end):
        novo_estado["endereco"] = texto_novo

    return novo_estado

@app.post("/chat")
async def chat_endpoint(data: ChatInput):
    user = data.nome_usuario
    txt_low = data.texto.lower().strip()

    # Comando de reset
    if "reiniciar" in txt_low or "reset" in txt_low:
        db_reset_session(user)
        return {"respostas": ["Beleza! Memória apagada. --- O que você manda hoje, atleta?"], "imagem": None, "pix": None}

    # Carrega estado atual (tenta Supabase, fallback memória)
    sessao_banco = db_get_session(user) or {}
    print(f"🔍 Estado carregado para {user}: {sessao_banco}")

    # Analisa a mensagem atual e mescla com o estado anterior
    estado_final = analisar_contexto(data.texto, sessao_banco)
    
    # Preserva dados que vieram do banco mas não foram sobrescritos
    for campo in ["produto", "plano", "whatsapp", "endereco", "pix_gerado"]:
        if campo not in estado_final or estado_final[campo] is None:
            if campo in sessao_banco and sessao_banco[campo] is not None:
                estado_final[campo] = sessao_banco[campo]
    
    prod = estado_final.get("produto")
    plan = estado_final.get("plano")
    zap = estado_final.get("whatsapp")
    end = estado_final.get("endereco")
    pix_gerado = estado_final.get("pix_gerado", False)

    # LOGS DETALHADOS
    print(f"--- DEBUG ---")
    print(f"Usuário: {user}")
    print(f"Mensagem: {data.texto}")
    print(f"Produto: {prod}")
    print(f"Plano: {plan}")
    print(f"WhatsApp: {zap}")
    print(f"Endereço: {end}")
    print(f"PIX gerado: {pix_gerado}")

    dados_validos = zap and end and len(str(zap)) > 6 and len(str(end)) > 5

    # ========== INTERVENÇÃO MANUAL REFORÇADA ==========
    # Se o cliente já escolheu um produto (no estado atual ou no anterior), mas ainda não escolheu o plano,
    # e a mensagem contém "mensal" ou "unico", definimos o plano manualmente.
    produto_definido = prod is not None or (sessao_banco.get("produto") is not None)
    if produto_definido and not plan:
        if "mensal" in txt_low or "assinatura" in txt_low:
            print(">>> Intervenção: plano Mensal detectado")
            estado_final["plano"] = "Mensal"
            plan = "Mensal"
            db_upsert_session(user, estado_final)
            return {
                "respostas": [f"Fechou, plano Mensal! Agora me passa seu WhatsApp e endereço, por favor."],
                "imagem": None,
                "pix": None
            }
        elif "unico" in txt_low or "único" in txt_low or "avista" in txt_low or "à vista" in txt_low:
            print(">>> Intervenção: plano Único detectado")
            estado_final["plano"] = "Único"
            plan = "Único"
            db_upsert_session(user, estado_final)
            return {
                "respostas": [f"Fechou, plano Único! Agora me passa seu WhatsApp e endereço, por favor."],
                "imagem": None,
                "pix": None
            }

    # ========== GERAÇÃO DE PIX ==========
    pix_code = None
    payment_id = None

    if prod and plan and dados_validos and not pix_gerado:
        if "Whey" in prod: preco = 149.90
        elif "Creatina" in prod: preco = 99.90
        else: preco = 49.90

        if plan == "Mensal": preco = round(preco * 0.9, 2)

        try:
            if MP_ACCESS_TOKEN:
                sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
                payment_data = {
                    "transaction_amount": preco,
                    "description": f"{prod} ({plan})",
                    "payment_method_id": "pix",
                    "payer": {"email": "cliente@mars.com", "first_name": user},
                }
                mp_res = sdk.payment().create(payment_data)
                if mp_res["status"] == 201:
                    pix_code = mp_res["response"]["point_of_interaction"]["transaction_data"]["qr_code"]
                    payment_id = str(mp_res["response"]["id"])
                    estado_final["pix_gerado"] = True
                    estado_final["payment_id"] = payment_id
                    enviar_telegram(f"🟡 *NOVO PEDIDO:*\n👤 {user}\n🛒 {prod} ({plan})\n💰 R$ {preco:.2f}\n📱 `{zap}`\n📍 {end}")
        except Exception as e:
            print("Erro ao gerar PIX:", e)

    # Salva o estado final (no Supabase e/ou memória)
    db_upsert_session(user, estado_final)

    # ========== CONSTRUÇÃO DO STATUS_MSG ==========
    if pix_gerado:
        status_msg = f"Pedido de {prod} ({plan}) já gerou PIX. Cliente pode perguntar sobre outros produtos ou status do pagamento."
    elif not prod:
        status_msg = "Cliente ainda não escolheu produto. OFEREÇA O CARDÁPIO COMPLETO."
    elif not plan:
        status_msg = f"Cliente escolheu {prod}. Falta definir o plano (Único ou Mensal)."
    elif not dados_validos:
        status_msg = f"Cliente vai levar {prod} ({plan}). Falta WhatsApp e Endereço."
    else:
        status_msg = f"Todos os dados coletados. PIX será gerado."

    # ========== PROMPT ==========
    prompt = f"""
    Você é a MARS, assistente virtual da loja de suplementos.  
    Cliente: {user}.  
    Status atual: {status_msg}.  

    Use emojis e tom motivacional, mas seja direta.

    **REGRAS RÍGIDAS (siga exatamente):**

    1. **Leia o Status atual com atenção.** Ele diz o que já foi coletado e o que falta.

    2. Se o cliente **já escolheu um produto** (ex: Creatina) e **ainda não escolheu o plano** → pergunte APENAS o plano (Único ou Mensal).  
       Ex: "BOA! Creatina é energia pura! 💪 Vai querer plano Único ou Mensal? (Mensal tem 10% de desconto)"

    3. Se o cliente **já escolheu produto E plano**, mas **faltam WhatsApp e/ou endereço** → peça os dados que faltam.  
       - Se falta endereço: "Perfeito! Agora manda o endereço de entrega, por favor."  
       - Se falta WhatsApp: "Show! Agora me passa seu WhatsApp pra finalizar."  
       - Se faltam ambos: "Quase lá! Me passa seu WhatsApp e endereço pra entrega."

    4. Se o cliente **ainda não escolheu produto** → apresente o cardápio.  
       Cardápio:  
       💊 Creatina Pura R$99,90  
       🥛 Whey Gold R$149,90  
       👕 Camiseta Mars R$49,90  
       Planos: 🔁 Mensal (10% desconto) | 🎯 Único

    5. Se o cliente **já tem PIX gerado** → informe e pergunte se quer mais algo ou ver status.

    6. **NUNCA repita perguntas desnecessárias.** Se o cliente já respondeu algo, não pergunte de novo.

    7. **Interpretação de palavras-chave:**  
       - "único", "unico", "avista", "à vista" significam **plano Único**.  
       - "mensal", "assinatura" significam **plano Mensal**.  
       - Se o status disser que o produto já foi escolhido e você receber uma dessas palavras, responda confirmando o plano e peça os dados de contato (se ainda não tiver).  
       - **Nunca** liste produtos novamente quando o cliente já tiver escolhido um, a menos que ele peça explicitamente "quais produtos vocês vendem?".

    **Exemplos de respostas corretas:**

    - Cliente: "quero creatina" (status: produto não escolhido) → "Creatina é pré-treino top! 💊 Vai querer plano Único ou Mensal? (Mensal 10% off)"
    - Cliente: "plano unico" (status: produto já escolhido, falta plano) → "Fechou, plano Único! Agora me passa seu WhatsApp e endereço, por favor."
    - Cliente: "único" (status: produto já escolhido, falta plano) → "Show, plano Único! Agora preciso do seu WhatsApp e endereço para entrega, pode mandar?"
    - Cliente: "mensal" (status: produto já escolhido, falta plano) → "Fechou, plano Mensal! Agora me passa seu WhatsApp e endereço, por favor."
    - Cliente: "meu zap é 11999999999" (status: falta endereço) → "WhatsApp salvo! Agora manda o endereço de entrega."
    - Cliente: "rua x, 123" (status: falta WhatsApp) → "Endereço anotado! Só falta o WhatsApp pra gerar o PIX."
    - Cliente: "qual o status do meu pedido?" (status: PIX já gerado) → "Seu pagamento está sendo processado. Assim que confirmarmos, avisamos! Quer aproveitar e pedir mais algo? 🚀"
    - Cliente: "quais produtos vocês vendem?" (qualquer status) → liste o cardápio.

    Mantenha as respostas curtas, energéticas e SEMPRE baseadas no status atual.
    """

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": data.texto}],
            temperature=0.3
        )
        resposta_texto = resp.choices[0].message.content
    except Exception as e:
        resposta_texto = "Conexão instável. Tente novamente em instantes."

    img_url = None
    if prod and "Whey" in prod:
        img_url = "https://m.media-amazon.com/images/I/41sdCLWi29L._AC_SY300_SX300_QL70_ML2_.jpg"
    elif prod and "Creatina" in prod:
        img_url = "https://http2.mlstatic.com/D_NQ_NP_2X_942122-MLA99923169249_112025-F.webp"

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
                enviar_telegram(f"🟢 *VENDA APROVADA!* R$ {val}")
        return {"status": "ok"}
    except: return {"status": "error"}
