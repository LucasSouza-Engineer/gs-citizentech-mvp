"""Etapas da camada gold, chamadas pela DAG do Airflow e pelo script de execução local.

As tasks trocam apenas CAMINHOS de arquivo pelo XCom (nunca o dado em si), como recomenda o Airflow.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from . import carga, fontes, qualidade, risco, transformacao


def _ler(caminho: str) -> pd.DataFrame:
    return pd.read_parquet(caminho)


def calcular_gold(silver: dict[str, str], ref: datetime) -> dict[str, str]:
    cad, obs, est = _ler(silver["cadastros"]), _ler(silver["inmet_observacoes"]), _ler(silver["inmet_estacoes"])
    pluv, avisos, dim = _ler(silver["cemaden"]), _ler(silver["inmet_avisos"]), _ler(silver["dim_regiao"])
    ind = risco.indicadores_estacoes(obs, est, ref)
    r_cep = risco.risco_por_cep(cad, ind, pluv, avisos, ref)
    r_reg = risco.risco_por_regiao(dim, ind, pluv, avisos, ref)
    fila = risco.fila_alertas(cad, r_cep, ref)
    return {
        "risco_por_cep": carga.gravar_gold(r_cep, "risco_por_cep", ref),
        "risco_por_regiao": carga.gravar_gold(r_reg, "risco_por_regiao", ref),
        "indicadores_estacao": carga.gravar_gold(ind, "indicadores_estacao", ref),
        "fila_alertas": carga.gravar_gold(fila, "fila_alertas", ref),
    }


def checar_qualidade(silver: dict[str, str], gold: dict[str, str], ref: datetime) -> dict:
    return qualidade.checar(_ler(silver["cadastros"]), _ler(silver["inmet_observacoes"]), _ler(silver["cemaden"]),
                            _ler(gold["risco_por_cep"]), _ler(gold["fila_alertas"]), ref)


def publicar_mvp(gold: dict[str, str]) -> dict:
    return carga.publicar_mvp(_ler(gold["risco_por_cep"]), _ler(gold["fila_alertas"]))


def carregar_dw(silver: dict[str, str], gold: dict[str, str], ref: datetime) -> dict:
    return carga.carregar_dw({
        "dim_regiao": _ler(silver["dim_regiao"]),
        "fato_observacao_inmet": _ler(silver["inmet_observacoes"]),
        "fato_pluviometria_cemaden": _ler(silver["cemaden"]).drop(columns=["origem"]),
        "fato_risco_cep": _ler(gold["risco_por_cep"]).drop(columns=["referencia_utc"]),
        "fato_risco_regiao": _ler(gold["risco_por_regiao"]).drop(columns=["referencia_utc"]),
        "fato_alerta": _ler(gold["fila_alertas"]),
    }, ref)


def rodar_tudo(ref: datetime | None = None, com_dw: bool = True) -> dict:
    """Executa o pipeline inteiro em sequência, sem Airflow (testes e plano B da apresentação)."""
    ref = ref or fontes.agora_utc()
    bronze = {
        "inmet_estacoes": fontes.extrair_inmet_estacoes(ref),
        "inmet_observacoes": fontes.extrair_inmet_observacoes(ref),
        "inmet_avisos": fontes.extrair_inmet_avisos(ref),
        "cemaden": fontes.extrair_cemaden(ref),
        "cadastros": fontes.extrair_cadastros(ref),
    }
    t = transformacao
    silver = {
        "inmet_estacoes": t.tratar_inmet_estacoes(bronze["inmet_estacoes"], ref),
        "inmet_observacoes": t.tratar_inmet_observacoes(bronze["inmet_observacoes"], ref),
        "inmet_avisos": t.tratar_inmet_avisos(bronze["inmet_avisos"], ref),
        "cemaden": t.tratar_cemaden(bronze["cemaden"], ref),
        "cadastros": t.tratar_cadastros(bronze["cadastros"], ref),
        "dim_regiao": t.tratar_dim_regiao(ref),
    }
    gold = calcular_gold(silver, ref)
    rel = checar_qualidade(silver, gold, ref)
    mvp = publicar_mvp(gold)
    dw = carregar_dw(silver, gold, ref) if com_dw else {"dw": "pulado"}
    return {"referencia": ref.isoformat(), "bronze": bronze, "silver": silver, "gold": gold,
            "qualidade": rel["resumo"], "mvp": mvp, "dw": dw}
