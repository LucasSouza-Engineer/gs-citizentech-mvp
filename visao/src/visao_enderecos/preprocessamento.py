"""Pré-processamento do documento retificado para OCR."""
from __future__ import annotations

import cv2
import numpy as np


def normalizar_iluminacao(cinza: np.ndarray) -> np.ndarray:
    """Remove sombras e gradientes dividindo pela estimativa do fundo (fechamento morfológico)."""
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31))
    fundo = cv2.morphologyEx(cinza, cv2.MORPH_CLOSE, k)
    fundo = cv2.GaussianBlur(fundo, (0, 0), 9)
    norm = cv2.divide(cinza, fundo, scale=255)
    return norm


def remover_linhas(cinza: np.ndarray) -> np.ndarray:
    """Apaga linhas longas horizontais/verticais (molduras de caixas e tabelas).

    Bordas de caixa coladas no texto fazem o Tesseract perder a 1ª letra da
    linha ("Avenida" -> "venida"); abrir a imagem com elementos estruturantes
    longos isola só as linhas, que são pintadas com a cor do papel."""
    inv = 255 - cv2.threshold(cinza, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    h, w = cinza.shape
    horiz = cv2.morphologyEx(inv, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, w // 12), 1)))
    vert = cv2.morphologyEx(inv, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(40, h // 20))))
    linhas = cv2.bitwise_or(horiz, vert)
    # só traços finos: faixas grossas (texto borrado, cabeçalhos) não são molduras
    n, rot, stats, _ = cv2.connectedComponentsWithStats(linhas)
    for i in range(1, n):
        x, y, cw, ch = stats[i, :4]
        if min(cw, ch) > 6:
            linhas[rot == i] = 0
    linhas = cv2.dilate(linhas, np.ones((3, 3), np.uint8))
    out = cinza.copy()
    out[linhas > 0] = 255
    return out


def preprocessar(doc_bgr: np.ndarray, binarizar: bool = False) -> np.ndarray:
    """Retorna imagem em tons de cinza limpa (ou binária) pronta para o Tesseract.

    Etapas: cinza -> remoção de ruído (Non-Local Means) -> normalização de iluminação
    -> realce de contraste (CLAHE) -> (opcional) binarização Otsu.
    O Tesseract 5 (LSTM) costuma ir melhor com cinza limpo do que com binário
    agressivo; a binarização fica como segunda tentativa.
    """
    cinza = cv2.cvtColor(doc_bgr, cv2.COLOR_BGR2GRAY) if doc_bgr.ndim == 3 else doc_bgr
    # o ruído é removido ANTES do realce de contraste: CLAHE sobre ruído vira "textura" no papel
    suave = cv2.fastNlMeansDenoising(cinza, None, h=9, templateWindowSize=7, searchWindowSize=21)
    norm = normalizar_iluminacao(suave)
    clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
    limpo = clahe.apply(norm)
    if binarizar:
        _, limpo = cv2.threshold(limpo, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return limpo


def nitidez(cinza: np.ndarray) -> float:
    """Variância do Laplaciano: valores baixos indicam foto tremida/desfocada."""
    return float(cv2.Laplacian(cinza, cv2.CV_64F).var())
