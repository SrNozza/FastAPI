import os, re
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import httpx
from dotenv import load_dotenv

load_dotenv()

FACTA_BASE = os.getenv("FACTA_BASE_URL", "https://webservice-homol.facta.com.br").rstrip("/")
BASIC_AUTH = os.getenv("FACTA_BASIC_AUTH")  # ex: "Basic <base64(usuario:senha)>"
LOGIN_CERT = os.getenv("LOGIN_CERTIFICADO", "96676")
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))

app = FastAPI(title="Helena ↔ FACTA API", version="1.0.0")

def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")

async def get_token():
    headers = {"Authorization": BASIC_AUTH, "Accept":"application/json", "User-Agent":"helena-facta-api/1.0"}
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as cx:
        r = await cx.get(f"{FACTA_BASE}/gera-token", headers=headers)
        # Cloudflare/WAF geralmente retorna HTML. Tratamos isso aqui:
        if r.headers.get("content-type","").startswith("text/html"):
            raise HTTPException(502, "FACTA retornou página HTML (WAF). Peça whitelist de IP.")
        if r.status_code != 200:
            raise HTTPException(502, f"Token error: {r.text}")
        j = r.json()
        token = j.get("token")
        if not token:
            raise HTTPException(502, f"Token vazio: {j}")
        return token

# --------- Schemas de entrada ---------
class ConsultaIn(BaseModel):
    nome: str
    cpf: str
    data_nascimento: str = Field(..., description="dd/mm/aaaa")
    meses_vinculo: int | None = None
    renda: float | None = None
    canal: str | None = "chatbot"
    origem: str | None = "helena"
    id_contato: str | None = None
    telefone: str | None = None

class FormalizarIn(BaseModel):
    cpf: str
    data_nascimento: str
    renda: float
    sexo: str
    opcao_valor: int
    valor_parcela: float
    prazo: int

    # pessoais + docs
    nome: str
    estado_civil: str
    rg: str
    estado_rg: str
    orgao_emissor: str
    data_expedicao: str
    estado_natural: str
    cidade_natural: str
    nacionalidade: str

    # contato e endereço
    celular: str
    cep: str
    endereco: str
    numero: str
    bairro: str
    estado: str
    cidade: str
    nome_mae: str
    nome_pai: str
    valor_patrimonio: str
    cliente_iletrado_impossibilitado: str

    # bancário/pix
    tipo_conta: str
    banco: str
    agencia: str
    conta: str
    tipo_chave_pix: str
    chave_pix: str

    # vínculo
    matricula: str
    cnpj_empregador: str
    data_admissao: str

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/consulta-ofertas")
async def consulta_ofertas(body: ConsultaIn):
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}", "Accept":"application/json", "User-Agent":"helena-facta-api/1.0"}
    cpf = digits(body.cpf)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as cx:
        r = await cx.get(f"{FACTA_BASE}/consignado-trabalhador/consulta-ofertas",
                         params={"cpf": cpf}, headers=headers)
        if r.headers.get("content-type","").startswith("text/html"):
            raise HTTPException(502, "FACTA retornou HTML (WAF). Peça whitelist de IP.")
        try:
            j = r.json()
        except Exception:
            raise HTTPException(502, f"Consulta error: {r.text}")
    ofertas_raw = j.get("dados") or j.get("ofertas") or []
    ofertas = []
    if isinstance(ofertas_raw, list):
        for d in ofertas_raw:
            ofertas.append({"oferta": d.get("oferta") or d.get("descricao"),
                            "resposta": d.get("resposta") or d.get("situacao")})
    return {"status":"ok","cpf":cpf,"total_ofertas":len(ofertas),"ofertas":ofertas}

@app.post("/formalizar")
async def formalizar(body: FormalizarIn):
    token = await get_token()
    h = {"Authorization": f"Bearer {token}", "User-Agent":"helena-facta-api/1.0"}
    cpf = digits(body.cpf)

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as cx:
        # 1) Operações disponíveis
        r1 = await cx.get(f"{FACTA_BASE}/proposta/operacoes-disponiveis", params={
            "produto":"D","tipo_operacao":"13","averbador":"10010","convenio":"3",
            "opcao_valor": body.opcao_valor,
            "valor_parcela": body.valor_parcela,
            "prazo": body.prazo,
            "cpf": cpf,
            "data_nascimento": body.data_nascimento,
            "valor_renda": body.renda
        }, headers=h)
        t = r1.json().get("tabelas", [])
        if not t:
            raise HTTPException(400, "Sem tabela disponível para os parâmetros informados.")
        best = sorted(t, key=lambda x: float(x.get("valor_liquido",0)), reverse=True)[0]

        # 2) Etapa 1 – simulador
        fd1 = {
            "produto":"D","tipo_operacao":"13","averbador":"10010","convenio":"3",
            "cpf": cpf, "data_nascimento": body.data_nascimento,
            "login_certificado": LOGIN_CERT,
            "codigo_tabela": str(best["codigoTabela"]),
            "prazo": str(best["prazo"]),
            "valor_operacao": str(best.get("contrato", best.get("valor_liquido", 0))),
            "valor_parcela": str(best.get("parcela", 0)),
            "coeficiente": str(best.get("coeficiente"))
        }
        r2 = await cx.post(f"{FACTA_BASE}/proposta/etapa1-simulador", headers=h, data=fd1)
        j2 = r2.json()
        id_sim = j2.get("id_simulador")
        if not id_sim:
            raise HTTPException(502, f"Falha etapa1: {j2}")

        # 3) Etapa 2 – dados pessoais (form-data grande)
        fd2 = body.model_dump()
        fd2.update({"id_simulador": id_sim, "cpf": cpf})
        r3 = await cx.post(f"{FACTA_BASE}/proposta/etapa2-dados-pessoais", headers=h, data=fd2)
        j3 = r3.json()
        codigo_cliente = j3.get("codigo_cliente")
        if not codigo_cliente:
            raise HTTPException(502, f"Falha etapa2: {j3}")

        # 4) Etapa 3 – proposta/cadastro
        fd3 = {"codigo_cliente": codigo_cliente, "id_simulador": id_sim, "tipo_formalizacao":"DIG"}
        r4 = await cx.post(f"{FACTA_BASE}/proposta/etapa3-proposta-cadastro", headers=h, data=fd3)
        j4 = r4.json()
        codigo = j4.get("codigo")
        url_form = j4.get("url_formalizacao")

        if not codigo:
            raise HTTPException(502, f"Falha etapa3: {j4}")

        # 5) Envio de link ao cliente
        await cx.post(f"{FACTA_BASE}/proposta/envio-link", headers=h, data={"codigo_af": codigo, "tipo_envio":"Whatsapp"})

    return {"status":"ok","mensagem":"Proposta criada e link enviado.","codigo":codigo,"url_formalizacao":url_form}
