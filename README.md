---
title: Alerta de enchente e calor
emoji: 🌧️
colorFrom: blue
colorTo: red
sdk: docker
app_port: 8000
pinned: false
short_description: Alerta de enchente e calor por endereço (GS FIAP 2026.2)
---

# Alerta de enchente e calor por endereço · MVP

Global Solution FIAP 2026.2 · CitizenTech em Produção · ODS 11 e 13.

O cidadão fotografa uma conta de luz, água ou internet, confirma o endereço lido e passa a receber
alerta por WhatsApp/SMS **só quando a chuva ou o calor ameaçam a região dele**, e não a cidade inteira.

| | |
|---|---|
| **URL do MVP** | <https://alerta-enchente-calor.onrender.com> (Render, plano grátis: na primeira visita leva ~1 min para acordar) |
| **Cadastro** | `/` foto da conta → OCR → cidadão confirma ou corrige → cadastro |
| **Meu endereço** | `/meu?id=…` risco atual do CEP, alertas recebidos, cancelar e apagar dados (LGPD) |
| **Painel** | `/painel` risco por região, CEPs em alerta, últimos alertas disparados, consulta por CEP |
| **API** | `/docs` (Swagger) |

## Como as peças se encaixam

```
foto da conta ──► Visão (OCR + regras de endereço) ──► cidadão confirma ──► cadastros (SQLite)
                                                                                 │
INMET (estações e avisos) + Cemaden (pluviômetros)                                ▼
   └──► pipeline: bronze ─► silver ─► gold (risco_por_cep, fila_alertas) ─► 12 regras de qualidade
                                                                                 │
                                    disparo (WhatsApp/SMS) ◄── fila_alertas ◄────┘
```

* `visao/` é o componente de Visão Computacional **sem alteração** (cópia de `visao-enderecos`).
* `pipeline/` são as regras de risco e a fila do pipeline de Big Data **sem alteração** (cópia de `pipeline-dados`).
  No ambiente de dados quem agenda é o Airflow; aqui o próprio app roda o mesmo código a cada hora
  e logo depois de cada novo cadastro.
* `mvp/` é o que é próprio do produto: agendamento, disparo dos alertas, risco de qualquer CEP,
  página do cidadão, painel e exclusão completa dos dados.

Para atualizar as cópias depois de mexer em Visão ou no pipeline: `bash sincronizar.sh`.

### Níveis de risco

0 normal · 1 atenção · 2 alerta · 3 alerta máximo. A mensagem sai a partir do nível 2, no máximo uma vez
por tipo e nível a cada 6 horas. Chuva: pior pluviômetro do Cemaden a até 5 km (senão a estação do INMET
mais próxima), limites de 1 h, 24 h e 72 h. Calor: máxima de 24 h e sensação térmica (índice de calor da NOAA).
Aviso do INMET sozinho vale no máximo "atenção"; aviso confirmado pelo sensor sobe um nível.

### Dados e envio: o que é real e o que é simulado

* `PIPELINE_MODO=offline` (padrão da apresentação): usa o cenário gravado do temporal de 18/11 na Zona Leste,
  com os horários trazidos para agora. `auto` tenta as APIs do INMET e do Cemaden e cai para a amostra se elas falharem.
* O app sobe com 80 cadastros **sintéticos** (`DEMO_SEED=1`) para o painel ter movimento. Eles nunca recebem mensagem real.
* Sem provedor configurado, o envio é **simulado** e fica registrado (aparece como "simulado" no painel).
  Para enviar de verdade, configure a conta Twilio (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
  `TWILIO_WHATSAPP_FROM` e/ou `TWILIO_SMS_FROM`).

## Rodar local

```bash
sudo apt install tesseract-ocr tesseract-ocr-por      # macOS: brew install tesseract tesseract-lang
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                                             # 6 testes de ponta a ponta
uvicorn mvp.main:app --port 8000                      # http://localhost:8000
```

Com Docker: `docker build -t mvp-alertas . && docker run -p 8000:8000 mvp-alertas`.

## Publicar numa URL pública

### Opção 1 (grátis, recomendada): Hugging Face Spaces

Não precisa de cartão nem de GitHub. A URL fica `https://<usuario>-<nome-do-space>.hf.space`.

1. Crie uma conta em <https://huggingface.co/join> e confirme o e-mail.
2. Abra <https://huggingface.co/new-space>: dê um nome (ex.: `alerta-enchente-calor`), escolha **Docker** › **Blank**,
   hardware **CPU basic · Free**, visibilidade **Public** e clique em **Create Space**.
3. Na aba **Files**, clique em **Add file › Upload files**, arraste **o conteúdo** da pasta `mvp-alertas`
   (as pastas `mvp`, `visao`, `pipeline`, `azure`, `docs`, `tests` e os arquivos `Dockerfile`, `README.md`,
   `requirements.txt` etc.) e clique em **Commit changes to main**.
4. O Space monta a imagem sozinho (aba **Logs**, uns 5 minutos). Quando aparecer **Running**, a URL é a do botão
   **⋮ › Embed this Space › Direct URL**.

Opcional: em **Settings › Variables and secrets** crie o segredo `ADMIN_TOKEN` (protege a rota que roda o pipeline na hora).
O Space dorme depois de 48 h sem visitas e acorda no primeiro acesso; o banco recomeça quando ele reinicia
(os cadastros de demonstração são recriados sozinhos).

### Opção 2 (grátis): Render, a partir do GitHub

Com o código num repositório do GitHub: <https://dashboard.render.com> › **New › Blueprint** › escolha o repositório.
O `render.yaml` já configura tudo (Docker, plano free, health check). URL: `https://alerta-enchente-calor.onrender.com`.
No plano free o serviço dorme após 15 min sem acesso e leva ~1 min para acordar.

### Opção 3: Azure App Service (precisa de créditos)

Abra <https://shell.azure.com> (Bash), faça upload do `mvp-alertas.zip` e rode:

```bash
unzip -o mvp-alertas.zip -d mvp-alertas && cd mvp-alertas && bash azure/deploy.sh
```

O script cria grupo de recursos, Container Registry, plano B1 Linux e o Web App, monta a imagem na nuvem
(`az acr build`) e imprime a URL. Rodar de novo atualiza o mesmo app.

**CI/CD (GitHub Actions):** `.github/workflows/deploy-azure.yml` roda os testes em todo push e, com os segredos
`ACR_LOGIN_SERVER`, `ACR_USERNAME`, `ACR_PASSWORD`, `AZURE_WEBAPP_NAME` e `AZURE_WEBAPP_PUBLISH_PROFILE`
configurados, monta a imagem, publica no Web App e faz um teste de fumaça na URL. Sem os segredos, só os testes rodam.

## Operação

| Variável | Padrão | Para quê |
|---|---|---|
| `CADASTRO_DB` | `/home/dados/cadastros.sqlite3` | banco único (cadastro, risco, fila, envios); `/home` é persistente no App Service |
| `PIPELINE_MODO` | `offline` | `offline`, `auto` ou `online` |
| `PIPELINE_INTERVALO_MIN` | `60` | intervalo do agendador |
| `ADMIN_TOKEN` | gerado pelo deploy | protege `POST /api/v1/pipeline/executar` (cabeçalho `x-admin-token`) |
| `DEMO_SEED` | `1` | cria os cadastros sintéticos se o banco estiver vazio |

Saúde: `GET /api/v1/saude`. Logs: `az webapp log tail -g rg-citizentech -n <app>`.

## Privacidade (LGPD)

A foto é processada em memória e descartada. O telefone não entra no data lake: o pipeline só usa id e endereço,
e o disparo busca o contato pelo id. Telas e API mostram o celular mascarado. "Cancelar alertas" apaga o cadastro,
a fila e o histórico de envio daquela pessoa.
