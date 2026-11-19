"""Interpretação (parsing), normalização e validação de endereços brasileiros."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

UFS = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB",
       "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}

# Faixas oficiais de CEP por UF (Correios), em inteiros de 8 dígitos
FAIXAS_CEP = [
    ("SP", 1000000, 19999999), ("RJ", 20000000, 28999999), ("ES", 29000000, 29999999),
    ("MG", 30000000, 39999999), ("BA", 40000000, 48999999), ("SE", 49000000, 49999999),
    ("PE", 50000000, 56999999), ("AL", 57000000, 57999999), ("PB", 58000000, 58999999),
    ("RN", 59000000, 59999999), ("CE", 60000000, 63999999), ("PI", 64000000, 64999999),
    ("MA", 65000000, 65999999), ("PA", 66000000, 68899999), ("AP", 68900000, 68999999),
    ("AM", 69000000, 69299999), ("RR", 69300000, 69399999), ("AM", 69400000, 69899999),
    ("AC", 69900000, 69999999), ("DF", 70000000, 73699999), ("GO", 72800000, 76799999),
    ("TO", 77000000, 77999999), ("MT", 78000000, 78899999), ("RO", 76800000, 76999999),
    ("MS", 79000000, 79999999), ("PR", 80000000, 87999999), ("SC", 88000000, 89999999),
    ("RS", 90000000, 99999999),
]

TIPOS_LOGRADOURO = {
    "r": "Rua", "rua": "Rua", "av": "Avenida", "avenida": "Avenida", "al": "Alameda",
    "alameda": "Alameda", "tv": "Travessa", "trav": "Travessa", "travessa": "Travessa",
    "estr": "Estrada", "est": "Estrada", "estrada": "Estrada", "rod": "Rodovia", "rodovia": "Rodovia",
    "pc": "Praça", "pca": "Praça", "praca": "Praça", "praça": "Praça", "lgo": "Largo", "largo": "Largo",
    "via": "Via", "vl": "Vila", "vila": "Vila",
}
RE_TIPO = re.compile(r"^\s*(" + "|".join(sorted(map(re.escape, TIPOS_LOGRADOURO), key=len, reverse=True))
                     + r")(?:\.\s*|\s*[,-]\s*|\s+)(?=[A-ZÀ-Úa-zà-ú0-9])", re.IGNORECASE)
RE_COMPLEMENTO = re.compile(r"^(apto?\.?|apartamento|ap\.?|casa|bloco|bl\.?|fundos|sala|lote|lt\.?|conj\.?|"
                            r"conjunto|cj\.?|torre|andar|loja|sobrado|kitnet)\b", re.IGNORECASE)
# CEP com hífen/ponto (01305-154) ou precedido de "CEP" (CEP 01305154).
# Sequências soltas de 8 dígitos NÃO contam: confundiriam com nº de hidrômetro, contrato etc.
# palavras de outros campos da fatura que o OCR pode grudar no fim de uma linha de endereço
RE_LIXO_FIM = re.compile(r"\s*[|\[\]]?\s*\b(vencimento|total|instala[cç][aã]o|n[º°o]\s*da|leitura|classe|"
                         r"contrato|rgi|hidr[oô]metro)\b.*$", re.IGNORECASE)
RE_CEP = re.compile(r"(?:(?<![\w/])(\d{5})\s?[-.]\s?(\d{3})|(?i:cep)\s*:?\s*(\d{5})(\d{3}))(?![\d/])")


def cep_do_match(m: re.Match) -> str:
    return f"{m.group(1) or m.group(3)}-{m.group(2) or m.group(4)}"
MINUSCULAS = {"de", "da", "do", "das", "dos", "e", "d'"}


@dataclass
class Endereco:
    logradouro: str = ""
    numero: str = ""
    complemento: str = ""
    bairro: str = ""
    cidade: str = ""
    uf: str = ""
    cep: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def linha(self) -> str:
        comp = f" {self.complemento}" if self.complemento else ""
        return f"{self.logradouro}, {self.numero}{comp} - {self.bairro} - {self.cidade}/{self.uf} - CEP {self.cep}"


@dataclass
class Validacao:
    valido: bool
    erros: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    campos_revisar: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalizar_comparacao(s: str) -> str:
    """Forma canônica para comparar com o gabarito (sem acento, caixa, pontuação)."""
    s = sem_acento(s or "").casefold()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def titulo(s: str) -> str:
    """Converte 'RUA DOM PEDRO II' -> 'Rua Dom Pedro II', preservando preposições."""
    palavras = []
    for i, p in enumerate(s.lower().split()):
        if re.fullmatch(r"[ivxl]+", p) and len(p) <= 4 and i > 0:
            palavras.append(p.upper())
        elif i > 0 and p in MINUSCULAS:
            palavras.append(p)
        elif "'" in p:
            a, _, b = p.partition("'")
            palavras.append(f"{a.capitalize()}'{b.capitalize()}" if a not in MINUSCULAS else f"{a}'{b.capitalize()}")
        else:
            palavras.append(p[:1].upper() + p[1:])
    return " ".join(palavras)


def corrigir_digitos_ocr(s: str) -> str:
    """Troca letras confundidas com dígitos em trechos numéricos (O->0, l/I->1, S->5, B->8)."""
    tabela = str.maketrans({"O": "0", "o": "0", "D": "0", "l": "1", "I": "1", "|": "1", "S": "5",
                            "s": "5", "B": "8", "Z": "2", "z": "2", "g": "9", "q": "9"})

    def _fix(m: re.Match) -> str:
        return m.group(0).translate(tabela)

    # sequências com pelo menos 4 dígitos misturados a poucas letras confundíveis, ex: 0S429-O10
    return re.sub(r"(?<![A-Za-z])[0-9OoDlI|SsBZzgq]{5}\s?-\s?[0-9OoDlI|SsBZzgq]{3}(?![A-Za-z])", _fix, s)


def formatar_cep(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return f"{d[:5]}-{d[5:]}" if len(d) == 8 else s


def uf_do_cep(cep: str) -> str | None:
    d = re.sub(r"\D", "", cep or "")
    if len(d) != 8:
        return None
    n = int(d)
    for uf, a, b in FAIXAS_CEP:
        if a <= n <= b:
            return uf
    return None


def expandir_tipo(logradouro: str) -> str:
    m = RE_TIPO.match(logradouro)
    if not m:
        return logradouro.strip()
    tipo = TIPOS_LOGRADOURO[m.group(1).lower().rstrip(".")]
    return f"{tipo} {logradouro[m.end():].strip()}"


# --------------------------------------------------------------------------
def _caixa(s: str) -> str:
    """Padroniza para 'Título' textos que vieram todos em maiúsculas (ou mistura de caixa do OCR)."""
    letras = [c for c in s if c.isalpha()]
    if not letras:
        return s
    maius = sum(c.isupper() for c in letras) / len(letras)
    return titulo(s) if maius > 0.6 or maius < 0.05 or re.search(r"\b[a-zà-ú]+ [A-ZÀ-Ú]{3,}\b", s) else s


TROCAS_OCR = {"€": "C", "©": "C", "§": "S", "¢": "c", "»": "-", "«": "-", "—": "-", "–": "-", "=": "-"}


def limpar_ocr(s: str) -> str:
    for a, b in TROCAS_OCR.items():
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def interpretar(linhas: list[str]) -> Endereco:
    """Transforma as linhas de um bloco de endereço (OCR) em campos estruturados.

    Funciona para os formatos mais comuns de contas de consumo, p.ex.:
      RUA X, 581 APTO 32 / MOOCA / 03176-754 SÃO PAULO SP          (Enel)
      R. X, 675 - Apto 32 - Consolação / São Paulo - SP - CEP ...    (Sabesp)
      Rua X, 1130, Apto 63 / Mooca - São Paulo/SP / CEP: 03176-214   (Claro)
    """
    e = Endereco()
    texto = [RE_LIXO_FIM.sub("", corrigir_digitos_ocr(limpar_ocr(l))).strip(" |:;\"'") for l in linhas if l and l.strip()]
    texto = [t for t in texto if t]
    segmentos: list[str] = []
    for l in texto:
        segmentos += [s.strip(" ,;:|") for s in re.split(r"\s[-–—]\s|\s[-–—](?=[A-Za-zÀ-ú])", l) if s.strip(" ,;:|")]

    restantes = []
    for s in segmentos:
        # CEP
        m = RE_CEP.search(s)
        if m and not e.cep:
            e.cep = cep_do_match(m)
            s = (s[:m.start()] + " " + s[m.end():])
            s = re.sub(r"\bCEP\b\s*:?", " ", s, flags=re.IGNORECASE).strip(" ,;:-")
            if not s:
                continue
        elif re.fullmatch(r"(?i)cep\s*:?", s):
            continue
        # Cidade/UF: "São Paulo/SP", "SÃO PAULO SP", "São Paulo" seguido de "SP"
        m = re.search(r"(?:^|[\s/,-])([A-Za-z]{2})\s*$", s)
        if m and m.group(1).upper() in UFS and not e.uf and not RE_TIPO.match(s):
            e.uf = m.group(1).upper()
            cidade = s[:m.start()].strip(" /,-")
            if cidade:
                e.cidade = cidade
            elif restantes and not e.cidade and not RE_TIPO.match(restantes[-1]):
                e.cidade = restantes.pop()
            continue
        if s.upper() in UFS and not e.uf:
            e.uf = s.upper()
            if restantes and not e.cidade:
                e.cidade = restantes.pop()
            continue
        restantes.append(s)

    # Logradouro + número (+ complemento na mesma linha)
    for i, s in enumerate(restantes):
        # 1º: número logo após vírgula ("Rua 25 de Março, 100"); 2º: primeiro número após o nome
        m = (re.match(r"^(.+?[A-Za-zÀ-ú.])\s*,\s*(?:n[º°o]?\.?\s*)?(\d{1,5}[A-Za-z]?)\b\s*,?\s*(.*)$", s)
             or re.match(r"^(.*?[A-Za-zÀ-ú.])\s*,?\s+(?:n[º°o]?\.?\s*)?(\d{1,5}[A-Za-z]?)\b\s*,?\s*(.*)$", s))
        if m and (RE_TIPO.match(s) or i == 0):
            e.logradouro = expandir_tipo(_caixa(m.group(1).rstrip(", ")))
            e.numero = m.group(2)
            resto = m.group(3).strip(" ,-")
            if resto:
                e.complemento = resto
            restantes.pop(i)
            break
    for s in list(restantes):
        if RE_COMPLEMENTO.match(s) and not e.complemento:
            e.complemento = s
            restantes.remove(s)
    if restantes and not e.bairro:
        e.bairro = restantes.pop(0)
    if restantes and not e.cidade:
        e.cidade = restantes.pop(0)

    e.logradouro = titulo(e.logradouro)
    e.bairro = titulo(e.bairro)
    e.cidade = titulo(e.cidade)
    e.complemento = _caixa(e.complemento)
    return e


def validar(e: Endereco) -> Validacao:
    v = Validacao(valido=True)
    if not re.fullmatch(r"\d{5}-\d{3}", e.cep or ""):
        v.erros.append("CEP ausente ou mal formatado")
        v.campos_revisar.append("cep")
    if e.uf not in UFS:
        v.erros.append("UF inválida")
        v.campos_revisar.append("uf")
    uf_cep = uf_do_cep(e.cep)
    if uf_cep and e.uf in UFS and uf_cep != e.uf:
        v.erros.append(f"CEP {e.cep} pertence a {uf_cep}, mas a UF lida foi {e.uf}")
        v.campos_revisar += ["cep", "uf"]
    if not e.logradouro or len(e.logradouro) < 4:
        v.erros.append("Logradouro não identificado")
        v.campos_revisar.append("logradouro")
    elif not RE_TIPO.match(e.logradouro):
        v.avisos.append("Tipo de logradouro (Rua, Av.) não reconhecido")
    if not re.fullmatch(r"\d{1,5}[A-Za-z]?", e.numero or ""):
        v.erros.append("Número ausente")
        v.campos_revisar.append("numero")
    if not e.bairro:
        v.avisos.append("Bairro não identificado")
        v.campos_revisar.append("bairro")
    if not e.cidade:
        v.erros.append("Cidade não identificada")
        v.campos_revisar.append("cidade")
    v.campos_revisar = list(dict.fromkeys(v.campos_revisar))
    v.valido = not v.erros
    return v
