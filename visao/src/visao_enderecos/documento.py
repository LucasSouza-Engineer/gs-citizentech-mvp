"""Detecção do documento na foto e correção de perspectiva (homografia)."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

LARGURA_SAIDA = 1240  # A4 a ~150 dpi
PROPORCAO_A4 = 297 / 210


@dataclass
class Deteccao:
    cantos: np.ndarray | None  # 4x2 (tl, tr, br, bl) em coordenadas da imagem original
    metodo: str
    area_relativa: float


def ordenar_cantos(pts: np.ndarray) -> np.ndarray:
    """Ordena 4 pontos como topo-esq, topo-dir, baixo-dir, baixo-esq."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def _quadrilatero(contorno: np.ndarray) -> np.ndarray | None:
    hull = cv2.convexHull(contorno)
    peri = cv2.arcLength(hull, True)
    for eps in (0.01, 0.02, 0.03, 0.04, 0.05, 0.07):
        aprox = cv2.approxPolyDP(hull, eps * peri, True)
        if len(aprox) == 4:
            return aprox.reshape(4, 2).astype(np.float32)
    # fallback: retângulo mínimo rotacionado
    return cv2.boxPoints(cv2.minAreaRect(hull)).astype(np.float32)


def _mascara_papel(img: np.ndarray) -> np.ndarray:
    """Papel = região clara e pouco saturada. Normaliza iluminação antes de limiarizar."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    s, v = hsv[..., 1].astype(np.float32), hsv[..., 2].astype(np.float32)
    score = v - 1.2 * s
    score = cv2.normalize(score, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    score = cv2.GaussianBlur(score, (7, 7), 0)
    _, m = cv2.threshold(score, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k, iterations=2)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    return m


def _mascara_fundo(img: np.ndarray) -> np.ndarray:
    """Documento = tudo que difere da cor do fundo, estimada pela moldura da foto.

    Inclui cabeçalhos coloridos (que a máscara de 'papel branco' perderia)."""
    lab = cv2.cvtColor(cv2.GaussianBlur(img, (9, 9), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
    h, w = lab.shape[:2]
    b = max(4, int(0.03 * min(h, w)))
    moldura = np.concatenate([lab[:b].reshape(-1, 3), lab[-b:].reshape(-1, 3),
                              lab[:, :b].reshape(-1, 3), lab[:, -b:].reshape(-1, 3)])
    ref = np.median(moldura, axis=0)
    dist = np.linalg.norm(lab - ref, axis=2)
    ruido = np.percentile(np.linalg.norm(moldura - ref, axis=1), 98)
    # limiar absoluto (variação natural do fundo + margem): Otsu falharia com cabeçalhos escuros
    m = (dist > max(1.3 * ruido, 14.0)).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k, iterations=3)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    # preenche buracos internos (texto escuro parecido com o fundo)
    cont, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cheia = np.zeros_like(m)
    cv2.drawContours(cheia, cont, -1, 255, thickness=cv2.FILLED)
    return cheia


def _mascara_grabcut(img: np.ndarray) -> np.ndarray:
    """GrabCut: moldura da foto = fundo certo, centro = documento certo, resto = a decidir.

    Aprende modelos de cor (GMM) do fundo e do documento, então funciona mesmo
    quando o fundo tem cor parecida com o cabeçalho ou há sombra forte."""
    h, w = img.shape[:2]
    esc = 400 / max(h, w)
    peq = cv2.resize(img, None, fx=esc, fy=esc, interpolation=cv2.INTER_AREA)
    ph, pw = peq.shape[:2]
    mask = np.full((ph, pw), cv2.GC_PR_FGD, np.uint8)
    b = max(3, int(0.025 * min(ph, pw)))
    mask[:b], mask[-b:], mask[:, :b], mask[:, -b:] = cv2.GC_BGD, cv2.GC_BGD, cv2.GC_BGD, cv2.GC_BGD
    cy, cx = ph // 2, pw // 2
    mask[cy - ph // 6:cy + ph // 6, cx - pw // 6:cx + pw // 6] = cv2.GC_FGD
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(peq, mask, None, bgd, fgd, 4, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return np.zeros((h, w), np.uint8)
    m = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    return cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)


def _fora_eh_fundo(img: np.ndarray, quad: np.ndarray, dist: int = 8) -> float:
    """Fração de pontos logo FORA do quadrilátero que parecem fundo.

    Se a borda detectada cortou um pedaço do documento (ex.: cabeçalho colorido),
    o que está 'fora' ainda é documento e essa fração cai."""
    lab = cv2.cvtColor(cv2.GaussianBlur(img, (7, 7), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
    h, w = lab.shape[:2]
    b = max(4, int(0.03 * min(h, w)))
    moldura = np.concatenate([lab[:b].reshape(-1, 3), lab[-b:].reshape(-1, 3),
                              lab[:, :b].reshape(-1, 3), lab[:, -b:].reshape(-1, 3)])
    ref = np.median(moldura, axis=0)
    limiar = max(1.3 * np.percentile(np.linalg.norm(moldura - ref, axis=1), 98), 14.0)
    centro = quad.mean(axis=0)
    total = fundo = 0
    for i in range(4):
        a, c = quad[i], quad[(i + 1) % 4]
        normal = np.array([c[1] - a[1], a[0] - c[0]], np.float32)
        normal /= np.linalg.norm(normal) + 1e-6
        meio = (a + c) / 2
        if np.dot(meio - centro, normal) < 0:
            normal = -normal
        for t in np.linspace(0.1, 0.9, 25):
            x, y = (a + t * (c - a) + dist * normal).astype(int)
            if 0 <= x < w and 0 <= y < h:
                total += 1
                fundo += np.linalg.norm(lab[y, x] - ref) < limiar
            else:
                total += 1
                fundo += 1  # fora da foto conta como fundo
    return fundo / total if total else 0.0


def _suporte_bordas(cinza: np.ndarray, quad: np.ndarray) -> float:
    """Fração do perímetro do quadrilátero que coincide com bordas reais da imagem."""
    bordas = cv2.Canny(cv2.GaussianBlur(cinza, (5, 5), 0), 30, 90)
    bordas = cv2.dilate(bordas, np.ones((5, 5), np.uint8))
    h, w = cinza.shape
    total = acertos = 0
    for i in range(4):
        a, b = quad[i], quad[(i + 1) % 4]
        n = max(2, int(np.linalg.norm(b - a) / 3))
        for t in np.linspace(0.05, 0.95, n):
            x, y = (a + t * (b - a)).astype(int)
            if 2 <= x < w - 2 and 2 <= y < h - 2:
                total += 1
                acertos += bordas[y, x] > 0
    return acertos / total if total else 0.0


def _mascara_bordas(img: np.ndarray) -> np.ndarray:
    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cinza = cv2.GaussianBlur(cinza, (5, 5), 0)
    mediana = np.median(cinza)
    bordas = cv2.Canny(cinza, int(max(0, 0.66 * mediana)), int(min(255, 1.33 * mediana)))
    bordas = cv2.dilate(bordas, np.ones((5, 5), np.uint8), iterations=2)
    return bordas


def _moldura_eh_papel(img: np.ndarray) -> bool:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    b = max(4, int(0.02 * min(img.shape[:2])))
    moldura = np.concatenate([hsv[:b].reshape(-1, 3), hsv[-b:].reshape(-1, 3),
                              hsv[:, :b].reshape(-1, 3), hsv[:, -b:].reshape(-1, 3)])
    papel = (moldura[:, 2] > 200) & (moldura[:, 1] < 40)
    return float(papel.mean()) > 0.7


def detectar_documento(img: np.ndarray, area_minima: float = 0.15) -> Deteccao:
    """Procura o quadrilátero do papel na foto.

    Estratégias concorrentes (distância de cor ao fundo e GrabCut) geram
    candidatos; vence o de maior nota (bordas reais x retangularidade x
    'fora é fundo'). Canny e 'papel claro' ficam como último recurso.
    Se a própria moldura da imagem já é papel (digitalização ou print já
    recortado), não há nada a retificar e devolve cantos=None.
    """
    h, w = img.shape[:2]
    escala = 800 / max(h, w)
    peq = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA) if escala < 1 else img.copy()
    escala = min(escala, 1.0)
    area_img = peq.shape[0] * peq.shape[1]

    if _moldura_eh_papel(peq):
        return Deteccao(None, "ja_recortada", 1.0)

    cinza = cv2.cvtColor(peq, cv2.COLOR_BGR2GRAY)
    melhores: list[tuple[float, np.ndarray, str]] = []
    # a máscara "papel claro" fica só como último recurso: ela corta cabeçalhos coloridos
    # "bordas" (Canny) tende a achar caixas internas: entra só se as outras falharem
    mascaras = [("fundo", _mascara_fundo(peq)), ("grabcut", _mascara_grabcut(peq))]
    for nome, mascara in mascaras:
        contornos, _ = cv2.findContours(mascara, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(contornos, key=cv2.contourArea, reverse=True)[:3]:
            area = cv2.contourArea(c) / area_img
            if area < area_minima or area > 0.98:
                continue
            q = _quadrilatero(c)
            if q is None:
                continue
            # quadrilátero deve explicar bem o contorno (retangularidade)
            solidez = cv2.contourArea(c) / max(cv2.contourArea(q), 1)
            if solidez < 0.85:
                continue
            q = ordenar_cantos(q)
            # parte do objeto que ficou FORA do quadrilátero (quadrilátero cortou o documento)
            m_c = np.zeros(mascara.shape, np.uint8)
            cv2.drawContours(m_c, [c], -1, 1, cv2.FILLED)
            m_q = np.zeros(mascara.shape, np.uint8)
            cv2.fillPoly(m_q, [np.round(q).astype(np.int32)], 1)
            excesso = float(np.logical_and(m_c, 1 - m_q).sum() / max(1, m_c.sum()))
            solidez = min(solidez, 1.0) * max(0.0, 1 - 6 * excesso)
            # nota = quanto do contorno do quadrilátero cai sobre bordas reais da foto
            # e quanto do que está logo fora dele parece fundo (não cortou o documento)
            nota = (_suporte_bordas(cinza, q) * solidez * (0.4 + 0.6 * _fora_eh_fundo(peq, q))
                    + 0.05 * (cv2.contourArea(q) / area_img))
            melhores.append((nota, q, nome))
            break
    if not melhores:
        mascaras = [("bordas", _mascara_bordas(peq)), ("cor", _mascara_papel(peq))]
        for nome, mascara in mascaras:
            contornos, _ = cv2.findContours(mascara, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contornos:
                c = max(contornos, key=cv2.contourArea)
                if cv2.contourArea(c) / area_img >= area_minima:
                    melhores.append((0.0, ordenar_cantos(_quadrilatero(c)), nome))
                    break
    if not melhores:
        return Deteccao(None, "nenhum", 0.0)
    pont, q, nome = max(melhores, key=lambda t: t[0])
    cantos = ordenar_cantos(q / escala)
    return Deteccao(cantos, nome, float(cv2.contourArea(cantos) / (h * w)))


def expandir(cantos: np.ndarray, fator: float, forma: tuple[int, int]) -> np.ndarray:
    """Afasta os cantos do centro: uma margem pequena evita cortar a 1ª letra das linhas
    quando a borda detectada fica alguns pixels para dentro do papel."""
    c = cantos.mean(axis=0)
    novo = c + (cantos - c) * (1 + fator)
    novo[:, 0] = novo[:, 0].clip(0, forma[1] - 1)
    novo[:, 1] = novo[:, 1].clip(0, forma[0] - 1)
    return novo.astype(np.float32)


def retificar(img: np.ndarray, cantos: np.ndarray, largura: int = LARGURA_SAIDA, margem: float = 0.025) -> np.ndarray:
    """Aplica a homografia que leva os 4 cantos para um retângulo A4 (vista de frente)."""
    cantos = expandir(ordenar_cantos(cantos), margem, img.shape[:2]) if margem else cantos
    tl, tr, br, bl = ordenar_cantos(cantos)
    larg_med = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    alt_med = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    prop = alt_med / max(larg_med, 1)
    # documentos de consumo são A4/carta: se a proporção medida for próxima, força A4
    if 1.25 < prop < 1.55:
        prop = PROPORCAO_A4
    elif 1 / 1.55 < prop < 1 / 1.25:  # documento deitado (foto de lado): mantém resolução do lado maior
        prop = 1 / PROPORCAO_A4
        largura = int(round(largura * PROPORCAO_A4))
    altura = int(round(largura * prop))
    destino = np.array([[0, 0], [largura - 1, 0], [largura - 1, altura - 1], [0, altura - 1]], np.float32)
    M = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], np.float32), destino)
    return cv2.warpPerspective(img, M, (largura, altura), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def iou_quadrilateros(a: np.ndarray, b: np.ndarray, forma: tuple[int, int]) -> float:
    ma = np.zeros(forma, np.uint8)
    mb = np.zeros(forma, np.uint8)
    cv2.fillPoly(ma, [np.round(a).astype(np.int32)], 1)
    cv2.fillPoly(mb, [np.round(b).astype(np.int32)], 1)
    inter = np.logical_and(ma, mb).sum()
    uniao = np.logical_or(ma, mb).sum()
    return float(inter / uniao) if uniao else 0.0


def _osd(cinza: np.ndarray) -> tuple[int, float]:
    import re

    import pytesseract
    try:
        osd = pytesseract.image_to_osd(cinza, config="--psm 0 -c min_characters_to_try=20")
    except pytesseract.TesseractError:
        return 0, 0.0
    m_rot = re.search(r"Rotate:\s*(\d+)", osd)
    m_conf = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
    return (int(m_rot.group(1)) if m_rot else 0), (float(m_conf.group(1)) if m_conf else 0.0)


GIROS = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def corrigir_orientacao(doc: np.ndarray, conf_minima: float = 1.5) -> tuple[np.ndarray, int]:
    """Deixa o texto 'em pé' usando a detecção de orientação (OSD) do Tesseract.

    Cobre fotos tiradas com o celular deitado ou de cabeça para baixo.
    Devolve o documento girado e o ângulo aplicado no sentido horário (0, 90, 180 ou 270)."""
    cinza = cv2.cvtColor(doc, cv2.COLOR_BGR2GRAY) if doc.ndim == 3 else doc
    angulo, conf = _osd(cinza)
    if conf < conf_minima:
        angulo = 0
        h, w = cinza.shape[:2]
        if w > h * 1.15:
            # documento deitado e OSD sem certeza: gira 90° e pergunta de novo
            a2, c2 = _osd(cv2.rotate(cinza, cv2.ROTATE_90_CLOCKWISE))
            angulo = 270 if (a2 == 180 and c2 >= conf_minima) else 90
    return (cv2.rotate(doc, GIROS[angulo]) if angulo in GIROS else doc), angulo
