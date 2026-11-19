"""Localiza o endereço do CLIENTE entre os vários endereços de uma fatura.

Uma conta de consumo costuma ter 2 a 4 endereços: o do imóvel/instalação (o que
queremos), o de cobrança/correspondência, o da agência de atendimento e o da
sede da empresa (rodapé, junto do CNPJ). A estratégia é:

1. Encontrar todas as linhas com CEP (âncora numérica muito robusta ao OCR).
2. Para cada CEP, montar o bloco de endereço subindo pelas linhas vizinhas
   (mesma coluna, espaçamento vertical pequeno) até achar um rótulo.
3. Pontuar cada bloco com regras explicáveis:
     + rótulo positivo (unidade consumidora, imóvel, instalação)
     - rótulo negativo (cobrança, agência, atendimento, correspondência)
     - marcas de endereço institucional (CNPJ, S.A., Cia., nome da empresa)
     + endereço válido após interpretação
4. Escolher o melhor e devolver também os demais candidatos com suas notas,
   para que a decisão seja auditável.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .endereco import RE_CEP, Endereco, Validacao, corrigir_digitos_ocr, interpretar, normalizar_comparacao, validar
from .ocr import Linha

ROTULOS_POSITIVOS = {
    # radicais curtos: o OCR às vezes corta o fim do rótulo ("ENDEREÇO DA UNIDAD")
    "enel": ["unidad", "consumidora", "local de consumo"],
    "sabesp": ["imovel", "endereco de fornecimento", "ligacao"],
    "claro": ["endereco de instala", "local de instala"],
}
ROTULOS_POSITIVOS_GERAIS = ["unidad", "consumidora", "imovel", "endereco de instala", "local de instala", "endereco de entrega",
                            "local de consumo", "endereco do cliente", "endereco"]
ROTULOS_NEGATIVOS = ["cobranca", "agencia", "atendimento", "correspondencia", "posto", "loja"]
MARCAS_INSTITUCIONAIS = [" s a ", " s a", "cia ", "companhia", "distribuicao", "saneamento",
                         "inscricao estadual", "claro s", "enel distribuicao", "sabesp"]
RE_CNPJ = re.compile(r"\d{2}\.?\d{3}\.?\d{3}\s?/\s?\d{4}")
RE_ROTULO = re.compile(r"(endere[cç]o|unidade consumidora|cliente|ag[eê]ncia|instala[cç][aã]o|im[oó]vel|"
                       r"cobran[cç]a|atendimento|dados do)", re.IGNORECASE)


@dataclass
class Candidato:
    linhas: list[str]
    rotulo: str
    pontuacao: float
    motivos: list[str]
    endereco: Endereco
    validacao: Validacao
    conf_ocr: float
    caixa: tuple[int, int, int, int]


@dataclass
class ResultadoExtracao:
    escolhido: Candidato | None
    candidatos: list[Candidato] = field(default_factory=list)


def _eh_rotulo(l: Linha) -> bool:
    t = l.texto.strip()
    if RE_CEP.search(corrigir_digitos_ocr(t)):
        return False
    # rótulo: curto, termina em ':' ou contém palavra de rótulo e não tem número de porta
    return bool(RE_ROTULO.search(t)) and (len(t) < 45 or t.endswith(":"))


def _mesma_coluna(a: Linha, b: Linha) -> bool:
    return abs(a.x - b.x) < 60 or (min(a.x2, b.x2) - max(a.x, b.x)) > 0.5 * min(a.w, b.w)


def _bloco(linhas: list[Linha], i_cep: int) -> tuple[list[Linha], Linha | None]:
    bloco = [linhas[i_cep]]
    rotulo = None
    atual = linhas[i_cep]
    # candidatos acima, do mais próximo ao mais distante
    acima = [l for l in reversed(linhas[:i_cep]) if l.y2 <= atual.y + atual.h * 0.5]
    for l in acima:
        if len(bloco) >= 5:
            break
        if not _mesma_coluna(l, atual):
            continue
        gap = atual.y - l.y2
        limite = 1.6 * max(l.h, atual.h) + 12
        if gap > limite:
            break
        if _eh_rotulo(l):
            rotulo = l
            break
        # linha com texto + rótulo na mesma linha: "Endereço do Imóvel: R. Augusta..."
        if ":" in l.texto and RE_ROTULO.search(l.texto.split(":")[0]):
            rotulo = Linha(l.texto.split(":")[0], l.x, l.y, l.w, l.h, l.conf)
            bloco.insert(0, Linha(l.texto.split(":", 1)[1], l.x, l.y, l.w, l.h, l.conf))
            break
        bloco.insert(0, l)
        atual = l
    # rótulo logo acima do bloco, mesmo que em coluna levemente diferente
    if rotulo is None:
        topo = bloco[0]
        for l in reversed(linhas[: linhas.index(bloco[0]) if bloco[0] in linhas else i_cep]):
            if topo.y - l.y2 > 2.5 * topo.h + 15:
                break
            if _eh_rotulo(l):
                rotulo = l
                break
    return bloco, rotulo


def _pontuar(bloco_txt: list[str], rotulo_txt: str, empresa: str | None) -> tuple[float, list[str]]:
    p, motivos = 0.0, []
    r = normalizar_comparacao(rotulo_txt)
    b = " " + normalizar_comparacao(" ".join(bloco_txt)) + " "
    if empresa and any(k in r for k in ROTULOS_POSITIVOS.get(empresa, [])):
        p += 3
        motivos.append(f"rótulo típico da {empresa}: '{rotulo_txt}'")
    elif any(k in r for k in ROTULOS_POSITIVOS_GERAIS[:-1]):
        p += 2
        motivos.append(f"rótulo de endereço do cliente: '{rotulo_txt}'")
    elif "endereco" in r:
        p += 0.5
        motivos.append("rótulo genérico 'endereço'")
    neg = [k for k in ROTULOS_NEGATIVOS if k in r or k in b[:40]]
    if neg:
        p -= 3
        motivos.append(f"rótulo de endereço que não é do imóvel ({', '.join(neg)})")
    inst = [k.strip() for k in MARCAS_INSTITUCIONAIS if k in b]
    bruto = " ".join(bloco_txt)
    if RE_CNPJ.search(bruto) or re.search(r"(?i)\bcnp", bruto):
        inst.append("CNPJ")
    if inst:
        p -= 4
        motivos.append(f"parece endereço da empresa ({', '.join(inst)})")
    return p, motivos


def extrair_endereco(linhas: list[Linha], empresa: str | None) -> ResultadoExtracao:
    for l in linhas:
        l.texto = corrigir_digitos_ocr(l.texto)
    candidatos: list[Candidato] = []
    usados: set[int] = set()
    blocos_vistos: list[set[int]] = []
    for i, l in enumerate(linhas):
        if i in usados or not RE_CEP.search(l.texto):
            continue
        bloco, rotulo = _bloco(linhas, i)
        usados.add(i)
        textos = [b.texto for b in bloco]
        ids = {id(b) for b in bloco}
        if any(ids <= ja for ja in blocos_vistos):  # mesmo bloco físico já avaliado
            continue
        blocos_vistos.append(ids)
        rot_txt = rotulo.texto if rotulo else ""
        pont, motivos = _pontuar(textos, rot_txt, empresa)
        end = interpretar(textos)
        val = validar(end)
        if val.valido:
            pont += 1
            motivos.append("endereço completo e consistente (CEP x UF)")
        else:
            pont -= 0.5 * len(val.erros)
        conf = sum(b.conf for b in bloco) / len(bloco)
        caixa = (min(b.x for b in bloco), min(b.y for b in bloco), max(b.x2 for b in bloco), max(b.y2 for b in bloco))
        candidatos.append(Candidato(textos, rot_txt, round(pont, 2), motivos, end, val, round(conf, 1), caixa))

    # Fallback: nenhum CEP legível -> usa as linhas logo abaixo do melhor rótulo positivo
    if not candidatos:
        for i, l in enumerate(linhas):
            r = normalizar_comparacao(l.texto)
            if _eh_rotulo(l) and any(k in r for k in ROTULOS_POSITIVOS_GERAIS[:-1]):
                bloco = [x for x in linhas[i + 1:i + 5] if _mesma_coluna(x, l)][:3]
                if not bloco:
                    continue
                textos = [b.texto for b in bloco]
                end = interpretar(textos)
                val = validar(end)
                caixa = (min(b.x for b in bloco), min(b.y for b in bloco), max(b.x2 for b in bloco), max(b.y2 for b in bloco))
                candidatos.append(Candidato(textos, l.texto, 1.0, ["fallback: CEP ilegível, usando bloco abaixo do rótulo"],
                                            end, val, sum(b.conf for b in bloco) / len(bloco), caixa))
                break

    candidatos.sort(key=lambda c: c.pontuacao, reverse=True)
    # Nunca devolve um endereço que parece da empresa/agência/cobrança: melhor pedir
    # para o cidadão digitar do que cadastrar alertas para o lugar errado.
    escolhido = candidatos[0] if candidatos and candidatos[0].pontuacao > -1 else None
    return ResultadoExtracao(escolhido, candidatos)
