"""Serviço de alertas do MVP: liga o cadastro (componente de Visão) ao pipeline de risco.

- rodar_ciclo(): roda o pipeline (bronze -> silver -> gold -> qualidade -> publica no SQLite)
  e em seguida dispara os alertas pendentes da fila.
- disparar_pendentes(): lê fila_alertas (status 'pendente'), busca o contato no cadastro
  e envia por WhatsApp/SMS. Sem provedor configurado, o envio é SIMULADO e fica registrado.
- risco_do_cep(): risco de um CEP qualquer, mesmo antes de ele ter cadastro (usa os
  indicadores da última execução do pipeline e as mesmas regras da camada gold).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone

import pandas as pd
import requests

from citizentech import config, etapas, fontes, risco, transformacao

log = logging.getLogger("mvp.alertas")
_trava = threading.Lock()
ESTADO: dict = {"ultima_execucao": None, "resultado": None, "erro": None, "silver": None, "gold": None,
                "origem_dados": None, "em_execucao": False}

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_SMS_FROM = os.getenv("TWILIO_SMS_FROM", "")            # ex.: +15005550006
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")  # ex.: whatsapp:+14155238886 (sandbox)


def _con() -> sqlite3.Connection:
    config.CADASTRO_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.CADASTRO_DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _tem_tabela(con, nome: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def preparar_banco() -> None:
    with closing(_con()) as con, con:
        con.execute("""CREATE TABLE IF NOT EXISTS envios (
            id_alerta TEXT, id_cadastro TEXT, canal TEXT, contato_mascarado TEXT, provedor TEXT,
            status TEXT, detalhe TEXT, enviado_em_utc TEXT)""")


# ------------------------------------------------------------------ cadastros de demonstração
def semear_demo() -> int:
    """Grava os cadastros sintéticos do pipeline (origem='demo') se o banco ainda não tem nenhum.
    Para esses cadastros o envio é SEMPRE simulado, mesmo com provedor real configurado."""
    import json
    cad = json.loads((config.AMOSTRA / "cadastros_exemplo.json").read_text(encoding="utf-8"))
    cols = ["id", "criado_em", "canal", "contato", "logradouro", "numero", "complemento", "bairro", "cidade", "uf",
            "cep", "origem", "empresa_documento", "campos_corrigidos", "confianca_ocr"]
    from api.app import _db  # garante a tabela com o esquema da API de cadastro
    with closing(_db()) as con, con:
        if con.execute("SELECT COUNT(*) FROM cadastros").fetchone()[0]:
            return 0
        con.executemany(f"INSERT OR IGNORE INTO cadastros VALUES ({','.join('?' * len(cols))})",
                        [tuple("demo" if k == "origem" else c[k] for k in cols) for c in cad])
    log.info("%d cadastros de demonstração gravados", len(cad))
    return len(cad)


# ------------------------------------------------------------------ envio
def _mascarar(contato: str) -> str:
    return f"({contato[:2]}) *****-{contato[-4:]}" if contato else "?"


def _enviar(canal: str, contato: str, mensagem: str, real: bool) -> tuple[str, str, str]:
    """Devolve (provedor, status, detalhe)."""
    remetente = TWILIO_WHATSAPP_FROM if canal == "whatsapp" else TWILIO_SMS_FROM
    if not (real and TWILIO_SID and TWILIO_TOKEN and remetente):
        return "simulado", "enviado", "envio simulado (sem provedor configurado)"
    destino = f"+55{contato}"
    if canal == "whatsapp":
        destino = f"whatsapp:{destino}"
    try:
        r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                          data={"From": remetente, "To": destino, "Body": mensagem},
                          auth=(TWILIO_SID, TWILIO_TOKEN), timeout=20)
        if r.status_code < 300:
            return "twilio", "enviado", r.json().get("sid", "")
        return "twilio", "falhou", f"HTTP {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        return "twilio", "falhou", type(e).__name__


def disparar_pendentes(limite: int = 500) -> dict:
    preparar_banco()
    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    enviados = falhas = 0
    with closing(_con()) as con:
        if not _tem_tabela(con, "fila_alertas"):
            return {"enviados": 0, "falhas": 0}
        pend = con.execute("""SELECT f.id_alerta, f.id_cadastro, f.canal, f.mensagem, c.contato, c.origem
                              FROM fila_alertas f JOIN cadastros c ON c.id = f.id_cadastro
                              WHERE f.status = 'pendente' ORDER BY f.nivel DESC LIMIT ?""", (limite,)).fetchall()
        for a in pend:
            provedor, status, detalhe = _enviar(a["canal"], a["contato"], a["mensagem"], real=a["origem"] != "demo")
            with con:
                con.execute("UPDATE fila_alertas SET status=?, enviado_em_utc=? WHERE id_alerta=?",
                            (status, agora, a["id_alerta"]))
                con.execute("INSERT INTO envios VALUES (?,?,?,?,?,?,?,?)",
                            (a["id_alerta"], a["id_cadastro"], a["canal"], _mascarar(a["contato"]), provedor, status,
                             detalhe, agora))
            enviados += status == "enviado"
            falhas += status != "enviado"
        # alertas de cadastros excluídos (LGPD) não são enviados
        with con:
            con.execute("""UPDATE fila_alertas SET status='cancelado' WHERE status='pendente'
                           AND id_cadastro NOT IN (SELECT id FROM cadastros)""")
    if enviados or falhas:
        log.info("disparo: %d enviados, %d falhas", enviados, falhas)
    return {"enviados": enviados, "falhas": falhas}


# ------------------------------------------------------------------ ciclo completo
def rodar_ciclo() -> dict:
    if not _trava.acquire(blocking=False):
        return {"status": "já em execução"}
    ESTADO["em_execucao"] = True
    try:
        res = etapas.rodar_tudo(com_dw=bool(config.DW_URL))
        ESTADO.update(ultima_execucao=datetime.now(timezone.utc).isoformat(timespec="seconds"), erro=None,
                      silver=res["silver"], gold=res["gold"],
                      origem_dados=_origens(res["bronze"]),
                      resultado={"referencia": res["referencia"], "qualidade": res["qualidade"], "mvp": res["mvp"]})
        ESTADO["resultado"]["disparo"] = disparar_pendentes()
        return ESTADO["resultado"]
    except Exception as e:  # noqa: BLE001 - o app segue no ar com a última tabela boa
        log.exception("pipeline falhou")
        ESTADO.update(ultima_execucao=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      erro=f"{type(e).__name__}: {e}")
        return {"status": "erro", "erro": ESTADO["erro"]}
    finally:
        ESTADO["em_execucao"] = False
        _trava.release()


def _origens(bronze: dict) -> dict:
    import json
    from pathlib import Path
    out = {}
    for fonte, caminho in bronze.items():
        try:
            out[fonte] = json.loads(Path(caminho).read_text(encoding="utf-8"))["origem"]
        except Exception:  # noqa: BLE001
            out[fonte] = "?"
    return out


def rodar_em_segundo_plano() -> None:
    threading.Thread(target=rodar_ciclo, daemon=True, name="ciclo-alertas").start()


def agendar(intervalo_min: int) -> None:
    """Agendador simples dentro do app (o Airflow faz o mesmo papel no ambiente de dados)."""
    import time

    def laco():
        while True:
            rodar_ciclo()
            time.sleep(intervalo_min * 60)
    threading.Thread(target=laco, daemon=True, name="agendador").start()


# ------------------------------------------------------------------ consultas
def _linhas(sql: str, args=()) -> list[dict]:
    with closing(_con()) as con:
        tabelas = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        precisa = {t for t in ("risco_por_cep", "fila_alertas", "envios", "cadastros") if t in sql}
        if not precisa <= tabelas:
            return []
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def risco_do_cep(cep: str) -> dict | None:
    """Primeiro procura na tabela publicada pelo pipeline; senão calcula na hora com os mesmos dados."""
    cep = transformacao.so_digitos(cep)
    if len(cep) != 8:
        return None
    r = _linhas("SELECT * FROM risco_por_cep WHERE cep = ?", (cep,))
    if r:
        return {**r[0], "calculado": "pipeline"}
    if not ESTADO.get("silver") or not ESTADO.get("gold"):
        return None
    s, g = ESTADO["silver"], ESTADO["gold"]
    dim = transformacao.carregar_dim_regiao()
    linha = dim[dim["cep5"] == cep[:5]]
    if linha.empty:  # mesma regra de último recurso da camada silver: prefixo de 3 dígitos
        linha = dim[dim["cep5"].str[:3] == cep[:3]]
        if linha.empty:
            return None
        linha = pd.DataFrame([{"regiao": linha["regiao"].iloc[0], "cidade": linha["cidade"].iloc[0],
                               "cod_ibge": linha["cod_ibge"].iloc[0], "latitude": linha["latitude"].mean(),
                               "longitude": linha["longitude"].mean()}])
    p = linha[["regiao", "cidade", "cod_ibge", "latitude", "longitude"]].head(1).assign(cep=cep)
    calc = risco.calcular_risco_pontos(p, pd.read_parquet(g["indicadores_estacao"]),
                                       pd.read_parquet(s["cemaden"]), pd.read_parquet(s["inmet_avisos"]))
    if calc.empty:
        return None
    d = calc.iloc[0].to_dict()
    d = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in d.items()}
    for k, v in list(d.items()):
        if hasattr(v, "item"):
            d[k] = v.item()
    return {**d, "calculado": "na hora"}


def regioes() -> list[dict]:
    if not ESTADO.get("gold"):
        return []
    df = pd.read_parquet(ESTADO["gold"]["risco_por_regiao"]).drop(columns=["referencia_utc"])
    df = df.sort_values(["nivel_geral", "chuva_1h_mm"], ascending=False)
    return df.astype(object).where(df.notna(), None).to_dict("records")


def resumo() -> dict:
    cad = _linhas("SELECT origem, COUNT(*) n FROM cadastros GROUP BY origem")
    ceps = _linhas("SELECT COUNT(*) n, SUM(deve_alertar) a FROM risco_por_cep")
    env = _linhas("SELECT provedor, status, COUNT(*) n FROM envios GROUP BY provedor, status")
    return {"cadastros": {c["origem"]: c["n"] for c in cad},
            "ceps_monitorados": ceps[0]["n"] if ceps else 0,
            "ceps_em_alerta": (ceps[0]["a"] or 0) if ceps else 0,
            "envios": env, "pipeline": {k: ESTADO[k] for k in ("ultima_execucao", "erro", "origem_dados",
                                                              "em_execucao", "resultado")}}


def alertas_recentes(limite: int = 30) -> list[dict]:
    return _linhas("""SELECT f.tipo, f.nivel, f.cep, f.canal, f.mensagem, f.status, f.enviado_em_utc,
                             e.contato_mascarado, e.provedor
                      FROM fila_alertas f LEFT JOIN envios e ON e.id_alerta = f.id_alerta
                      ORDER BY COALESCE(f.enviado_em_utc, f.criado_em_utc) DESC LIMIT ?""", (limite,))


def alertas_do_cadastro(ident: str) -> list[dict]:
    return _linhas("""SELECT f.tipo, f.nivel, f.mensagem, f.status, f.criado_em_utc, f.enviado_em_utc, e.provedor
                      FROM fila_alertas f LEFT JOIN envios e ON e.id_alerta = f.id_alerta
                      WHERE f.id_cadastro = ? ORDER BY f.criado_em_utc DESC""", (ident,))


def excluir_alertas(ident: str) -> None:
    """LGPD: ao excluir o cadastro, apaga também a fila e o histórico de envio dele."""
    with closing(_con()) as con, con:
        for t in ("fila_alertas", "envios"):
            if _tem_tabela(con, t):
                con.execute(f"DELETE FROM {t} WHERE id_cadastro = ?", (ident,))


__all__ = ["ESTADO", "agendar", "alertas_do_cadastro", "alertas_recentes", "disparar_pendentes", "excluir_alertas",
           "fontes", "preparar_banco", "regioes", "resumo", "risco_do_cep", "rodar_ciclo", "rodar_em_segundo_plano",
           "semear_demo"]
