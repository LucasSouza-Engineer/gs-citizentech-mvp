"""MVP CitizenTech · Alerta de enchente e calor por endereço.

Um único serviço web que junta as duas peças já prontas, sem reescrevê-las:
  visao/     cadastro pela foto da conta (OCR) + tela de confirmação  -> tabela `cadastros`
  pipeline/  regras de risco por CEP (INMET + Cemaden) e fila de alertas -> `risco_por_cep`, `fila_alertas`
e acrescenta o que faltava para virar produto: agendamento do pipeline, disparo dos alertas,
página "meu endereço" para o cidadão e um painel público.

Rodar local:  uvicorn mvp.main:app --port 8000   (na raiz deste repositório)
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
# um só banco para cadastro, risco e fila (o mesmo SQLite que o Airflow publica)
os.environ.setdefault("CADASTRO_DB", str(RAIZ / "dados" / "cadastros.sqlite3"))
os.environ.setdefault("PIPELINE_RAIZ", str(RAIZ / "pipeline"))
os.environ.setdefault("PIPELINE_AMOSTRA", str(RAIZ / "pipeline" / "amostra"))
os.environ.setdefault("PIPELINE_LAKE", str(RAIZ / "dados" / "lake"))
os.environ.setdefault("PIPELINE_MODO", "offline")
sys.path[:0] = [str(RAIZ / "visao"), str(RAIZ / "pipeline")]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from fastapi import Header, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from api import app as visao_api  # noqa: E402  (componente de Visão, intacto)
from mvp import alertas  # noqa: E402

app = visao_api.app
app.title = "CitizenTech · Alerta de enchente e calor por endereço"
app.description = ("MVP: cadastro pela foto da conta (Visão), risco por CEP com dados do INMET e do Cemaden "
                   "(pipeline de dados) e disparo de alertas por WhatsApp/SMS.")
ESTATICO = RAIZ / "mvp" / "static"
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
INTERVALO_MIN = int(os.getenv("PIPELINE_INTERVALO_MIN", "60"))

# rotas da Visão que o MVP estende: "/" ganha navegação e o DELETE apaga também os alertas
app.router.routes = [r for r in app.router.routes
                     if not (isinstance(r, APIRoute) and (r.path == "/" or
                                                          (r.path == "/api/v1/cadastros/{ident}" and "DELETE" in r.methods)))]
app.mount("/mvp", StaticFiles(directory=ESTATICO), name="mvp")


@app.on_event("startup")
def _iniciar():
    alertas.preparar_banco()
    if os.getenv("DEMO_SEED", "1") == "1":
        alertas.semear_demo()
    if os.getenv("PIPELINE_AGENDADO", "1") == "1":
        alertas.agendar(INTERVALO_MIN)


@app.middleware("http")
async def _apos_cadastro(request: Request, call_next):
    resp = await call_next(request)
    # novo cadastro: recalcula o risco e a fila na hora, sem esperar a próxima execução agendada
    if request.method == "POST" and request.url.path == "/api/v1/cadastros" and resp.status_code == 201:
        alertas.rodar_em_segundo_plano()
    return resp


@app.get("/", include_in_schema=False)
def index():
    html = (RAIZ / "visao" / "api" / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("</body>", '<script src="/mvp/cadastro-extra.js"></script>\n</body>'))


@app.get("/painel", include_in_schema=False)
def painel():
    return FileResponse(ESTATICO / "painel.html")


@app.get("/meu", include_in_schema=False)
def meu():
    return FileResponse(ESTATICO / "meu.html")


@app.get("/api/v1/risco/{cep}", tags=["alertas"])
def risco_cep(cep: str):
    """Risco atual de enchente e de calor para um CEP (níveis 0 normal, 1 atenção, 2 alerta, 3 alerta máximo)."""
    r = alertas.risco_do_cep(cep)
    if not r:
        raise HTTPException(404, "Ainda não temos dados para esse CEP. Tente de novo em alguns minutos.")
    return r


@app.get("/api/v1/regioes", tags=["alertas"])
def regioes():
    """Risco atual nas regiões de referência da Grande SP (mesma tabela que alimenta o BI)."""
    return alertas.regioes()


@app.get("/api/v1/painel", tags=["alertas"])
def painel_dados():
    return {"resumo": alertas.resumo(), "alertas": alertas.alertas_recentes()}


@app.get("/api/v1/cadastros/{ident}/alertas", tags=["alertas"])
def alertas_cadastro(ident: str):
    cad = visao_api.obter(ident)  # 404 se não existir; contato já mascarado
    return {"cadastro": cad, "risco": alertas.risco_do_cep(cad["cep"]), "alertas": alertas.alertas_do_cadastro(ident)}


@app.delete("/api/v1/cadastros/{ident}", status_code=204, tags=["default"])
def excluir(ident: str):
    """Direito de exclusão (LGPD art. 18): apaga o cadastro, a fila e o histórico de envio."""
    visao_api.excluir(ident)
    alertas.excluir_alertas(ident)


@app.post("/api/v1/pipeline/executar", tags=["operação"])
def executar(x_admin_token: str = Header(default="")):
    """Roda o pipeline e o disparo agora (o agendador já faz isso a cada PIPELINE_INTERVALO_MIN minutos)."""
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "token inválido")
    return alertas.rodar_ciclo()
