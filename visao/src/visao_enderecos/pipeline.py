"""Pipeline completo: foto -> documento retificado -> empresa -> endereço validado."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from .classificador import classificar
from .documento import GIROS, corrigir_orientacao, detectar_documento, retificar
from .extrator import Candidato, extrair_endereco
from .ocr import ler_linhas
from .preprocessamento import nitidez, preprocessar

LIMIAR_NITIDEZ = 60.0
LIMIAR_CONFIANCA = 0.75


@dataclass
class Resultado:
    empresa: str | None
    confianca_empresa: float
    endereco: dict | None
    valido: bool
    confianca: float
    campos_revisar: list[str]
    erros: list[str]
    avisos: list[str]
    documento_detectado: bool
    metodo_deteccao: str
    cantos: list | None
    candidatos: list[dict] = field(default_factory=list)
    caixa_endereco: list | None = None  # x1, y1, x2, y2 no documento retificado
    tempos_ms: dict = field(default_factory=dict)
    documento: np.ndarray | None = None  # imagem retificada (não serializada)
    texto_ocr: str = ""

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("documento", "texto_ocr")}
        return d


def _conf(c: Candidato, margem: float) -> float:
    base = c.conf_ocr / 100
    if not c.validacao.valido:
        base *= 0.5
    base *= 1 - 0.1 * len(c.validacao.campos_revisar)
    base *= min(1.0, 0.6 + 0.1 * margem)  # pouca diferença para o 2º candidato = mais dúvida
    return round(max(0.0, min(1.0, base)), 3)


def _valido(ext) -> bool:
    return bool(ext.escolhido and ext.escolhido.validacao.valido)


def _ler(doc: np.ndarray, lang: str, rapido: bool = False):
    """Pré-processa e lê o documento; tenta variações da imagem até achar um endereço válido."""
    limpo = preprocessar(doc)
    tentativas = [("cinza", lambda: limpo)]
    if not rapido:
        tentativas += [("ampliada", lambda: cv2.resize(limpo, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC)),
                       ("binaria", lambda: preprocessar(doc, binarizar=True))]
    melhor = None
    for _, gerar in tentativas:
        imagem = gerar()
        fator = imagem.shape[1] / limpo.shape[1]
        linhas = ler_linhas(imagem, lang=lang)
        if fator != 1:  # volta as coordenadas para o documento retificado
            for l in linhas:
                l.x, l.y, l.w, l.h = (int(round(v / fator)) for v in (l.x, l.y, l.w, l.h))
        texto = "\n".join(l.texto for l in linhas)
        cls = classificar(doc, texto)
        ext = extrair_endereco(linhas, cls.empresa)
        if melhor is None or (ext.escolhido and (melhor[1].escolhido is None or
                                                 (_valido(ext) and not _valido(melhor[1])))):
            melhor = (cls, ext, texto)
        if _valido(ext):
            break
    return limpo, melhor


def processar(img: np.ndarray, corrigir_perspectiva: bool = True, lang: str = "por") -> Resultado:
    t0 = time.perf_counter()
    tempos = {}
    if img is None or img.size == 0:
        raise ValueError("Imagem vazia ou ilegível")
    avisos = []

    # 1. Detecção do documento + correção de perspectiva
    det = detectar_documento(img) if corrigir_perspectiva else None
    if det is not None and det.cantos is not None:
        doc = retificar(img, det.cantos)
    else:
        # Sem documento detectado: assume foto já recortada / digitalização
        esc = 1240 / img.shape[1]
        doc = cv2.resize(img, None, fx=esc, fy=esc, interpolation=cv2.INTER_CUBIC)
        if corrigir_perspectiva and det is not None and det.metodo != "ja_recortada":
            avisos.append("Bordas do documento não encontradas; usando a imagem inteira")
    tempos["deteccao_retificacao"] = round((time.perf_counter() - t0) * 1000)

    # 1b. Orientação: foto de lado ou de cabeça para baixo
    to = time.perf_counter()
    doc, angulo = corrigir_orientacao(doc)
    if angulo:
        avisos.append(f"Documento girado {angulo}° para leitura")
        if doc.shape[1] != 1240:  # padroniza a largura para o OCR
            esc = 1240 / doc.shape[1]
            doc = cv2.resize(doc, None, fx=esc, fy=esc, interpolation=cv2.INTER_AREA)
    tempos["orientacao"] = round((time.perf_counter() - to) * 1000)

    # 2 e 3. Pré-processamento + OCR + classificação + extração
    t1 = time.perf_counter()
    limpo, (cls, ext, texto) = _ler(doc, lang)
    if not _valido(ext):
        # Orientação errada é a causa mais comum de leitura vazia: tenta os outros giros
        for giro in (180, 90, 270):
            alt = cv2.rotate(doc, GIROS[giro])
            if alt.shape[1] != 1240:
                e2 = 1240 / alt.shape[1]
                alt = cv2.resize(alt, None, fx=e2, fy=e2, interpolation=cv2.INTER_AREA)
            limpo2, r2 = _ler(alt, lang, rapido=True)
            if _valido(r2[1]):
                doc, limpo, (cls, ext, texto) = alt, limpo2, r2
                avisos.append(f"Documento girado mais {giro}° após a 1ª leitura falhar")
                break
    if nitidez(limpo) < LIMIAR_NITIDEZ:
        avisos.append("Foto pouco nítida: se possível, tire outra com o celular apoiado")
    tempos["ocr_extracao"] = round((time.perf_counter() - t1) * 1000)
    tempos["total"] = round((time.perf_counter() - t0) * 1000)

    esc = ext.escolhido
    cands = [dict(linhas=c.linhas, rotulo=c.rotulo, pontuacao=c.pontuacao, motivos=c.motivos,
                  endereco=c.endereco.to_dict()) for c in ext.candidatos]
    if esc is None:
        return Resultado(cls.empresa, cls.confianca, None, False, 0.0,
                         ["logradouro", "numero", "bairro", "cidade", "uf", "cep"],
                         ["Nenhum endereço encontrado no documento"], avisos,
                         det is not None and det.cantos is not None, det.metodo if det else "desligado",
                         det.cantos.tolist() if det is not None and det.cantos is not None else None,
                         cands, tempos_ms=tempos, documento=doc, texto_ocr=texto)
    margem = esc.pontuacao - (ext.candidatos[1].pontuacao if len(ext.candidatos) > 1 else -5)
    conf = _conf(esc, margem)
    revisar = list(esc.validacao.campos_revisar)
    if conf < LIMIAR_CONFIANCA and not revisar:
        avisos.append("Confiança baixa: confira todos os campos")
    return Resultado(
        empresa=cls.empresa, confianca_empresa=cls.confianca, endereco=esc.endereco.to_dict(),
        valido=esc.validacao.valido, confianca=conf, campos_revisar=revisar, erros=esc.validacao.erros,
        avisos=avisos + esc.validacao.avisos, documento_detectado=det is not None and det.cantos is not None,
        metodo_deteccao=det.metodo if det else "desligado",
        cantos=det.cantos.round(1).tolist() if det is not None and det.cantos is not None else None,
        candidatos=cands, caixa_endereco=list(esc.caixa), tempos_ms=tempos, documento=doc, texto_ocr=texto)
