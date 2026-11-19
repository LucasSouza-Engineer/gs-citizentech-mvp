"""Tratamento (camada silver): limpa, tipa, padroniza e deduplica cada fonte.

Entrada: arquivo bronze (JSON com envelope). Saída: Parquet em silver/<tabela>/dt=AAAA-MM-DD/HHMM.parquet.
Os nomes das colunas seguem o dicionário de dados (docs/GS_BigData_Pipeline.pdf).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import config

log = logging.getLogger(__name__)

SEVERIDADE_NIVEL = {"perigo potencial": 1, "perigo": 2, "grande perigo": 3}
# Faixas fisicamente plausíveis: fora delas o valor é erro de sensor e vira nulo.
FAIXAS = {"temp_c": (-10, 50), "temp_max_c": (-10, 50), "temp_min_c": (-10, 50), "umidade_pct": (0, 100),
          "chuva_mm": (0, 200), "vento_ms": (0, 60), "rajada_ms": (0, 80), "pressao_hpa": (800, 1100)}


# ---------------------------------------------------------------- utilitários
def ler_bronze(caminho: str) -> tuple[dict, list]:
    env = json.loads(Path(caminho).read_text(encoding="utf-8"))
    return {k: v for k, v in env.items() if k != "dados"}, env["dados"]


def gravar_silver(df: pd.DataFrame, tabela: str, ref: datetime) -> str:
    p = config.LAKE / "silver" / tabela / f"dt={ref:%Y-%m-%d}" / f"{ref:%H%M}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    log.info("silver/%s: %d linhas -> %s", tabela, len(df), p)
    return str(p)


def num(s: pd.Series) -> pd.Series:
    """Converte para número aceitando vírgula decimal e textos vazios da API do INMET."""
    return pd.to_numeric(s.astype("string").str.replace(",", ".", regex=False).str.strip(), errors="coerce")


def sem_acento(t: str) -> str:
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower().strip()


def so_digitos(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _col(df: pd.DataFrame, *nomes: str) -> pd.Series:
    """Primeira coluna existente entre as variações de nome que a fonte já usou."""
    mapa = {c.lower(): c for c in df.columns}
    for n in nomes:
        if n.lower() in mapa:
            return df[mapa[n.lower()]]
    return pd.Series([None] * len(df), index=df.index, dtype="object")


# ---------------------------------------------------------------- INMET
def tratar_inmet_estacoes(caminho: str, ref: datetime) -> str:
    meta, dados = ler_bronze(caminho)
    df = pd.DataFrame(dados)
    out = pd.DataFrame({
        "cd_estacao": _col(df, "CD_ESTACAO").astype("string").str.strip(),
        "nome_estacao": _col(df, "DC_NOME").astype("string").str.strip().str.title(),
        "uf": _col(df, "SG_ESTADO").astype("string"),
        "latitude": num(_col(df, "VL_LATITUDE")),
        "longitude": num(_col(df, "VL_LONGITUDE")),
        "situacao": _col(df, "CD_SITUACAO").astype("string"),
    }).dropna(subset=["cd_estacao", "latitude", "longitude"]).drop_duplicates("cd_estacao")
    out["origem"] = meta["origem"]
    return gravar_silver(out, "inmet_estacoes", ref)


def tratar_inmet_observacoes(caminho: str, ref: datetime) -> str:
    meta, dados = ler_bronze(caminho)
    df = pd.DataFrame(dados)
    hora = _col(df, "HR_MEDICAO").astype("string").str.zfill(4)
    out = pd.DataFrame({
        "cd_estacao": _col(df, "CD_ESTACAO").astype("string"),
        "ts_utc": pd.to_datetime(_col(df, "DT_MEDICAO").astype("string") + " " + hora,
                                 format="%Y-%m-%d %H%M", errors="coerce", utc=True),
        "temp_c": num(_col(df, "TEM_INS")),
        "temp_max_c": num(_col(df, "TEM_MAX")),
        "temp_min_c": num(_col(df, "TEM_MIN")),
        "umidade_pct": num(_col(df, "UMD_INS")),
        "chuva_mm": num(_col(df, "CHUVA")),
        "vento_ms": num(_col(df, "VEN_VEL")),
        "rajada_ms": num(_col(df, "VEN_RAJ")),
        "pressao_hpa": num(_col(df, "PRE_INS")),
    })
    for c, (lo, hi) in FAIXAS.items():
        out.loc[(out[c] < lo) | (out[c] > hi), c] = np.nan
    out = (out.dropna(subset=["cd_estacao", "ts_utc"])
              .sort_values(["cd_estacao", "ts_utc"])
              .drop_duplicates(["cd_estacao", "ts_utc"], keep="last"))
    out["origem"] = meta["origem"]
    return gravar_silver(out.reset_index(drop=True), "inmet_observacoes", ref)


def hora_brasilia_para_utc(v) -> pd.Timestamp:
    """Avisos do INMET vêm no horário de Brasília sem fuso; se vier com fuso, respeita."""
    t = pd.to_datetime(v, errors="coerce")
    if pd.isna(t):
        return pd.NaT
    return (t.tz_localize("America/Sao_Paulo") if t.tzinfo is None else t).tz_convert("UTC")


def _categoria_aviso(descricao: str) -> str:
    d = sem_acento(descricao)
    if any(k in d for k in ("chuva", "tempestade", "alagamento", "inundacao", "ciclone")):
        return "chuva"
    if any(k in d for k in ("calor", "temperatura", "baixa umidade")):
        return "calor"
    return "outro"


def tratar_inmet_avisos(caminho: str, ref: datetime) -> str:
    meta, dados = ler_bronze(caminho)
    linhas = []
    for a in dados:
        if a.get("_vazio"):
            continue
        geocodes = [g for g in (so_digitos(x) for x in str(a.get("geocodes", "")).split(",")) if len(g) == 7]
        for g in geocodes:
            linhas.append({
                "id_aviso": str(a.get("id")),
                "tipo_aviso": str(a.get("descricao", "")).strip(),
                "categoria": _categoria_aviso(a.get("descricao", "")),
                "severidade": str(a.get("severidade", "")).strip(),
                "nivel_aviso": SEVERIDADE_NIVEL.get(sem_acento(a.get("severidade", "")), 0),
                "inicio_utc": hora_brasilia_para_utc(a.get("inicio")),
                "fim_utc": hora_brasilia_para_utc(a.get("fim")),
                "cod_ibge": g,
            })
    cols = ["id_aviso", "tipo_aviso", "categoria", "severidade", "nivel_aviso", "inicio_utc", "fim_utc", "cod_ibge"]
    out = pd.DataFrame(linhas, columns=cols).drop_duplicates(["id_aviso", "cod_ibge"])
    t = pd.Timestamp(ref)
    out["ativo"] = (out["inicio_utc"] <= t) & (out["fim_utc"] >= t)
    out["origem"] = meta["origem"]
    return gravar_silver(out, "inmet_avisos", ref)


# ---------------------------------------------------------------- Cemaden
def tratar_cemaden(caminho: str, ref: datetime) -> str:
    meta, dados = ler_bronze(caminho)
    df = pd.DataFrame(dados)
    out = pd.DataFrame({
        "cod_pluviometro": _col(df, "codestacao", "cod_estacao", "codEstacao").astype("string"),
        "nome_pluviometro": _col(df, "nomeestacao", "nome", "nomeEstacao").astype("string").str.strip(),
        "cidade": _col(df, "cidade", "municipio").astype("string").str.strip(),
        "cod_ibge": _col(df, "codibge", "cod_ibge", "geocodigo").astype("string").str.replace(r"\D", "", regex=True),
        "latitude": num(_col(df, "latitude", "lat")),
        "longitude": num(_col(df, "longitude", "lon", "lng")),
        "ts_utc": pd.to_datetime(_col(df, "datahora", "dataHora", "data"), errors="coerce", utc=True),
        "chuva_1h_mm": num(_col(df, "acc1hr", "chuva1h")),
        "chuva_24h_mm": num(_col(df, "acc24hr", "chuva24h")),
        "chuva_72h_mm": num(_col(df, "acc72hr", "chuva72h")),
    })
    for c in ("chuva_1h_mm", "chuva_24h_mm", "chuva_72h_mm"):
        out.loc[(out[c] < 0) | (out[c] > 500), c] = np.nan
    # 72 h nunca pode ser menor que 24 h, nem 24 h menor que 1 h: corrige inconsistência do acumulado
    out["chuva_24h_mm"] = out[["chuva_24h_mm", "chuva_1h_mm"]].max(axis=1)
    out["chuva_72h_mm"] = out[["chuva_72h_mm", "chuva_24h_mm"]].max(axis=1)
    out = (out.dropna(subset=["cod_pluviometro", "latitude", "longitude"])
              .sort_values("ts_utc").drop_duplicates("cod_pluviometro", keep="last"))
    out["origem"] = meta["origem"]
    return gravar_silver(out.reset_index(drop=True), "cemaden_pluviometros", ref)


# ---------------------------------------------------------------- dimensão de regiões (referência)
def carregar_dim_regiao() -> pd.DataFrame:
    df = pd.read_csv(config.AMOSTRA / "cep_regioes.csv", dtype={"cep5": str, "cod_ibge": str})
    df["cep5"] = df["cep5"].str.zfill(5)
    return df


def tratar_dim_regiao(ref: datetime) -> str:
    return gravar_silver(carregar_dim_regiao(), "dim_regiao", ref)


# ---------------------------------------------------------------- cadastros + geocodificação do CEP
def _cache_cep() -> Path:
    return config.LAKE / "silver" / "cache_cep.parquet"


def _geocodificar_online(ceps: list[str]) -> pd.DataFrame:
    """BrasilAPI v2 devolve coordenadas para boa parte dos CEPs. Resultado fica em cache."""
    cache = pd.read_parquet(_cache_cep()) if _cache_cep().exists() else pd.DataFrame(
        columns=["cep", "latitude", "longitude"])
    faltam = [c for c in ceps if c not in set(cache["cep"])]
    novos = []
    if config.MODO != "offline":
        for cep in faltam[:200]:  # limite por execução para respeitar a API pública
            try:
                r = requests.get(config.CEP_API.format(cep=cep), timeout=config.TIMEOUT)
                coords = (r.json().get("location") or {}).get("coordinates") or {}
                if coords.get("latitude"):
                    novos.append({"cep": cep, "latitude": float(coords["latitude"]),
                                  "longitude": float(coords["longitude"])})
            except Exception as e:  # noqa: BLE001
                log.info("CEP %s sem coordenada online (%s); usa a tabela de regiões.", cep, type(e).__name__)
                break  # sem rede: não insiste nos demais
    if novos:
        cache = pd.concat([cache, pd.DataFrame(novos)], ignore_index=True).drop_duplicates("cep", keep="last")
        _cache_cep().parent.mkdir(parents=True, exist_ok=True)
        cache.to_parquet(_cache_cep(), index=False)
    return cache


def tratar_cadastros(caminho: str, ref: datetime) -> str:
    meta, dados = ler_bronze(caminho)
    df = pd.DataFrame(dados)
    out = pd.DataFrame({
        "id_cadastro": df["id"].astype("string"),
        "canal": df["canal"].astype("string").str.lower(),
        "cep": df["cep"].map(so_digitos).astype("string"),
        "bairro": df["bairro"].astype("string").str.strip().str.title(),
        "cidade": df["cidade"].astype("string").str.strip().str.title(),
        "uf": df["uf"].astype("string").str.upper(),
        "criado_em": pd.to_datetime(df["criado_em"], utc=True, errors="coerce"),
    })
    out = out[out["cep"].str.len() == 8].drop_duplicates("id_cadastro")
    out["cep5"] = out["cep"].str[:5]

    dim = carregar_dim_regiao()
    out = out.merge(dim[["cep5", "regiao", "cod_ibge", "latitude", "longitude"]], on="cep5", how="left")
    out["metodo_geo"] = np.where(out["latitude"].notna(), "tabela_cep5", None)

    # CEP fora da tabela de referência: tenta a coordenada exata online
    sem = out["latitude"].isna()
    if sem.any():
        cache = _geocodificar_online(sorted(out.loc[sem, "cep"].unique()))
        if not cache.empty:
            m = out.loc[sem, ["cep"]].merge(cache, on="cep", how="left")
            out.loc[sem, "latitude"] = m["latitude"].to_numpy()
            out.loc[sem, "longitude"] = m["longitude"].to_numpy()
            out.loc[sem & out["latitude"].notna(), "metodo_geo"] = "brasilapi"
    # último recurso: prefixo de 3 dígitos (mesma macro-região postal)
    sem = out["latitude"].isna()
    if sem.any():
        dim3 = dim.assign(cep3=dim["cep5"].str[:3]).groupby("cep3").agg(
            latitude=("latitude", "mean"), longitude=("longitude", "mean"),
            cod_ibge=("cod_ibge", "first"), regiao=("regiao", "first")).reset_index()
        m = out.loc[sem, ["cep"]].assign(cep3=lambda d: d["cep"].str[:3]).merge(dim3, on="cep3", how="left")
        for c in ("latitude", "longitude", "cod_ibge", "regiao"):
            out.loc[sem, c] = m[c].to_numpy()
        out.loc[sem & out["latitude"].notna(), "metodo_geo"] = "tabela_cep3"
    out["regiao"] = out["regiao"].fillna(out["bairro"])
    out["latitude"] = pd.to_numeric(out["latitude"], errors="coerce")
    out["longitude"] = pd.to_numeric(out["longitude"], errors="coerce")
    # CEP achado só online vem sem município: completa o código IBGE pelo nome da cidade
    por_cidade = dim.assign(c=dim["cidade"].map(sem_acento)).drop_duplicates("c").set_index("c")["cod_ibge"]
    out["cod_ibge"] = out["cod_ibge"].fillna(out["cidade"].map(lambda c: por_cidade.get(sem_acento(c))))
    out["origem"] = meta["origem"]
    return gravar_silver(out.reset_index(drop=True), "cadastros", ref)
