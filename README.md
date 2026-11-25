# Bot de copy trading para Polymarket

O bot acompanha novas compras de uma carteira pela Data API v2 e prepara ordens limite GTC pelo SDK oficial `polymarket-client`. A configuração inicial usa simulação e filtra títulos que contêm Bitcoin, Ethereum ou XRP. Vendas e operações de combo ficam fora da estratégia.

## Executar

Use Python 3.11 ou superior. Na pasta do projeto, instale as dependências:

```bash
python -m pip install -r requirements.txt
```

Copie `.env.example` para `.env` e preencha `TARGET_WALLET` com o endereço público que quer acompanhar. Execute:

```bash
python test.py
```

Com `DRY_RUN = True`, basta o endereço público. O bot consulta o livro e mostra preço, quantidade e custo das ordens simuladas. Saldo, autorização da conta e preenchimento dependem da execução real.

Para operar com saldo real, preencha `POLY_PRIVATE_KEY` e `POLY_FUNDER` no `.env` local e altere `DRY_RUN` para `False` em `test.py`. O SDK identifica o tipo da carteira. Uma ordem aceita pode ficar aberta até encontrar contraparte ou ser cancelada. Uma compra real pode resultar em perda.

## Filtros e valores

Ajuste as constantes no início de `test.py`. Uma lista `WHITELIST_WORDS` vazia libera todos os títulos. `BET_MODE = "FIXED"` usa `FIXED_AMOUNT`. Em `"RATIO"`, o valor é a compra observada multiplicada por `COPY_RATIO`. Os limites históricos `MIN_USDC_PER_TRADE` e `MAX_USDC_PER_TRADE` se aplicam ao valor da ordem em pUSD.

O bot ajusta o preço para baixo ao incremento do livro, arredonda a quantidade para baixo e verifica o mínimo de cotas do mercado. Com o padrão de 0,50 pUSD, uma compra a 0,50 gera 1 cota e será ignorada se o livro exigir 5. O bot não aumenta o orçamento para chegar ao mínimo.

## Acompanhamento e recuperação

Cada sessão começa no próximo segundo. O bot percorre todas as páginas de cada consulta e relê os últimos 60 segundos para captar registros publicados com atraso. Atividade publicada depois dessa janela, ou ocorrida enquanto o bot estava desligado, pode ficar de fora. As compras identificadas ficam em um arquivo SQLite local por alvo e por conta de execução, separado da simulação. O JSON da versão anterior representa hashes de transações inteiras e não é usado para disparar ordens antigas.

Uma falha de consulta ou preparação mantém a compra pendente para nova tentativa, inclusive após reiniciar. Antes do envio, o bot grava a intenção no banco. Se a resposta se perder ou o processo parar durante o envio, a operação fica como `unconfirmed` e aparece no console na próxima inicialização. Confira ordens abertas e histórico de negociações na conta antes de decidir se precisa intervir. Preserve o SQLite para manter esse registro. O estado `accepted` indica que a corretora aceitou a ordem, que ainda pode estar aberta.

## Testes

```bash
python -m unittest -v test_copy_bot
```

Os testes usam o SDK instalado, respostas HTTP simuladas e um banco temporário. Não exigem carteira.
