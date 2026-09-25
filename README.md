# Bot de Copy Trading para Polymarket

Script em Python que acompanha as operações recentes de uma carteira na Polymarket e, conforme os filtros e limites configurados, envia ordens de compra correspondentes para a sua própria conta.

> **Atenção:** o arquivo `test.py` está configurado com `DRY_RUN = False`. Nesse modo, o bot pode enviar ordens reais e usar saldo da sua conta. Para começar, altere para `DRY_RUN = True` e confira o comportamento antes de habilitar operações reais. Copy trading envolve risco de perda; não há garantia de lucro.

## O que o bot faz

- Consulta a atividade pública de uma carteira pela Data API da Polymarket.
- Ao iniciar, sincroniza as operações recentes e passa a acompanhar novas operações.
- Por padrão, considera apenas compras (`BUY`) em mercados cujos títulos contenham `Bitcoin`, `Ethereum` ou `XRP`.
- Busca o token do resultado correspondente pela Gamma API e envia uma ordem limite GTC pelo CLOB.
- Salva as transações já processadas e o último horário acompanhado em `polymarket_copy_state.json`, evitando copiá-las novamente.
- Consulta o saldo antes de enviar uma ordem.

## Requisitos

- Python 3
- Acesso à internet para consultar as APIs da Polymarket
- Uma carteira e credenciais configuradas, caso vá habilitar ordens reais

Instale as bibliotecas usadas pelo script:

```bash
python -m pip install requests python-dotenv py-clob-client
```

## Configuração

1. Em `test.py`, substitua `TARGET_WALLET` pelo endereço público da carteira que deseja acompanhar.
2. Revise os filtros e limites no início de `test.py`:

   - `DRY_RUN`: `True` simula as operações; `False` habilita o envio de ordens.
   - `WHITELIST_WORDS`: palavras que devem aparecer no título do mercado. Uma lista vazia remove esse filtro.
   - `BET_MODE`: `FIXED` usa valor fixo; `RATIO` calcula uma proporção da operação acompanhada.
   - `FIXED_AMOUNT` e `COPY_RATIO`: definem o valor ou a proporção copiada.
   - `MAX_USDC_PER_TRADE` e `MIN_USDC_PER_TRADE`: limitam o valor por operação.
   - `POLL_SECONDS`: intervalo entre consultas.

3. Para ordens reais, crie um arquivo `.env` local com as variáveis esperadas pelo script:

   ```dotenv
   POLY_PRIVATE_KEY=sua_chave_privada
   POLY_FUNDER=endereco_da_carteira_de_fundos
   POLY_SIGNATURE_TYPE=1
   ```

   `POLY_SIGNATURE_TYPE` é opcional no script e assume `1` quando omitido. Nunca compartilhe sua chave privada nem a envie para o GitHub. Se alguma credencial real já tiver sido publicada, revogue-a ou substitua-a.

## Executar

Na pasta do projeto, rode:

```bash
python test.py
```

Mantenha `DRY_RUN = True` enquanto verifica os filtros e a configuração. Quando `DRY_RUN = False`, o script tenta autenticar, consulta o saldo e pode enviar ordens limite reais na rede Polygon. Interrompa a execução com `Ctrl+C`.

## Observações

- O script acompanha somente novas operações após a sincronização inicial; ele não copia o histórico já existente.
- As ordens são enviadas como GTC no preço observado para a operação acompanhada. O preço e a execução podem mudar antes que a ordem seja preenchida.
- O arquivo `polymarket_copy_state.json` guarda o estado local do acompanhamento. Apagá-lo faz o bot iniciar uma nova sincronização.
- Este projeto é fornecido para estudo e uso por conta e risco de quem o executa. Revise o código e os limites antes de conectar uma carteira.
