"""Checagens de qualidade de dados antes de publicar para o MVP.

Cada regra tem severidade:
  erro  -> a task falha e NADA é publicado (o MVP continua com a última tabela boa)
  aviso -> fica registrado no relatório e no log, mas a publicação segue
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import pandas as pd

from . import config

log = logging.getLogger(__name__)


class FalhaQualidade(Exception):
    pass


def checar(cad: pd.DataFrame, obs: pd.DataFrame, pluv: pd.DataFrame, risco: pd.DataFrame,
           fila: pd.DataFrame, ref: datetime) -> dict:
    t = pd.Timestamp(ref)
    regras = []

    def regra(nome, ok, severidade, detalhe):
        regras.append({"regra": nome, "ok": bool(ok), "severidade": severidade, "detalhe": detalhe})

    geo = cad["latitude"].notna().mean() if len(cad) else 0
    regra("cadastros_nao_vazio", len(cad) > 0, "erro", f"{len(cad)} cadastros")
    regra("cadastros_id_unico", cad["id_cadastro"].is_unique, "erro", "id_cadastro sem duplicata")
    regra("cadastros_cep_8_digitos", cad["cep"].str.fullmatch(r"\d{8}").all(), "erro", "todo CEP com 8 dígitos")
    regra("cadastros_geocodificados", geo >= 0.9, "erro" if geo < 0.5 else "aviso",
          f"{geo:.0%} dos cadastros com coordenada (meta 90%)")

    ultima = obs["ts_utc"].max() if len(obs) else None
    atraso = (t - ultima).total_seconds() / 3600 if ultima is not None else None
    regra("inmet_tem_observacoes", len(obs) > 0, "erro", f"{len(obs)} observações horárias")
    regra("inmet_atualizado", atraso is not None and atraso <= 3, "aviso",
          f"última observação há {atraso:.1f} h (máx. 3 h)" if atraso is not None else "sem observação")
    nulos_temp = obs["temp_c"].isna().mean() if len(obs) else 1
    regra("inmet_temperatura_preenchida", nulos_temp <= 0.2, "aviso", f"{nulos_temp:.0%} de temperatura nula")
    regra("cemaden_tem_pluviometros", len(pluv) > 0, "aviso",
          f"{len(pluv)} pluviômetros (sem eles a chuva vem só do INMET)")

    regra("risco_cobre_todos_ceps", set(cad.dropna(subset=["latitude"])["cep"]) <= set(risco["cep"]), "erro",
          "todo CEP geocodificado tem linha de risco")
    regra("risco_niveis_validos", risco[["nivel_enchente", "nivel_calor"]].isin([0, 1, 2, 3]).all().all(), "erro",
          "níveis entre 0 e 3")
    regra("risco_sem_nulos_chave", risco[["cep", "nivel_geral", "situacao"]].notna().all().all(), "erro",
          "cep, nivel_geral e situacao preenchidos")
    regra("fila_sem_duplicata", fila["id_alerta"].is_unique, "erro", "um alerta por cadastro/tipo/nível/janela")

    erros = [r for r in regras if not r["ok"] and r["severidade"] == "erro"]
    avisos = [r for r in regras if not r["ok"] and r["severidade"] == "aviso"]
    rel = {"referencia_utc": t.isoformat(), "aprovado": not erros, "regras": regras,
           "resumo": {"total": len(regras), "ok": sum(r["ok"] for r in regras), "erros": len(erros),
                      "avisos": len(avisos)}}
    p = config.LAKE / "gold" / "qualidade" / f"dt={t:%Y-%m-%d}" / f"{t:%H%M}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rel, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in regras:
        log.info("[%s] %-32s %s", "OK " if r["ok"] else r["severidade"].upper(), r["regra"], r["detalhe"])
    if erros:
        raise FalhaQualidade("Qualidade reprovada: " + ", ".join(r["regra"] for r in erros))
    return rel
