# Metrics_LES — Painel de desempenho (YouTube + X)

Protótipo local que coleta seus próprios posts pelas APIs oficiais, guarda tudo em SQLite e
responde três perguntas:

1. **Qual o melhor horário para postar?** (audiência ativa + seu histórico, com intervalo de confiança)
2. **Algum post foi mal por quê?** (esperado vs. real por formato, sem vazamento de futuro)
3. **Houve queda anômala de distribuição?** (possível "shadowban" — heurística, não veredito)

Tudo roda na sua máquina. Nenhum dado sai daqui, nenhum servidor é necessário.

---

## Estrutura

```
src/
  config.py            configuração via .env
  db.py                schema SQLite + helpers de escrita
  collectors/
    youtube.py         YouTube Data API + YouTube Analytics API (OAuth)
    x.py               X API v2 (OAuth 2.0 PKCE) com trava de orçamento
  analysis/
    frame.py           carrega posts + métricas mais recentes em um DataFrame
    timing.py          heatmaps de audiência e desempenho por horário
    baseline.py        esperado vs. real (mediana histórica por formato)
    anomalies.py       detecção de quedas + relatório de "shadowban"
  dashboard.py         interface Streamlit (4 abas)
scripts/
  init_db.py           cria o banco (opcionalmente com dados de demonstração)
  collect.py           CLI de coleta
tests/                 testes das funções de análise
```

Banco: `data/analytics.db` (SQLite). Segredos: `.secrets/` (fora do git).

---

## Instalação (Windows / PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

### Ver o painel já funcionando (dados sintéticos)

```powershell
.venv\Scripts\python.exe scripts\init_db.py --demo
.venv\Scripts\python.exe -m streamlit run src\dashboard.py
```

Abre em <http://localhost:8501>. Para limpar depois: `python scripts\init_db.py --reset`.

---

## Configurar o YouTube (grátis)

1. Acesse <https://console.cloud.google.com> e crie um projeto.
2. **APIs e serviços → Biblioteca**: ative **YouTube Data API v3** e **YouTube Analytics API**.
3. **Tela de consentimento OAuth**: tipo *Externo*, preencha o essencial e adicione seu e-mail em
   **Usuários de teste**.
4. **Credenciais → Criar credenciais → ID do cliente OAuth → Aplicativo para computador**.
5. Baixe o JSON e salve como `.secrets\client_secret.json`
   (alternativa: `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` no `.env`).

Colete:

```powershell
.venv\Scripts\python.exe scripts\collect.py youtube
.venv\Scripts\python.exe scripts\collect.py youtube --limit 50   # só os 50 mais recentes
```

Na primeira execução abre o navegador para autorizar; o token fica salvo em
`.secrets\youtube_token.json` e é renovado sozinho.

**O que é coletado:** todos os vídeos do canal, views, minutos assistidos, retenção média,
likes, comentários, compartilhamentos, inscritos ganhos/perdidos, impressões e CTR do thumbnail
(quando a conta tem esses dados), origem do tráfego (busca, navegação, sugeridos, externo),
inscritos vs. não inscritos, e os horários/dias em que seu público assiste.

---

## Configurar o X (pago por uso — centavos)

O X não tem mais tier grátis desde fevereiro/2026. O modelo é pré-pago, por requisição:

- leitura de **post seu**: ~US$ 0,001
- leitura de post de terceiro: ~US$ 0,005
- publicar: ~US$ 0,015 (US$ 0,20 se tiver link)

Para analisar 500 posts seus: **~US$ 0,50**. O coletor tem trava de orçamento
(`X_BUDGET_USD` no `.env`) e recusa rodar acima do limite.

1. <https://console.x.com> → crie um App.
2. Em **User authentication settings**: ative OAuth 2.0, tipo *Web App / Automated App or Bot*,
   callback exatamente `http://localhost:8080/callback`.
3. Copie **Client ID** (e **Client Secret**, se o app for confidencial) para o `.env`.
4. Compre créditos no Developer Console.

```powershell
.venv\Scripts\python.exe scripts\collect.py x --dry-run          # só estima o custo
.venv\Scripts\python.exe scripts\collect.py x --limit 200 --max-spend 1.00
```

Na autorização, o navegador vai tentar abrir `localhost:8080` e dar erro de conexão —
isso é esperado: copie a URL completa da barra de endereços e cole no terminal.

> Métricas privadas (impressões, cliques no perfil, cliques no link) só existem para os
> últimos ~30 dias. Por isso o coletor grava **snapshots**: rode semanalmente para não perder
> o histórico. Posts antigos ainda entram com as métricas públicas (likes, reposts, respostas).

---

## Comandos úteis

```powershell
.venv\Scripts\python.exe scripts\init_db.py            # cria/atualiza o banco
.venv\Scripts\python.exe scripts\init_db.py --reset    # zera e recria
.venv\Scripts\python.exe scripts\collect.py all        # YouTube + X
.venv\Scripts\python.exe -m pytest -q                  # testes
.venv\Scripts\python.exe -m streamlit run src\dashboard.py       # painel
```

---

## Como ler o painel (e o que ele *não* diz)

- **Horários:** a aba mostra duas coisas diferentes. O heatmap de audiência vem do YouTube
  (dado real de quando as pessoas assistem). O heatmap de desempenho vem do seu histórico e é
  **encolhido para a média** — um único post bom às 3h não vira recomendação. A tabela ordena
  pelo piso do intervalo (P10), não pela média.
- **Esperado vs. real:** cada post é comparado à mediana dos posts **anteriores** do mesmo
  formato. É comparação descritiva, não causal: o modelo não sabe se você mudou o gancho, o tema
  ou o thumbnail.
- **Diagnóstico:** o índice soma sinais (queda consecutiva de alcance, perda de alcance de
  não inscritos, queda de navegação/sugestão). Ele detecta **anomalia**, não penalidade.
  Nenhuma plataforma publica "você está com shadowban". Trate como alerta para investigar.

### Limitações honestas

- Com menos de ~20 posts por formato, as estimativas são instáveis. O painel avisa quando não há base.
- Métricas acumulam com o tempo. Comparar um post de 2h com um de 30 dias é erro — por isso
  guardamos o instante da coleta e usamos o snapshot mais recente de cada post.
- O X não fornece dados de audiência (quem vê, quando vê), apenas o seu próprio desempenho.
- Fuso horário padrão: `America/Sao_Paulo` (`ANALYTICS_TZ` no `.env`).

---

## Automação gratuita (opcional)

Para coletar todo dia sem manter nada aberto, use o **Agendador de Tarefas do Windows**:

```powershell
schtasks /create /tn "ColetaRedes" /tr "C:\caminho\para\.venv\Scripts\python.exe C:\caminho\para\scripts\collect.py all" /sc daily /st 09:00
```

Ou GitHub Actions (repo privado, minutos gratuitos), rodando uma vez por dia. Nesse caso guarde
os tokens como *secrets* e faça commit apenas do código.

---

## Próximos passos sugeridos

1. Rodar a coleta por 4–6 semanas para formar base estatística.
2. Adicionar features de conteúdo (tema via embeddings, tipo de gancho, OCR de thumbnail) para
   explicar *por que* um post ficou abaixo — hoje o painel mostra *quanto*, não *por quê*.
3. Modelo de previsão (gradient boosting) quando houver 100+ posts por formato.
4. Alertas por e-mail/Telegram quando o índice de anomalia passar de 60.
