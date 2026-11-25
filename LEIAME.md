# Painel Ouro na web (grátis)

Versão web do Painel_Ouro_v2.mq5: calcula o mesmo score (-10..+10), confirmações,
votação técnica e gatilho, e mostra numa página que você abre no celular.
Roda sozinho no GitHub, sem MT5 ligado e sem servidor.

## Instalação (uns 10 minutos, tudo pelo navegador do celular ou PC)

1. Crie uma conta grátis em github.com e um repositório novo (ex.: `painel-ouro`).
   Pode ser público (o Pages grátis exige público, a menos que você tenha plano pago).
   Atenção: tudo no repositório fica visível, então não coloque senhas nos arquivos.
2. Envie todos os arquivos deste zip para o repositório (botão Add file > Upload files),
   mantendo as pastas `docs/` e `.github/workflows/`.
3. Em Settings > Pages: Source = "Deploy from a branch", Branch = `main`, pasta = `/docs`. Salve.
   Seu painel ficará em `https://SEU-USUARIO.github.io/painel-ouro/`.
4. Em Actions, abra "Atualizar painel" e clique em "Run workflow" para gerar o primeiro
   `data.json`. Depois ele roda sozinho a cada ~10 minutos.
5. Abra o link no celular e use "Adicionar à tela inicial" para virar um ícone.

## Avisos no Telegram (opcional)

1. No Telegram, fale com @BotFather, envie `/newbot` e guarde o token.
2. Mande qualquer mensagem para o seu bot e abra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` para achar o seu `chat id`.
3. No GitHub: Settings > Secrets and variables > Actions > New repository secret:
   `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`.
4. Você recebe aviso quando o gatilho vira Compra/Venda ou o sinal macro muda.

## Ajustes (arquivo config.json, dá para editar pelo app do GitHub)

- `geo` (baixo, neutro, moderado, elevado) e `tes` (true/false): os dois fatores manuais do EA.
- `min_score`, `min_conf`, `min_atr_spread`: mesmas regras do EA.
- `assumed_spread`: o Yahoo não informa spread, então é um valor fixo (padrão 0,30).
  Ajuste para o spread típico da sua conta.
- `tf_regime` / `tf_trigger`: timeframes (padrão 1h e 5m).
- `watchlist`: pares mostrados no fim da página.

## Diferenças em relação ao MT5

- Dados do Yahoo Finance (XAUUSD=X, DX-Y.NYB) e FRED, não da sua corretora. O DXY vem do
  índice real, não do USDINDEX.fs. Preços e variações do dia podem diferir um pouco.
- O GitHub agenda a cada 10 min, mas pode atrasar. O macro (H1) funciona bem assim;
  o gatilho de 5 minutos fica atrasado em relação ao mercado, então use como referência.
- Se o repositório ficar 60 dias sem atividade, o GitHub pode pausar a rotina. É só
  reativar em Actions.
- Fim de semana: a página avisa quando o último preço está velho.
