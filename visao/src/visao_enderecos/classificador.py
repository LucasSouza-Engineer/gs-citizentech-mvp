"""Identificação da empresa emissora (Enel, Sabesp, Claro).

Combina duas evidências independentes:
  * texto: palavras-chave características encontradas pelo OCR (peso maior);
  * cor: matiz dominante da faixa de cabeçalho (roxo Enel, azul Sabesp, vermelho Claro).
Assim a classificação continua funcionando se o OCR do logotipo falhar.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .endereco import normalizar_comparacao

PALAVRAS = {
    "enel": ["enel", "energia eletrica", "unidade consumidora", "kwh", "bandeira tarifaria", "instalacao n"],
    "sabesp": ["sabesp", "agua e esgoto", "rgi", "hidrometro", "saneamento", "m3", "endereco do imovel"],
    "claro": ["claro", "fatura claro", "internet", "endereco de instalacao", "endereco de cobranca", "claro tv"],
}
# Matiz no OpenCV (0-179) esperado para o cabeçalho de cada empresa
MATIZ = {"enel": 140, "sabesp": 105, "claro": 2}


@dataclass
class Classificacao:
    empresa: str | None
    confianca: float
    pontuacoes: dict = field(default_factory=dict)


def _pont_cor(doc_bgr: np.ndarray) -> dict[str, float]:
    h = doc_bgr.shape[0]
    cab = doc_bgr[: max(1, int(h * 0.10))]
    hsv = cv2.cvtColor(cab, cv2.COLOR_BGR2HSV)
    sat = hsv[..., 1] > 70
    if sat.mean() < 0.05:  # cabeçalho sem cor (ex.: cópia em P&B)
        return {k: 0.0 for k in MATIZ}
    matizes = hsv[..., 0][sat].astype(np.float32)
    res = {}
    for emp, alvo in MATIZ.items():
        dist = np.minimum(np.abs(matizes - alvo), 180 - np.abs(matizes - alvo))
        res[emp] = float((dist < 12).mean())
    return res


def classificar(doc_bgr: np.ndarray, texto_ocr: str) -> Classificacao:
    t = normalizar_comparacao(texto_ocr)
    pont_txt = {emp: sum(1 for p in pals if p in t) / len(pals) for emp, pals in PALAVRAS.items()}
    pont_cor = _pont_cor(doc_bgr)
    total = {emp: 0.7 * pont_txt[emp] + 0.3 * pont_cor[emp] for emp in PALAVRAS}
    ordem = sorted(total, key=total.get, reverse=True)
    melhor, segundo = ordem[0], ordem[1]
    if total[melhor] < 0.15:
        return Classificacao(None, 0.0, total)
    conf = min(1.0, total[melhor] / (total[melhor] + total[segundo] + 1e-6))
    return Classificacao(melhor, round(conf, 3), {k: round(v, 3) for k, v in total.items()})
