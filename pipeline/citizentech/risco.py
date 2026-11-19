"""Regras de negócio (camada gold): risco de enchente e de calor por CEP e fila de alertas.

Níveis (iguais para os dois riscos):
  0 normal · 1 atenção · 2 alerta · 3 alerta máximo
O MVP dispara mensagem a partir do nível 2 (config.NIVEL_MINIMO_ALERTA).
As faixas estão documentadas no PDF, seção "Regras de risco".
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger(__name__)
NOMES_NIVEL = {0: "normal", 1: "atenção", 2: "alerta", 3: "alerta máximo"}

# limiares de chuva (mm) -> nível. Referência: critérios usuais de Cemaden/Defesa Civil-SP para alagamento.
LIM_CHUVA_1H = [(40, 3), (20, 2), (10, 1)]
LIM_CHUVA_24H = [(80, 3), (50, 2), (30, 1)]
LIM_CHUVA_72H = [(150, 3), (100, 2), (70, 1)]
# temperatura máxima (°C) e índice de calor (sensação térmica, °C; faixas da NOAA)
LIM_TEMP_MAX = [(38, 3), (36, 2), (33, 1)]
LIM_INDICE_CALOR = [(54, 3), (41, 2), (32, 1)]


def nivel(valor: float, limiares: list[tuple[float, int]]) -> int:
    if valor is None or pd.isna(valor):
        return 0
    for lim, n in limiares:
        if valor >= lim:
            return n
    return 0


def indice_calor(temp_c: float, umidade: float) -> float:
    """Índice de calor de Rothfusz (NOAA), em °C. Abaixo de 27 °C devolve a própria temperatura."""
    if pd.isna(temp_c) or pd.isna(umidade):
        return np.nan
    if temp_c < 27:
        return float(temp_c)
    t, r = temp_c * 9 / 5 + 32, float(umidade)
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * r - .22475541 * t * r - .00683783 * t * t
          - .05481717 * r * r + .00122874 * t * t * r + .00085282 * t * r * r - .00000199 * t * t * r * r)
    if r < 13 and 80 <= t <= 112:
        hi -= ((13 - r) / 4) * np.sqrt((17 - abs(t - 95)) / 17)
    elif r > 85 and 80 <= t <= 87:
        hi += ((r - 85) / 10) * ((87 - t) / 5)
    return round((hi - 32) * 5 / 9, 1)


def distancia_km(lat1, lon1, lat2, lon2):
    """Haversine vetorizado."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(x, dtype=float)) for x in (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * np.arcsin(np.sqrt(a))


def combinar(medidas: dict[str, int], nivel_aviso: int, aviso: str | None, normal: str) -> tuple[int, str]:
    """Junta o que foi MEDIDO (Cemaden/INMET) com o que foi AVISADO (previsão do INMET).

    - só aviso, sem medição relevante: no máximo "atenção" (1); "Grande Perigo" sozinho vale "alerta" (2)
    - aviso "Perigo" ou "Grande Perigo" + medição já relevante: sobe um nível (previsão confirmada pelo sensor)
    Assim o alerta não dispara para a cidade inteira só porque há um aviso amplo do INMET,
    mas fica mais forte onde o sensor já confirma o problema.
    """
    n_med = max(medidas.values(), default=0)
    motivos = [k for k, v in medidas.items() if k and v == n_med and n_med > 0]
    n = max(n_med, 1 if nivel_aviso else 0)
    if nivel_aviso == 3:
        n = max(n, 2)
    if nivel_aviso >= 2 and n_med >= 1:
        n = min(3, n_med + 1)
    if nivel_aviso:
        motivos.append(f"aviso do INMET: {aviso}")
    return n, ("; ".join(motivos) if n > 0 else normal)


# ---------------------------------------------------------------- indicadores por estação INMET
def indicadores_estacoes(obs: pd.DataFrame, estacoes: pd.DataFrame, ref: datetime) -> pd.DataFrame:
    t = pd.Timestamp(ref)
    o = obs[obs["ts_utc"] <= t]
    linhas = []
    for cod, g in o.groupby("cd_estacao"):
        u24 = g[g["ts_utc"] > t - pd.Timedelta(hours=24)]
        ult = g.dropna(subset=["temp_c"]).tail(1)
        ic = [indice_calor(a, b) for a, b in zip(u24["temp_c"], u24["umidade_pct"])]
        linhas.append({
            "cd_estacao": cod,
            "ultima_obs_utc": g["ts_utc"].max(),
            "temp_atual_c": ult["temp_c"].iloc[0] if len(ult) else np.nan,
            "umidade_atual_pct": ult["umidade_pct"].iloc[0] if len(ult) else np.nan,
            "temp_max_24h_c": u24[["temp_max_c", "temp_c"]].max().max(),
            "indice_calor_max_24h_c": np.nanmax(ic) if len(ic) and not np.all(np.isnan(ic)) else np.nan,
            "chuva_1h_inmet_mm": g[g["ts_utc"] > t - pd.Timedelta(hours=1)]["chuva_mm"].sum(min_count=1),
            "chuva_24h_inmet_mm": u24["chuva_mm"].sum(min_count=1),
            "chuva_72h_inmet_mm": g[g["ts_utc"] > t - pd.Timedelta(hours=72)]["chuva_mm"].sum(min_count=1),
        })
    ind = pd.DataFrame(linhas)
    return estacoes[["cd_estacao", "nome_estacao", "latitude", "longitude"]].merge(ind, on="cd_estacao", how="inner")


# ---------------------------------------------------------------- risco para um conjunto de pontos
def calcular_risco_pontos(pontos: pd.DataFrame, ind_est: pd.DataFrame, pluv: pd.DataFrame,
                          avisos: pd.DataFrame) -> pd.DataFrame:
    """pontos: colunas latitude, longitude, cod_ibge (+ quaisquer colunas de identificação)."""
    p = pontos.dropna(subset=["latitude", "longitude"]).copy().reset_index(drop=True)
    if p.empty:
        return p

    # 1) estação INMET mais próxima (temperatura, umidade e chuva de referência)
    d = distancia_km(p["latitude"].to_numpy()[:, None], p["longitude"].to_numpy()[:, None],
                     ind_est["latitude"].to_numpy()[None, :], ind_est["longitude"].to_numpy()[None, :])
    idx = d.argmin(axis=1)
    est = ind_est.iloc[idx].reset_index(drop=True)
    p["cd_estacao_inmet"] = est["cd_estacao"]
    p["dist_estacao_km"] = d[np.arange(len(p)), idx].round(1)
    for c in ("temp_atual_c", "umidade_atual_pct", "temp_max_24h_c", "indice_calor_max_24h_c"):
        p[c] = est[c].to_numpy()

    # 2) chuva: pior pluviômetro do Cemaden num raio de N km (a chuva de verão é muito localizada);
    #    sem pluviômetro no raio, usa a chuva medida pela estação INMET mais próxima.
    if len(pluv):
        dp = distancia_km(p["latitude"].to_numpy()[:, None], p["longitude"].to_numpy()[:, None],
                          pluv["latitude"].to_numpy()[None, :], pluv["longitude"].to_numpy()[None, :])
        dentro = dp <= config.RAIO_PLUVIOMETRO_KM
    else:
        dp, dentro = np.zeros((len(p), 0)), np.zeros((len(p), 0), dtype=bool)
    for janela in ("1h", "24h", "72h"):
        col = f"chuva_{janela}_mm"
        if len(pluv):
            v = pluv[col].to_numpy(dtype=float)
            pior = pd.DataFrame(np.where(dentro, v[None, :], np.nan)).max(axis=1)  # NaN quando não há no raio
        else:
            pior = pd.Series(np.nan, index=p.index)
        p[col] = pior.fillna(pd.Series(est[f"chuva_{janela}_inmet_mm"].to_numpy(dtype=float))).round(1)
    p["pluviometros_no_raio"] = dentro.sum(axis=1)
    p["fonte_chuva"] = np.where(dentro.any(axis=1), "cemaden", "inmet")

    # 3) avisos ativos do INMET para o município do ponto
    ativos = avisos[avisos["ativo"]] if len(avisos) else avisos
    for cat in ("chuva", "calor"):
        a = (ativos[ativos["categoria"] == cat].sort_values("nivel_aviso")
             .groupby("cod_ibge").agg(nivel=("nivel_aviso", "max"), aviso=("tipo_aviso", "last"),
                                      sev=("severidade", "last")))
        m = p[["cod_ibge"]].merge(a, left_on="cod_ibge", right_index=True, how="left")
        p[f"nivel_aviso_{cat}"] = m["nivel"].fillna(0).astype(int).to_numpy()
        p[f"aviso_{cat}"] = np.where(m["aviso"].notna(), m["aviso"].astype(str) + " (" + m["sev"].astype(str) + ")",
                                     None)

    # 4) níveis e motivo legível
    niveis_ench, motivos_ench, niveis_cal, motivos_cal = [], [], [], []
    for r in p.itertuples():
        medidas = {
            f"{r.chuva_1h_mm:.0f} mm em 1 h" if pd.notna(r.chuva_1h_mm) else "": nivel(r.chuva_1h_mm, LIM_CHUVA_1H),
            f"{r.chuva_24h_mm:.0f} mm em 24 h" if pd.notna(r.chuva_24h_mm) else "": nivel(r.chuva_24h_mm, LIM_CHUVA_24H),
            f"{r.chuva_72h_mm:.0f} mm em 72 h" if pd.notna(r.chuva_72h_mm) else "": nivel(r.chuva_72h_mm, LIM_CHUVA_72H),
        }
        n, motivo = combinar(medidas, r.nivel_aviso_chuva, r.aviso_chuva, "sem chuva relevante")
        niveis_ench.append(n)
        motivos_ench.append(motivo)

        medidas = {
            f"máxima de {r.temp_max_24h_c:.1f} °C" if pd.notna(r.temp_max_24h_c) else "": nivel(r.temp_max_24h_c, LIM_TEMP_MAX),
            f"sensação térmica de {r.indice_calor_max_24h_c:.0f} °C" if pd.notna(r.indice_calor_max_24h_c) else "":
                nivel(r.indice_calor_max_24h_c, LIM_INDICE_CALOR),
        }
        n, motivo = combinar(medidas, r.nivel_aviso_calor, r.aviso_calor, "temperatura normal")
        niveis_cal.append(n)
        motivos_cal.append(motivo)

    p["nivel_enchente"], p["motivo_enchente"] = niveis_ench, motivos_ench
    p["nivel_calor"], p["motivo_calor"] = niveis_cal, motivos_cal
    p["nivel_geral"] = p[["nivel_enchente", "nivel_calor"]].max(axis=1)
    p["situacao"] = p["nivel_geral"].map(NOMES_NIVEL)
    p["deve_alertar"] = p["nivel_geral"] >= config.NIVEL_MINIMO_ALERTA
    return p


# ---------------------------------------------------------------- tabelas gold
COLS_RISCO = ["cep", "regiao", "cidade", "cod_ibge", "latitude", "longitude", "cadastros",
              "cd_estacao_inmet", "dist_estacao_km", "pluviometros_no_raio", "fonte_chuva",
              "chuva_1h_mm", "chuva_24h_mm", "chuva_72h_mm", "temp_atual_c", "umidade_atual_pct",
              "temp_max_24h_c", "indice_calor_max_24h_c", "aviso_chuva", "aviso_calor",
              "nivel_enchente", "motivo_enchente", "nivel_calor", "motivo_calor", "nivel_geral",
              "situacao", "deve_alertar", "referencia_utc", "atualizado_em_utc"]


def risco_por_cep(cad: pd.DataFrame, ind_est, pluv, avisos, ref: datetime) -> pd.DataFrame:
    pontos = (cad.dropna(subset=["latitude"])
              .groupby("cep").agg(regiao=("regiao", "first"), cidade=("cidade", "first"),
                                  cod_ibge=("cod_ibge", "first"), latitude=("latitude", "first"),
                                  longitude=("longitude", "first"), cadastros=("id_cadastro", "nunique"))
              .reset_index())
    r = calcular_risco_pontos(pontos, ind_est, pluv, avisos)
    r["referencia_utc"] = pd.Timestamp(ref)
    r["atualizado_em_utc"] = pd.Timestamp.now(tz="UTC").floor("s")
    return r[COLS_RISCO]


def risco_por_regiao(dim: pd.DataFrame, ind_est, pluv, avisos, ref: datetime) -> pd.DataFrame:
    """Mesmo cálculo sobre o centro de cada região de referência: alimenta o mapa do BI."""
    r = calcular_risco_pontos(dim.copy(), ind_est, pluv, avisos)
    r["referencia_utc"] = pd.Timestamp(ref)
    cols = ["cep5", "regiao", "cidade", "zona", "cod_ibge", "latitude", "longitude", "chuva_1h_mm", "chuva_24h_mm",
            "chuva_72h_mm", "temp_max_24h_c", "indice_calor_max_24h_c", "nivel_enchente", "nivel_calor",
            "nivel_geral", "situacao", "referencia_utc"]
    return r[cols]


MSG = {
    "enchente": ("Alerta de chuva forte ({nivel}) para o CEP {cep}, {regiao}: {motivo}. "
                 "Evite ruas que costumam alagar, não atravesse água corrente e tire objetos de valor do chão. "
                 "Emergência: Defesa Civil 199 / Bombeiros 193."),
    "calor": ("Alerta de calor ({nivel}) para o CEP {cep}, {regiao}: {motivo}. "
              "Beba água, evite sol entre 10h e 16h e procure sombra. Cuide de idosos, crianças e animais. "
              "Mal-estar forte: SAMU 192."),
}


def fila_alertas(cad: pd.DataFrame, risco: pd.DataFrame, ref: datetime) -> pd.DataFrame:
    linhas = []
    janela = pd.Timestamp(ref).floor(f"{config.JANELA_DEDUP_HORAS}h")
    alvo = risco[risco["deve_alertar"]]
    for c in cad.merge(alvo, on="cep", suffixes=("", "_r")).itertuples():
        for tipo in ("enchente", "calor"):
            n = getattr(c, f"nivel_{tipo}")
            if n < config.NIVEL_MINIMO_ALERTA:
                continue
            chave = f"{c.id_cadastro}|{tipo}|{n}|{janela.isoformat()}"
            linhas.append({
                "id_alerta": hashlib.sha1(chave.encode()).hexdigest()[:16],
                "id_cadastro": c.id_cadastro, "canal": c.canal, "cep": c.cep, "tipo": tipo, "nivel": n,
                "mensagem": MSG[tipo].format(nivel=NOMES_NIVEL[n], cep=f"{c.cep[:5]}-{c.cep[5:]}",
                                             regiao=c.regiao, motivo=getattr(c, f"motivo_{tipo}")),
                "janela_utc": janela, "status": "pendente",
                "criado_em_utc": pd.Timestamp.now(tz="UTC").floor("s"),
            })
    cols = ["id_alerta", "id_cadastro", "canal", "cep", "tipo", "nivel", "mensagem", "janela_utc", "status",
            "criado_em_utc"]
    return pd.DataFrame(linhas, columns=cols)
