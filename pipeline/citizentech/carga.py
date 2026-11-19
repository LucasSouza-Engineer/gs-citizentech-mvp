"""Publicação (destinos do dado gold).

1. MVP (SQLite da API de cadastro): tabela risco_por_cep (substituída a cada execução) e
   fila_alertas (acrescenta; a chave id_alerta impede mandar o mesmo alerta duas vezes na janela).
2. Data warehouse (Postgres) para o BI: histórico de risco por CEP/região e fatos de medição.
3. Arquivos gold no data lake (Parquet + CSV) para auditoria e para quem quiser abrir no Excel/Power BI.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime

import pandas as pd

from . import config

log = logging.getLogger(__name__)


def gravar_gold(df: pd.DataFrame, tabela: str, ref: datetime) -> str:
    base = config.LAKE / "gold" / tabela
    p = base / f"dt={ref:%Y-%m-%d}" / f"{ref:%H%M}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    df.to_csv(base / "atual.csv", index=False, encoding="utf-8-sig")  # versão mais recente, fácil de abrir
    log.info("gold/%s: %d linhas -> %s", tabela, len(df), p)
    return str(p)


def _para_sql(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        elif pd.api.types.is_bool_dtype(out[c]):
            out[c] = out[c].astype(int)
    return out.astype(object).where(out.notna(), None)


def publicar_mvp(risco: pd.DataFrame, fila: pd.DataFrame) -> dict:
    db = config.CADASTRO_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as con, con:
        r = _para_sql(risco)
        con.execute("DROP TABLE IF EXISTS risco_por_cep_novo")
        r.to_sql("risco_por_cep_novo", con, index=False)
        # troca atômica: o MVP nunca lê uma tabela pela metade
        con.execute("DROP TABLE IF EXISTS risco_por_cep")
        con.execute("ALTER TABLE risco_por_cep_novo RENAME TO risco_por_cep")
        con.execute("CREATE INDEX IF NOT EXISTS ix_risco_cep ON risco_por_cep(cep)")

        con.execute("""CREATE TABLE IF NOT EXISTS fila_alertas (
            id_alerta TEXT PRIMARY KEY, id_cadastro TEXT, canal TEXT, cep TEXT, tipo TEXT, nivel INTEGER,
            mensagem TEXT, janela_utc TEXT, status TEXT, criado_em_utc TEXT, enviado_em_utc TEXT)""")
        antes = con.execute("SELECT COUNT(*) FROM fila_alertas").fetchone()[0]
        f = _para_sql(fila)
        con.executemany(
            "INSERT OR IGNORE INTO fila_alertas (id_alerta, id_cadastro, canal, cep, tipo, nivel, mensagem, "
            "janela_utc, status, criado_em_utc) VALUES (?,?,?,?,?,?,?,?,?,?)",
            f[["id_alerta", "id_cadastro", "canal", "cep", "tipo", "nivel", "mensagem", "janela_utc", "status",
               "criado_em_utc"]].itertuples(index=False, name=None))
        novos = con.execute("SELECT COUNT(*) FROM fila_alertas").fetchone()[0] - antes
    res = {"banco": str(db), "ceps": len(risco), "alertas_novos": novos, "alertas_repetidos": len(fila) - novos}
    log.info("MVP atualizado: %s", res)
    return res


def carregar_dw(tabelas: dict[str, pd.DataFrame], ref: datetime) -> dict:
    """Carga idempotente: apaga a mesma referência antes de inserir (rodar de novo não duplica)."""
    if not config.DW_URL:
        log.warning("DW_URL vazio: carga no data warehouse pulada.")
        return {"dw": "pulado"}
    from sqlalchemy import create_engine, inspect, text

    eng = create_engine(config.DW_URL)
    res = {}
    with eng.begin() as con:
        con.execute(text("CREATE SCHEMA IF NOT EXISTS dw"))
        insp = inspect(con)
        for nome, df in tabelas.items():
            df = df.copy()
            df["referencia_utc"] = pd.Timestamp(ref)
            if insp.has_table(nome, schema="dw"):
                con.execute(text(f"DELETE FROM dw.{nome} WHERE referencia_utc = :r"), {"r": pd.Timestamp(ref)})
            df.to_sql(nome, con, schema="dw", if_exists="append", index=False)
            res[nome] = len(df)
    log.info("DW carregado: %s", res)
    return res
