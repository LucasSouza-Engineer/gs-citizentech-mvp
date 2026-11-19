"""Componente de Visão Computacional do MVP CitizenTech: extração de endereço de contas de consumo."""
from .endereco import Endereco, interpretar, validar
from .pipeline import Resultado, processar

__all__ = ["Endereco", "Resultado", "interpretar", "processar", "validar"]
__version__ = "1.0.0"
