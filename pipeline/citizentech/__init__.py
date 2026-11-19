"""Pipeline de dados do CitizenTech: alerta de enchente e calor por endereço.

Camadas (arquitetura medalhão):
  bronze  -> dado bruto exatamente como veio da fonte (JSON/registro do SQLite), por data de coleta
  silver  -> dado limpo, tipado, deduplicado e padronizado (Parquet)
  gold    -> risco por CEP e fila de alertas, prontos para o MVP e para o BI
"""
__version__ = "1.0.0"
