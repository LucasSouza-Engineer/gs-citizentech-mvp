"""Testes de ponta a ponta do MVP: cadastro -> pipeline -> risco -> alerta -> exclusão (LGPD)."""
import os
import sys
import time
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cliente(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mvp")
    os.environ.update(CADASTRO_DB=str(tmp / "cad.sqlite3"), PIPELINE_LAKE=str(tmp / "lake"),
                      PIPELINE_MODO="offline", PIPELINE_AGENDADO="0", DEMO_SEED="1", ADMIN_TOKEN="segredo")
    sys.path.insert(0, str(RAIZ))
    from fastapi.testclient import TestClient

    from mvp.main import app
    with TestClient(app) as c:
        r = c.post("/api/v1/pipeline/executar", headers={"x-admin-token": "segredo"})
        assert r.status_code == 200, r.text
        yield c


def test_pipeline_publica_risco_e_dispara(cliente):
    p = cliente.get("/api/v1/painel").json()
    assert p["resumo"]["pipeline"]["erro"] is None
    assert p["resumo"]["pipeline"]["resultado"]["qualidade"]["erros"] == 0
    assert p["resumo"]["ceps_monitorados"] > 0
    assert any(a["status"] == "enviado" for a in p["alertas"])


def test_executar_exige_token(cliente):
    assert cliente.post("/api/v1/pipeline/executar").status_code == 401


def test_risco_de_cep_sem_cadastro_calculado_na_hora(cliente):
    r = cliente.get("/api/v1/risco/08226-000")
    assert r.status_code == 200
    j = r.json()
    assert j["regiao"] == "Itaquera" and j["nivel_enchente"] in (0, 1, 2, 3)
    assert cliente.get("/api/v1/risco/123").status_code == 404


def test_regioes(cliente):
    reg = cliente.get("/api/v1/regioes").json()
    assert len(reg) > 10 and {"nivel_enchente", "nivel_calor"} <= reg[0].keys()


def test_fluxo_cidadao(cliente):
    corpo = {"endereco": {"logradouro": "Rua Teste", "numero": "10", "bairro": "Itaquera", "cidade": "São Paulo",
                          "uf": "SP", "cep": "08220-010"},
             "canal": "whatsapp", "contato": "(11) 91234-5678", "consentimento": True, "origem": "manual"}
    r = cliente.post("/api/v1/cadastros", json=corpo)
    assert r.status_code == 201
    ident = r.json()["id"]
    # o cadastro dispara o pipeline em segundo plano; espera o alerta aparecer
    for _ in range(60):
        j = cliente.get(f"/api/v1/cadastros/{ident}/alertas").json()
        if j["alertas"]:
            break
        time.sleep(0.5)
    assert j["risco"]["calculado"] == "pipeline"
    assert j["alertas"] and j["alertas"][0]["status"] == "enviado"
    assert "5678" in j["cadastro"]["contato"] and "91234" not in j["cadastro"]["contato"]
    # LGPD: excluir apaga cadastro e alertas
    assert cliente.delete(f"/api/v1/cadastros/{ident}").status_code == 204
    assert cliente.get(f"/api/v1/cadastros/{ident}/alertas").status_code == 404


def test_paginas(cliente):
    assert "cadastro-extra.js" in cliente.get("/").text
    for p in ("/painel", "/meu", "/mvp/painel.html", "/api/v1/saude"):
        assert cliente.get(p).status_code == 200, p
