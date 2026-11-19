"""Extração (camada bronze): busca o dado bruto de cada fonte e grava como veio.

Cada fonte tenta primeiro a API real. Se ela não responder (sem internet, token
ausente, fora do ar), o modo "auto" usa a amostra local em dados/amostra/, que
segue o mesmo formato da API. O arquivo bronze registra de onde o dado veio
(campo "origem": "api" ou "amostra"), para a rastreabilidade ficar no próprio dado.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)
UA = {"User-Agent": "citizentech-pipeline/1.0 (projeto academico)"}


# ---------------------------------------------------------------- utilitários
def agora_utc() -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _caminho_bronze(fonte: str, ref: datetime) -> Path:
    p = config.LAKE / "bronze" / fonte / f"dt={ref:%Y-%m-%d}" / f"{ref:%H%M}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _gravar_bronze(fonte: str, ref: datetime, origem: str, url: str, dados) -> str:
    caminho = _caminho_bronze(fonte, ref)
    envelope = {"fonte": fonte, "origem": origem, "url": url,
                "coletado_em": datetime.now(timezone.utc).isoformat(), "referencia": ref.isoformat(),
                "registros": len(dados) if hasattr(dados, "__len__") else None, "dados": dados}
    caminho.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    log.info("bronze/%s: %s registros (%s) -> %s", fonte, envelope["registros"], origem, caminho)
    return str(caminho)


def _amostra(nome: str):
    return json.loads((config.AMOSTRA / nome).read_text(encoding="utf-8"))


def _get_json(url: str):
    r = requests.get(url, headers=UA, timeout=config.TIMEOUT)
    r.raise_for_status()
    return r.json()


def _com_fallback(fonte: str, url: str, buscar, amostra):
    """Executa a busca real conforme o modo do pipeline; cai para a amostra no modo auto."""
    if config.MODO != "offline":
        try:
            dados = buscar()
            if not dados:
                raise ValueError("fonte respondeu vazio")
            return "api", dados
        except Exception as e:  # noqa: BLE001 - qualquer falha de rede/formato vira fallback
            if config.MODO == "online":
                raise
            log.warning("%s indisponível (%s: %s). Usando amostra local.", fonte, type(e).__name__, e)
    return "amostra", amostra()


# A amostra é um cenário gravado (fim de tarde quente com temporal na Grande SP).
# Para a demonstração parecer "ao vivo", os horários são deslocados para a hora atual.
def _deslocamento(ultimo: datetime, ref: datetime) -> timedelta:
    return ref - ultimo


# ---------------------------------------------------------------- INMET estações
def extrair_inmet_estacoes(ref: datetime) -> str:
    url = f"{config.INMET_API}/estacoes/T"

    def buscar():
        return [e for e in _get_json(url) if e.get("SG_ESTADO") == config.UF]

    origem, dados = _com_fallback("inmet_estacoes", url, buscar, lambda: _amostra("inmet_estacoes.json"))
    return _gravar_bronze("inmet_estacoes", ref, origem, url, dados)


# ---------------------------------------------------------------- INMET observações horárias
def _estacoes_alvo() -> list[str]:
    return [e["CD_ESTACAO"] for e in _amostra("inmet_estacoes.json")]


def extrair_inmet_observacoes(ref: datetime) -> str:
    ini, fim = (ref - timedelta(days=3)).date(), ref.date()
    base = f"{config.INMET_API}/token/estacao" if config.INMET_TOKEN else f"{config.INMET_API}/estacao"
    url = f"{base}/{ini}/{fim}/{{estacao}}" + (f"/{config.INMET_TOKEN}" if config.INMET_TOKEN else "")

    def buscar():
        dados = []
        for cod in _estacoes_alvo():
            dados.extend(_get_json(url.format(estacao=cod)) or [])
        return dados

    def amostra():
        dados = _amostra("inmet_observacoes.json")
        ts = [datetime.strptime(f"{d['DT_MEDICAO']} {d['HR_MEDICAO']}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
              for d in dados]
        delta = _deslocamento(max(ts), ref)
        for d, t in zip(dados, ts):
            n = t + delta
            d["DT_MEDICAO"], d["HR_MEDICAO"] = f"{n:%Y-%m-%d}", f"{n:%H%M}"
        return dados

    url_publica = url.replace(config.INMET_TOKEN, "***") if config.INMET_TOKEN else url  # token fora do log
    origem, dados = _com_fallback("inmet_observacoes", url_publica, buscar, amostra)
    return _gravar_bronze("inmet_observacoes", ref, origem, url_publica, dados)


# ---------------------------------------------------------------- INMET avisos meteorológicos
def extrair_inmet_avisos(ref: datetime) -> str:
    url = config.INMET_AVISOS_URL

    def buscar():
        bruto = _get_json(url)
        # a API devolve {"hoje": [...], "futuro": [...]}; achata mantendo de qual lista veio
        if isinstance(bruto, dict):
            return [dict(a, _lista=k) for k, v in bruto.items() if isinstance(v, list) for a in v] or [{"_vazio": True}]
        return bruto

    def amostra():
        dados = _amostra("inmet_avisos.json")
        # os horários do INMET estão em Brasília (UTC-3); o primeiro aviso passa a ter começado 3 h antes de ref
        fmt = "%Y-%m-%d %H:%M:%S.%f"
        brt = timezone(timedelta(hours=-3))
        base = datetime.strptime(dados[0]["inicio"], fmt).replace(tzinfo=brt)
        delta = _deslocamento(base, ref - timedelta(hours=3))
        for a in dados:
            for k in ("inicio", "fim"):
                a[k] = (datetime.strptime(a[k], fmt) + delta).strftime(fmt)[:-5]
        return dados

    origem, dados = _com_fallback("inmet_avisos", url, buscar, amostra)
    return _gravar_bronze("inmet_avisos", ref, origem, url, dados)


# ---------------------------------------------------------------- Cemaden pluviômetros
def extrair_cemaden(ref: datetime) -> str:
    url = config.CEMADEN_URL.format(uf=config.UF)

    def amostra():
        dados = _amostra("cemaden_pluviometros.json")
        fmt = "%Y-%m-%d %H:%M:%S"
        ultimo = max(datetime.strptime(d["datahora"], fmt) for d in dados).replace(tzinfo=timezone.utc)
        delta = _deslocamento(ultimo, ref)
        for d in dados:
            d["datahora"] = (datetime.strptime(d["datahora"], fmt).replace(tzinfo=timezone.utc) + delta).strftime(fmt)
        return dados

    origem, dados = _com_fallback("cemaden", url, lambda: _get_json(url), amostra)
    return _gravar_bronze("cemaden", ref, origem, url, dados)


# ---------------------------------------------------------------- cadastros do MVP
# LGPD (minimização): o telefone NÃO entra no data lake. O pipeline só precisa do
# id e do endereço; o disparador de alertas busca o contato pelo id no banco do MVP.
COLUNAS_CADASTRO = ["id", "criado_em", "canal", "logradouro", "numero", "bairro", "cidade", "uf", "cep", "origem"]


def extrair_cadastros(ref: datetime) -> str:
    db = config.CADASTRO_DB
    if db.exists():
        with closing(sqlite3.connect(db)) as con:
            tem = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cadastros'").fetchone()
            linhas = con.execute(f"SELECT {', '.join(COLUNAS_CADASTRO)} FROM cadastros").fetchall() if tem else []
        if linhas:
            dados = [dict(zip(COLUNAS_CADASTRO, l)) for l in linhas]
            return _gravar_bronze("cadastros", ref, "mvp_sqlite", str(db), dados)
    if config.MODO == "online":
        raise FileNotFoundError(f"Banco de cadastros não encontrado ou vazio: {db}")
    log.warning("Banco do MVP sem cadastros (%s). Usando cadastros de exemplo.", db)
    dados = [{k: c[k] for k in COLUNAS_CADASTRO} for c in _amostra("cadastros_exemplo.json")]
    return _gravar_bronze("cadastros", ref, "amostra", "dados/amostra/cadastros_exemplo.json", dados)
