"""Configuração única do pipeline, lida de variáveis de ambiente (docker-compose / .env)."""
from __future__ import annotations

import os
from pathlib import Path

# Raiz do projeto: /opt/airflow no container, a pasta do repositório quando roda local.
RAIZ = Path(os.getenv("PIPELINE_RAIZ", Path(__file__).resolve().parents[2]))

LAKE = Path(os.getenv("PIPELINE_LAKE", RAIZ / "dados" / "lake"))
AMOSTRA = Path(os.getenv("PIPELINE_AMOSTRA", RAIZ / "dados" / "amostra"))

# auto   = tenta a fonte real e, se falhar, usa a amostra local (padrão)
# online = só fonte real (falha se a fonte cair)
# offline= só amostra local (apresentação sem internet)
MODO = os.getenv("PIPELINE_MODO", "auto").lower()

# Banco do MVP (o mesmo SQLite da API de cadastro do componente de Visão).
CADASTRO_DB = Path(os.getenv("CADASTRO_DB", RAIZ / "dados" / "mvp" / "cadastros.sqlite3"))

# Data warehouse para o BI (Postgres). Vazio = pula a carga no DW.
DW_URL = os.getenv("DW_URL", "")

# Fontes reais
INMET_TOKEN = os.getenv("INMET_TOKEN", "")
INMET_API = os.getenv("INMET_API", "https://apitempo.inmet.gov.br")
INMET_AVISOS_URL = os.getenv("INMET_AVISOS_URL", "https://apiprevmet3.inmet.gov.br/avisos/ativos")
CEMADEN_URL = os.getenv("CEMADEN_URL",
                        "http://sjc.salvar.cemaden.gov.br/resources/graficos/interativo/getJson2.php?uf={uf}")
CEP_API = os.getenv("CEP_API", "https://brasilapi.com.br/api/cep/v2/{cep}")
UF = os.getenv("PIPELINE_UF", "SP")
TIMEOUT = int(os.getenv("PIPELINE_TIMEOUT", "20"))

# Regras de negócio
RAIO_PLUVIOMETRO_KM = float(os.getenv("RAIO_PLUVIOMETRO_KM", "5"))
JANELA_DEDUP_HORAS = int(os.getenv("JANELA_DEDUP_HORAS", "6"))
NIVEL_MINIMO_ALERTA = int(os.getenv("NIVEL_MINIMO_ALERTA", "2"))
