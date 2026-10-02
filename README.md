# Ponto de Coleta Adelinha — versão de teste

Sistema web responsivo para cadastro público, gestão interna de pacotes, aviso manual por WhatsApp, cálculo de prazo e retirada com comprovante. Os dados ficam em SQLite no servidor, não no navegador.

## Link da prévia

**[Abrir a versão de teste do Ponto de Coleta Adelinha](https://ponto-coleta-adelinha-teste.onrender.com)**

O endereço acima corresponde ao serviço `ponto-coleta-adelinha-teste` configurado em `render.yaml`. A versão nova aparecerá nesse mesmo link depois que esta branch for enviada ao GitHub, mesclada na branch acompanhada pelo Render e o deploy terminar com sucesso. O link não muda a cada atualização.

Para confirmar que a atualização entrou no ar, abra também o [health check da prévia](https://ponto-coleta-adelinha-teste.onrender.com/health). Ele deve mostrar `{"status":"ok"}`. Se o Render estiver configurado para exigir autenticação de acesso à prévia, entre primeiro com a conta autorizada no painel do Render.

> **Atenção:** esta versão usa dados e credenciais de demonstração. A minuta deve ser revisada e a segurança operacional validada antes de atender clientes reais.

## 1. Executar localmente (passo a passo)

Requer Python 3.11 ou superior.

1. Instale o **Python 3.11 ou mais recente** pelo site oficial do Python. No Windows, marque “Add Python to PATH”.
2. Baixe este projeto e abra um Terminal dentro da pasta dele.
3. No Linux/macOS, execute `chmod +x scripts/setup.sh && ./scripts/setup.sh`. O script cria um ambiente isolado, instala tudo e roda os testes.
4. Inicie com `.venv/bin/python app.py`. No Windows, use `.venv\Scripts\python app.py`.
5. Abra `http://localhost:5000` no navegador. Para encerrar, volte ao Terminal e pressione `Ctrl+C`.

Para incluir o exemplo fictício antes de iniciar:

```bash
.venv/bin/python seed_demo.py
```

O painel fica em `http://localhost:5000/login`; **somente no teste local**, use `admin` / `adelinha-teste`.

### Se aparecer `403 Forbidden` ao instalar

Esse erro não vem do sistema Adelinha: significa que a rede bloqueou o acesso do `pip` ao PyPI. Neste ambiente automatizado, tanto PyPI quanto os repositórios do Ubuntu foram bloqueados pelo proxy; trocar a versão do Flask não resolve. Tente, nesta ordem:

1. Execute o processo em sua internet residencial ou compartilhe a internet do celular.
2. Se estiver numa empresa/escola, peça ao suporte o endereço do **espelho PyPI** e execute `PIP_INDEX_URL=https://ENDERECO-DO-ESPELHO/simple ./scripts/setup.sh`.
3. Como alternativa, instale o Docker Desktop e execute `docker build -t adelinha-teste .` e depois `docker run --rm -p 5000:5000 -v adelinha-dados:/data adelinha-teste`. O Docker também precisa de acesso à internet na primeira construção.

Não desative o antivírus, o certificado TLS ou a verificação HTTPS para contornar o erro.

## Testar

```bash
pytest -q
```

Os testes cobrem proteção do painel, limites exatos das categorias, bloqueio de excesso, cobrança por datas corridas e retirada parcial/duplicada.

O endpoint `http://localhost:5000/health` deve responder `{"status":"ok"}` quando o servidor e o banco estiverem funcionando.

## Configuração para produção

Defina variáveis de ambiente antes de iniciar:

```bash
export SECRET_KEY="gere-uma-chave-longa-e-aleatoria"
export STAFF_USER="usuario-da-equipe"
export STAFF_PASSWORD_HASH="$(python -c 'from werkzeug.security import generate_password_hash; print(generate_password_hash("SENHA-FORTE-AQUI"))')"
export DATABASE="/caminho/persistente/adelinha.db"
export PORT=5000
export COOKIE_SECURE=1
```

Execute atrás de HTTPS com um servidor WSGI, por exemplo `gunicorn 'app:create_app()'`. Faça backup frequente do arquivo configurado em `DATABASE`. Para mais de um processo/servidor ou crescimento do uso, migre SQLite para PostgreSQL.

## 2. Salvar suas alterações no GitHub

Crie uma conta gratuita no GitHub e um repositório **privado**, sem adicionar README ou `.gitignore` pela tela. Dentro desta pasta, execute (troque o endereço pelo exibido no seu repositório):

```bash
git status
git add .
git commit -m "Descreva aqui a alteração"
git remote add origin https://github.com/SEU-USUARIO/SEU-REPOSITORIO.git
git push -u origin work
```

Nas próximas alterações, bastam `git add .`, `git commit -m "mensagem"` e `git push`. Se o GitHub pedir senha no Terminal, use um **Personal Access Token** ou entre com GitHub Desktop; a senha comum da conta não é aceita para `git push`.

Não envie o arquivo `data/adelinha.db`: ele contém dados pessoais e já está ignorado pelo Git. Nunca coloque senhas, `SECRET_KEY` ou cópias do banco no GitHub.

## 3. Abrir uma versão de teste na internet

O projeto inclui `render.yaml`, pronto para uma prévia no Render com HTTPS e disco persistente. Antes de começar, gere o hash da sua senha:

```bash
.venv/bin/python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('COLOQUE-UMA-SENHA-FORTE-AQUI'))"
```

Copie toda a linha exibida. Depois:

1. Acesse Render, entre com GitHub e escolha **New > Blueprint**.
2. Autorize apenas o repositório privado deste projeto e selecione-o.
3. Confirme o arquivo `render.yaml` e o serviço sugerido.
4. Quando solicitado, cole a linha no valor de `STAFF_PASSWORD_HASH`; marque-a como secreta. Não use a senha de demonstração.
5. Aguarde o deploy e abra a URL terminada em `.onrender.com`. Teste `/health`, faça um cadastro fictício, registre um pacote, confirme o aviso e faça uma retirada.
6. Verifique no painel do Render que o disco `adelinha-data` está anexado em `/var/data`. Sem disco persistente, os registros podem desaparecer em um novo deploy.

Essa URL é uma **versão de teste**. Não cadastre CPFs ou documentos reais até concluir revisão jurídica/LGPD, backup, controle de acesso e testes operacionais. Quando estiver satisfeito, conecte um domínio em **Settings > Custom Domains** e mantenha HTTPS habilitado.

## 4. Serviços, contas e custos

1. **Domínio** (opcional, recomendado): conta em um registrador, normalmente R$ 40–70/ano para `.com.br`.
2. **Hospedagem com disco persistente e HTTPS**: Render, Railway, Fly.io, VPS ou equivalente. O `render.yaml` escolhe um plano pago porque banco e assinaturas não podem ficar em armazenamento temporário. Confira o preço atual antes de confirmar; preços mudam.
3. **Repositório Git privado**: GitHub/GitLab possuem opções gratuitas.
4. **Backup externo criptografado**: armazenamento em nuvem; o custo depende de retenção e volume.
5. **WhatsApp**: nesta versão o envio é manual pelo link `wa.me`, sem API paga. Uma automação futura exigirá conta Meta Business/WhatsApp Business Platform e cobrança vigente da Meta/provedor.
6. **Pix**: conta bancária empresarial apta a receber pela chave CNPJ. Eventuais tarifas dependem do banco.

Antes da produção: revise a minuta com profissional jurídico/LGPD, troque as credenciais, restrinja quem acessa o painel, configure HTTPS, backups e política de retenção, e faça teste de restauração. CPF, documentos e assinaturas nunca são mostrados na área pública; por conter dados pessoais, o banco e seus backups devem ter acesso rigorosamente limitado.

## Atualizar no Windows sem perder dados

1. **Pare o sistema** e localize `data\adelinha.db` (ou o caminho definido em `DATABASE`).
2. Copie esse arquivo para uma pasta segura, com data no nome, por exemplo `Backup\adelinha-2026-09-30.db`. Não continue sem esse backup.
3. Abra PowerShell na pasta do projeto e execute `git status`. Se houver alterações suas, salve-as com `git add .` e `git commit -m "backup antes da atualização"`.
4. Execute `git pull origin work` para baixar a nova versão.
5. Ative o ambiente com `.venv\Scripts\Activate.ps1` e execute `python -m pip install -r requirements.txt`. O pacote `tzdata` agora está incluído para o fuso funcionar no Windows.
6. Execute `python -m pytest -q`. Depois inicie com `python app.py`.
7. Na primeira inicialização, a migração adiciona as novas colunas e tabelas sem recriar clientes, IDs, assinaturas, pacotes ou retiradas. Clientes antigos continuam sem senha até a equipe conferir a identidade e gerar o link temporário no cadastro completo.
8. Confira alguns registros. Se houver problema, pare o sistema e restaure a cópia do banco feita no passo 2.

Nunca substitua nem apague `adelinha.db` durante uma atualização. Faça também uma cópia antes de cada deploy ou mudança de versão.

Nesta atualização, a inicialização também acrescenta os campos de histórico do comprovante. Novas retiradas passam a guardar uma fotografia dos IDs, nome do cliente, valores, atraso e pagamento. Retiradas antigas são preservadas; campos que nunca foram gravados aparecem como **“Não registrado”**. A geração do PDF usa `reportlab`, instalado automaticamente pelo `requirements.txt`.

### Dados do Pix

O titular é **AMOR INFINITO MARKETING E SOLUCOES EMPRESARIAIS** e a chave CNPJ é `38145273000105`. A tela mostra o nome completo. Ao gerar o QR Code e o Pix Copia e Cola, o sistema aplica automaticamente o limite de 25 caracteres do padrão técnico Pix; a conferência do crédito continua manual.

### Migração dos endereços

A primeira inicialização acrescenta campos separados para o endereço residencial sem apagar registros. Cadastros antigos ficam identificados como **Não cadastrado** até a agência preenchê-los na edição do cliente. Novos cadastros exigem CEP, rua, número, bairro, cidade e estado. O endereço Pickup permanece separado e não é copiado para o endereço residencial.

### O que a migração preserva

A inicialização acrescenta, sem apagar tabelas, os campos de cliente ativo e versão de sessão, tipo de documento, confirmação de pagamento, auditoria e configuração Pix. Contratos, IDs, assinaturas, pacotes, retiradas e comprovantes existentes permanecem no mesmo banco. Informações que não eram gravadas em registros antigos aparecem como **Não registrado**.

## Regras implementadas

- ID de cliente `ADL-000001` e pacote `PCT-000001` sequenciais.
- Aceite guarda versão, assinatura e data/hora em `America/Sao_Paulo`.
- Pequeno: soma ≤ 80 cm **e** peso ≤ 10 kg; grande: soma ≤ 150 cm **e** peso ≤ 20 kg; acima disso é recusado.
- Confirmação explícita inicia o prazo; abrir/reabrir WhatsApp não o altera.
- O dia do aviso é o primeiro dos quatro; R$ 0,50 por data corrida desde o quinto dia.
- Retirada total ou parcial, por Pix ou dinheiro com confirmação manual, identificação e assinatura; pacote retirado não pode ser retirado novamente.
- Comprovante persistente e imprimível/salvável em PDF pelo navegador.
