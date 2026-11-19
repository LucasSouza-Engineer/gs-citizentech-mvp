"""API do componente de Visão + tela de cadastro do MVP.

Fluxo no MVP (alerta de enchente e calor por endereço):
  1. o cidadão fotografa uma conta de luz, água ou internet;
  2. POST /api/v1/enderecos/extrair devolve o endereço lido + campos a revisar;
  3. o cidadão CONFIRMA ou CORRIGE o endereço na tela (pessoa no circuito);
  4. POST /api/v1/cadastros grava o endereço confirmado; é ele que o serviço
     de alertas usa para saber em que área de risco (CEP/bairro/cidade) a pessoa está.

LGPD: a foto é processada em memória e descartada; só o endereço confirmado e
o contato informado são gravados, com consentimento explícito.

Rodar:  uvicorn api.app:app --reload   (a partir da raiz do repositório)
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from visao_enderecos import __version__, processar  # noqa: E402
from visao_enderecos.endereco import Endereco, formatar_cep, uf_do_cep, validar  # noqa: E402

DB_PATH = Path(os.getenv("CADASTRO_DB", RAIZ / "dados" / "cadastros.sqlite3"))
TAMANHO_MAX = 10 * 1024 * 1024
TIPOS_ACEITOS = {"image/jpeg", "image/png", "image/webp", "image/heic", "application/octet-stream"}

app = FastAPI(title="CitizenTech · Visão: extração de endereço", version=__version__,
              description="Lê o endereço de contas de consumo (Enel, Sabesp, Claro) para o cadastro de alertas.")
app.mount("/static", StaticFiles(directory=RAIZ / "api" / "static"), name="static")


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS cadastros (
        id TEXT PRIMARY KEY, criado_em TEXT, canal TEXT, contato TEXT,
        logradouro TEXT, numero TEXT, complemento TEXT, bairro TEXT, cidade TEXT, uf TEXT, cep TEXT,
        origem TEXT, empresa_documento TEXT, campos_corrigidos TEXT, confianca_ocr REAL)""")
    return con


# --------------------------------------------------------------------------
class EnderecoIn(BaseModel):
    logradouro: str = Field(min_length=3, max_length=120)
    numero: str = Field(min_length=1, max_length=10)
    complemento: str = Field(default="", max_length=60)
    bairro: str = Field(min_length=2, max_length=80)
    cidade: str = Field(min_length=2, max_length=80)
    uf: str = Field(min_length=2, max_length=2)
    cep: str

    @field_validator("cep")
    @classmethod
    def _cep(cls, v: str) -> str:
        v = formatar_cep(v)
        if not re.fullmatch(r"\d{5}-\d{3}", v):
            raise ValueError("CEP deve ter 8 dígitos")
        return v

    @field_validator("uf")
    @classmethod
    def _uf(cls, v: str) -> str:
        return v.upper()


class CadastroIn(BaseModel):
    endereco: EnderecoIn
    canal: str = Field(pattern="^(whatsapp|sms)$")
    contato: str = Field(description="Celular com DDD")
    consentimento: bool = Field(description="Aceite para uso do endereço e do contato no envio de alertas")
    origem: str = Field(default="ocr", pattern="^(ocr|manual)$")
    empresa_documento: str | None = None
    campos_corrigidos: list[str] = []
    confianca_ocr: float | None = None

    @field_validator("contato")
    @classmethod
    def _contato(cls, v: str) -> str:
        d = re.sub(r"\D", "", v)
        if d.startswith("55") and len(d) in (12, 13):
            d = d[2:]
        if len(d) not in (10, 11):
            raise ValueError("Informe o celular com DDD")
        return d


def _abrir_imagem(dados: bytes):
    """Abre a foto respeitando a rotação gravada pelo celular (EXIF Orientation)."""
    import io

    from PIL import Image, ImageOps
    try:
        with Image.open(io.BytesIO(dados)) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            return cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
    except Exception:
        return cv2.imdecode(np.frombuffer(dados, np.uint8), cv2.IMREAD_COLOR)


def _mascarar(contato: str) -> str:
    return f"({contato[:2]}) *****-{contato[-4:]}"


# --------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(RAIZ / "api" / "static" / "index.html")


@app.get("/api/v1/saude")
def saude():
    import pytesseract
    return {"status": "ok", "versao": __version__, "tesseract": str(pytesseract.get_tesseract_version())}


@app.post("/api/v1/enderecos/extrair")
async def extrair(arquivo: UploadFile = File(...)):
    if arquivo.content_type not in TIPOS_ACEITOS:
        raise HTTPException(415, "Envie uma foto (JPG, PNG ou WEBP)")
    dados = await arquivo.read(TAMANHO_MAX + 1)
    if len(dados) > TAMANHO_MAX:
        raise HTTPException(413, "Foto maior que 10 MB")
    img = _abrir_imagem(dados)
    del dados  # a foto não é gravada em lugar nenhum
    if img is None:
        raise HTTPException(422, "Não consegui abrir a imagem")
    # celulares geram fotos enormes: reduz para acelerar sem perder legibilidade
    escala = 2000 / max(img.shape[:2])
    if escala < 1:
        img = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    r = processar(img)
    corpo = r.to_dict()
    # trecho do documento onde o endereço foi lido, para o cidadão conferir
    if r.caixa_endereco and r.documento is not None:
        x1, y1, x2, y2 = r.caixa_endereco
        h, w = r.documento.shape[:2]
        recorte = r.documento[max(0, y1 - 50):min(h, y2 + 15), max(0, x1 - 15):min(w, x2 + 15)]
        ok, buf = cv2.imencode(".jpg", recorte, [cv2.IMWRITE_JPEG_QUALITY, 80])
        corpo["trecho_documento"] = "data:image/jpeg;base64," + base64.b64encode(buf).decode() if ok else None
    return corpo


@app.post("/api/v1/enderecos/validar")
def validar_endereco(e: EnderecoIn):
    end = Endereco(**e.model_dump())
    v = validar(end)
    return {"valido": v.valido, "erros": v.erros, "avisos": v.avisos, "campos_revisar": v.campos_revisar,
            "uf_esperada_pelo_cep": uf_do_cep(end.cep)}


@app.post("/api/v1/cadastros", status_code=201)
def cadastrar(c: CadastroIn):
    if not c.consentimento:
        raise HTTPException(422, "É preciso aceitar o uso dos dados para receber alertas")
    end = Endereco(**c.endereco.model_dump())
    v = validar(end)
    if not v.valido:
        raise HTTPException(422, {"mensagem": "Endereço incompleto ou inconsistente", "erros": v.erros,
                                  "campos_revisar": v.campos_revisar})
    ident = str(uuid.uuid4())
    with closing(_db()) as con, con:
        con.execute("INSERT INTO cadastros VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ident, datetime.now(timezone.utc).isoformat(), c.canal, c.contato, end.logradouro, end.numero,
                     end.complemento, end.bairro, end.cidade, end.uf, end.cep, c.origem, c.empresa_documento,
                     json.dumps(c.campos_corrigidos), c.confianca_ocr))
    return {"id": ident, "endereco": end.to_dict(), "canal": c.canal, "contato": _mascarar(c.contato),
            "mensagem": f"Pronto! Você vai receber alertas de chuva forte e calor para o CEP {end.cep}."}


@app.get("/api/v1/cadastros/{ident}")
def obter(ident: str):
    with closing(_db()) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM cadastros WHERE id = ?", (ident,)).fetchone()
    if not row:
        raise HTTPException(404, "Cadastro não encontrado")
    d = dict(row)
    d["contato"] = _mascarar(d["contato"])
    d["campos_corrigidos"] = json.loads(d["campos_corrigidos"] or "[]")
    return d


@app.delete("/api/v1/cadastros/{ident}", status_code=204)
def excluir(ident: str):
    """Direito de exclusão (LGPD art. 18)."""
    with closing(_db()) as con, con:
        n = con.execute("DELETE FROM cadastros WHERE id = ?", (ident,)).rowcount
    if not n:
        raise HTTPException(404, "Cadastro não encontrado")


@app.get("/api/v1/metricas")
def metricas():
    """Indicadores de uso real do OCR: quanto o cidadão precisou corrigir."""
    with closing(_db()) as con:
        linhas = con.execute("SELECT origem, campos_corrigidos FROM cadastros").fetchall()
    total = len(linhas)
    ocr = [json.loads(c or "[]") for o, c in linhas if o == "ocr"]
    sem_corr = sum(1 for c in ocr if not c)
    por_campo: dict[str, int] = {}
    for c in ocr:
        for campo in c:
            por_campo[campo] = por_campo.get(campo, 0) + 1
    return {"cadastros": total, "via_ocr": len(ocr), "via_manual": total - len(ocr),
            "ocr_aceito_sem_correcao": round(sem_corr / len(ocr), 3) if ocr else None,
            "correcoes_por_campo": por_campo}
