# Metrics_LES — Painel de desempenho (YouTube + X)

Protótipo local que coleta posts pelas APIs oficiais, guarda tudo em SQLite e responde
três perguntas:

1. **Qual o melhor horário para postar?** (audiência ativa + seu histórico, com intervalo de confiança)
2. **Algum post foi mal por quê?** (esperado vs. real por formato, sem vazamento de futuro)
3. **Houve queda anômala de distribuição?** (possível "shadowban" — heurística, não veredito)

Tudo roda na sua máquina. Nenhum dado sai daqui, nenhum servidor é necessário.

---

## Perfis: próprio vs. público

O banco aceita **vários perfis ao mesmo tempo**, de qualquer plataforma:

| Modo | Como autentica | O que entrega |
|---|---|---|
| **Próprio** (`owned`) | OAuth da conta | tudo: impressões, CTR, retenção, audiência por horário, origem de tráfego |
| **Público** (`public`) | só chave de API / bearer | views (YouTube), likes, comentários, reposts; **sem** impressões/CTR/audiência |

No painel há um seletor de perfil na barra lateral (`Todos os perfis`, ou um específico).
A aba **Diagnóstico** segue o perfil selecionado. Bancos antigos (sem `profile_id`) são
migrados automaticamente na primeira conexão.

---

## Estrutura

```
src/
  config.py            configuração via .env
  db.py                schema SQLite + migração + helpers de escrita
  profiles.py          normalização de @handle e cadastro de perfis
  importers/
    x_analytics.py     importa o CSV do analytics.x.com (sem API)
  collectors/
    youtube.py         YouTube Data API (público) + Analytics API (OAuth)
    x.py               X API v2: própria conta (OAuth) ou terceiros (bearer)
  analysis/
    frame.py           carrega posts + métricas mais recentes em um DataFrame
    timing.py          heatmaps de audiência e desempenho por horário
    baseline.py        esperado vs. real (mediana histórica por formato)
    anomalies.py       detecção de quedas + relatório de "shadowban"
  dashboard.py         interface Streamlit (4 abas)
scripts/
  init_db.py           cria o banco (opcionalmente com dados de demonstração)
  collect.py           CLI de coleta
  import_x.py          importa o CSV do X Analytics (grátis)
  profiles.py          lista/cadastra/remove perfis
tests/                 testes das funções de análise, perfis e migração
```

Banco: `data/analytics.db` (SQLite). Segredos: `.secrets/` (fora do git).

---

## Instalação (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

> Se `python` abrir a Microsoft Store, use o caminho do Python instalado
> (ex.: `C:\Users\seu_usuario\AppData\Local\Python\bin\python.exe`).

### Ver o painel já funcionando (dados sintéticos)

```powershell
.\.venv\Scripts\python.exe scripts\init_db.py --demo
.\.venv\Scripts\python.exe -m streamlit run src\dashboard.py
```

O `--demo` cria 4 perfis fictícios: 2 próprios (YouTube e X, com métricas completas) e
2 públicos (sem impressões/CTR/audiência), para você testar o filtro de perfis.
Abre em <http://localhost:8501>. Para limpar depois: `python scripts\init_db.py --reset`.

---

## Configurar o YouTube (conta própria, grátis)

1. Acesse <https://console.cloud.google.com> e crie um projeto.
2. **APIs e serviços → Biblioteca**: ative **YouTube Data API v3** e **YouTube Analytics API**.
3. **Tela de consentimento OAuth**: tipo *Externo*, preencha o essencial e adicione seu e-mail em
   **Usuários de teste**.
4. **Credenciais → Criar credenciais → ID do cliente OAuth → Aplicativo para computador**.
5. Baixe o JSON e salve como `.secrets\client_secret.json`
   (alternativa: `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` no `.env`).

Colete:

```powershell
.\.venv\Scripts\python.exe scripts\collect.py youtube
.\.venv\Scripts\python.exe scripts\collect.py youtube --limit 50   # só os 50 mais recentes
```

Na primeira execução abre o navegador para autorizar; o token fica salvo em
`.secrets\youtube_token.json` e é renovado sozinho.

**O que é coletado:** views, minutos assistidos, retenção média, likes, comentários,
compartilhamentos, inscritos ganhos/perdidos, impressões e CTR do thumbnail (quando a conta
tem esses dados), origem do tráfego, inscritos vs. não inscritos e os horários/dias em que
seu público assiste.

---

## Analisar outro canal do YouTube (público, grátis)

Para canais de terceiros não existe Analytics API — só dados públicos. É necessário apenas
uma **API key** (não precisa de OAuth):

1. No mesmo projeto do Google Cloud, ative **YouTube Data API v3**.
2. **Credenciais → Criar credenciais → Chave de API**.
3. Coloque em `YT_API_KEY` no `.env`.

```powershell
.\.venv\Scripts\python.exe scripts\collect.py youtube --profile "@canal"
.\.venv\Scripts\python.exe scripts\collect.py youtube --profile "https://youtube.com/@canal" --limit 80
.\.venv\Scripts\python.exe scripts\collect.py youtube --profile UCxxxxxxxxxxxxxxxxxxxxxx
```

Aceita `@handle`, URL, username legado ou ID do canal. Se `YT_CHANNEL_HANDLE` estiver no
`.env`, `--mode public` sem `--profile` usa esse canal.

**O que se perde no modo público:** impressões, CTR, retenção, minutos assistidos, inscritos
ganhos/perdidos, audiência por horário e origem de tráfego. O heatmap de audiência fica vazio;
as análises de horário e esperado vs. real continuam funcionando com views/likes/comentários.

---

## Configurar o X (conta própria, pago por uso — centavos)

O X não tem mais tier grátis. O modelo é pré-pago, por requisição:

- leitura de **post seu**: configurável em `X_OWNED_READ_USD` (~US$ 0,001)
- leitura de post de terceiro: configurável em `X_THIRD_PARTY_READ_USD` (~US$ 0,005)
- publicar: ~US$ 0,015 (US$ 0,20 se tiver link)

Para analisar 500 posts seus: **~US$ 0,50**. O coletor tem trava de orçamento
(`X_BUDGET_USD` no `.env`) e recusa rodar acima do limite.

1. <https://console.x.com> → crie um App.
2. Em **User authentication settings**: ative OAuth 2.0, tipo *Web App / Automated App or Bot*,
   callback exatamente `http://localhost:8080/callback`.
3. Copie **Client ID** (e **Client Secret**, se o app for confidencial) para o `.env`.
4. Compre créditos no Developer Console.

```powershell
.\.venv\Scripts\python.exe scripts\collect.py x --dry-run          # só estima o custo
.\.venv\Scripts\python.exe scripts\collect.py x --limit 200 --max-spend 1.00
```

Na autorização, o navegador vai tentar abrir `localhost:8080` e dar erro de conexão —
isso é esperado: copie a URL completa da barra de endereços e cole no terminal.

> Métricas privadas (impressões, cliques no perfil, cliques no link) só existem para os
> últimos ~30 dias. Por isso o coletor grava **snapshots**: rode semanalmente para não perder
> o histórico. Posts antigos ainda entram com as métricas públicas (likes, reposts, respostas).

---

## Analisar outro perfil no X (público)

1. Em <https://console.x.com>, copie o **Bearer Token** do App para `X_BEARER_TOKEN` no `.env`.
   (Sem bearer, o coletor usa o token OAuth que já estiver salvo.)
2. Rode:

```powershell
.\.venv\Scripts\python.exe scripts\collect.py x --profile "@perfil" --dry-run
.\.venv\Scripts\python.exe scripts\collect.py x --profile "@perfil" --limit 200 --max-spend 1.00
```

**Limitações do modo público no X:** a API só devolve métricas públicas (likes, respostas,
reposts, quotes, bookmarks). Sem impressões, o painel calcula a taxa de engajamento sobre o
número de **seguidores** do perfil — a coluna `reach_basis` registra isso e o painel avisa.
Leituras de terceiros saem mais caras que as da própria conta (ajuste o custo no `.env`).

---

## Sem gastar nada: importar o CSV do X Analytics (recomendado)

O painel nativo do X é **gratuito** e entrega mais métricas do que a API pública: impressões,
engagements, taxa de engajamento, cliques no perfil, cliques no link, bookmarks e novos seguidores.

1. Acesse <https://analytics.x.com> (basta registrar; também funciona via <https://ads.x.com> →
   **Analytics → Post activity**).
2. Na aba **Posts**, escolha o período e clique em **Export** para baixar o `.csv`.
   O export vem em **UTC** e tem limite de 30 dias / 3.000 posts por arquivo.
3. Valide e importe:

```powershell
.\.venv\Scripts\python.exe scripts\import_x.py posts.csv --profile "@seu_usuario" --dry-run
.\.venv\Scripts\python.exe scripts\import_x.py posts.csv --profile "@seu_usuario"
```

- `--dayfirst` se as datas vierem como DD/MM/AAAA.
- `--followers 3200` (opcional) registra o total de seguidores para normalizar o alcance.

O importador reconhece cabeçalhos em português, inglês e espanhol, ignora colunas desconhecidas
e usa o campo **Engagements** da própria plataforma na taxa de engajamento (em vez de somar só
likes/respostas/reposts). Importar o mesmo arquivo duas vezes não duplica posts. Como o X só
exporta até 30 dias por vez, exporte uma vez por mês.

> Se o botão de export não aparecer na sua conta, o painel "View post analytics" de cada post
> continua grátis — mas os dados precisam ser lançados manualmente. Raspar o site com bot viola
> os termos do X e pode bloquear a conta; não recomendo.

---

## Gerenciar perfis

```powershell
.\.venv\Scripts\python.exe scripts\profiles.py list
.\.venv\Scripts\python.exe scripts\profiles.py add youtube "@canal" --mode public --label "Concorrente"
.\.venv\Scripts\python.exe scripts\profiles.py remove youtube:canal
```

`add` apenas cadastra (não coleta). `remove` apaga o perfil **e** todos os posts/snapshots dele.

---

## Comandos úteis

```powershell
.\.venv\Scripts\python.exe scripts\init_db.py            # cria/atualiza o banco
.\.venv\Scripts\python.exe scripts\init_db.py --reset    # zera e recria
.\.venv\Scripts\python.exe scripts\collect.py all        # YouTube + X (contas próprias)
.\.venv\Scripts\python.exe scripts\collect.py x --profile "@fulano" --dry-run
.\.venv\Scripts\python.exe scripts\import_x.py posts.csv --profile "@voce"
.\.venv\Scripts\python.exe -m pytest -q                  # testes
.\.venv\Scripts\python.exe -m streamlit run src\dashboard.py       # painel
```

---

## Como ler o painel (e o que ele *não* diz)

- **Horários:** a aba mostra duas coisas diferentes. O heatmap de audiência vem do YouTube
  (dado real de quando as pessoas assistem, só para o canal próprio). O heatmap de desempenho
  vem do seu histórico e é **encolhido para a média** — um único post bom às 3h não vira
  recomendação. A tabela ordena pelo piso do intervalo (P10), não pela média.
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
- Perfis públicos não têm impressões/CTR; no X o engajamento cai para a base de seguidores.
- Importação do X: o CSV vem em UTC (o painel converte para o seu fuso) e cobre até 30 dias por arquivo.
- A estimativa de custo do X é por post lido; confira o billing real no Developer Console.
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
