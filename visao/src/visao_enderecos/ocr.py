"""Wrapper do Tesseract que devolve linhas com posição e confiança."""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pytesseract


@dataclass
class Linha:
    texto: str
    x: int
    y: int
    w: int
    h: int
    conf: float  # 0-100

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h


def ler_linhas(img: np.ndarray, lang: str = "por", psm: int = 4) -> list[Linha]:
    """Executa o OCR e agrupa as palavras em linhas (bloco/parágrafo/linha do Tesseract).

    Linhas que o Tesseract junta mas que estão separadas por um grande espaço
    horizontal (colunas diferentes) são quebradas em duas.
    """
    dados = pytesseract.image_to_data(img, lang=lang, config=f"--oem 1 --psm {psm}",
                                      output_type=pytesseract.Output.DICT)
    grupos: dict[tuple, list[int]] = {}
    for i, txt in enumerate(dados["text"]):
        if not txt.strip():
            continue
        chave = (dados["block_num"][i], dados["par_num"][i], dados["line_num"][i])
        grupos.setdefault(chave, []).append(i)

    linhas: list[Linha] = []
    for idx in grupos.values():
        idx.sort(key=lambda i: dados["left"][i])
        partes: list[list[int]] = [[idx[0]]]
        for i in idx[1:]:
            ant = partes[-1][-1]
            gap = dados["left"][i] - (dados["left"][ant] + dados["width"][ant])
            if gap > 2.2 * max(dados["height"][ant], 10):
                partes.append([i])
            else:
                partes[-1].append(i)
        for p in partes:
            xs = [dados["left"][i] for i in p]
            ys = [dados["top"][i] for i in p]
            x2 = [dados["left"][i] + dados["width"][i] for i in p]
            y2 = [dados["top"][i] + dados["height"][i] for i in p]
            confs = [float(dados["conf"][i]) for i in p if float(dados["conf"][i]) >= 0]
            texto = re.sub(r"\s+", " ", " ".join(dados["text"][i] for i in p)).strip()
            linhas.append(Linha(texto, min(xs), min(ys), max(x2) - min(xs), max(y2) - min(ys),
                                sum(confs) / len(confs) if confs else 0.0))
    linhas.sort(key=lambda l: (l.y, l.x))
    return linhas
