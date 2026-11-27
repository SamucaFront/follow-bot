# follow-bot

[![tests](../../actions/workflows/tests.yml/badge.svg)](../../actions/workflows/tests.yml)
[![follow-bot](../../actions/workflows/bot.yml/badge.svg)](../../actions/workflows/bot.yml)

Um assistente de networking para o GitHub que roda **inteiramente no GitHub Actions**, sem servidor, VPS ou computador ligado.

Todo dia ele encontra **20 perfis ativos, do mundo inteiro, que costumam seguir de volta**, e publica a lista numa issue. Você segue quem quiser, com um clique. O bot acompanha quem retribuiu e, depois do prazo que você definir, prepara uma lista de unfollow para **você aprovar**.

> **Por que o bot não segue sozinho?** As [Políticas de Uso Aceitável do GitHub](https://docs.github.com/pt/site-policy/acceptable-use-policies/github-acceptable-use-policies) proíbem "rank abuse, such as automated starring or following". Contas que seguem de forma automatizada são suspensas. Por isso o código **não tem** função de seguir: quem segue é sempre você. A única ação automática é deixar de seguir, e só depois do seu `/aprovar`.

---

## Como funciona

```
 a cada hora (cron)            uma vez por dia                     depois de N dias
┌──────────────────┐   ┌──────────────────────────────┐   ┌──────────────────────────────┐
│ sincroniza quem  │──▶│ busca perfis engajados       │   │ quem não seguiu de volta vai │
│ você segue e     │   │ (mundo inteiro) e abre issue │   │ para uma issue de revisão    │
│ quem te segue    │   │ "Sugestões para seguir"      │   │ "Revisar unfollow"           │
└──────────────────┘   └──────────────┬───────────────┘   └──────────────┬───────────────┘
                                      │ você segue manualmente           │ você desmarca quem quer
                                      ▼                                  │ manter e comenta /aprovar
                        próxima execução detecta e                       ▼
                        começa a contar o prazo             bot deixa de seguir os marcados
                                                            (até 20 por execução e por dia)
```

**O que é "engajado e que segue de volta":**

| Sinal | Filtro padrão |
|---|---|
| Proporção seguindo / seguidores perto de 1 (costuma retribuir) | entre 0,5 e 3,0 |
| Atividade pública recente | nos últimos 14 dias |
| Conta real, não vazia | 10 a 5000 seguidores, 3+ repositórios, segue 10+ |
| Não é organização nem bot | `type: User`, sem "bot" no nome |

Cada perfil aprovado recebe uma nota de 0 a 100 e os 20 melhores vão para a issue. As buscas variam ano de criação da conta e ordenação, sem filtro de país, então aparecem perfis do mundo inteiro e não sempre os mesmos.

Ninguém é mencionado nas issues: os perfis aparecem como links, sem `@`, para não notificar ninguém.

---

## Instalação (5 minutos)

### 1. Crie o repositório

Use este repositório como template, ou faça um fork. Ele pode ser **público**: nenhum segredo fica no código.

> O histórico (quem você seguiu, quando, quem retribuiu) fica no branch `bot-data`. Num repositório público essa informação fica visível, mas é a mesma que já aparece no seu perfil. Se preferir, deixe o repositório privado (o GitHub Actions tem 2.000 minutos grátis por mês em repositórios privados, e o bot usa cerca de 1 minuto por execução).

### 2. Crie o token pessoal

1. Acesse **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token** ([link direto](https://github.com/settings/personal-access-tokens/new)).
2. **Token name:** `follow-bot`. **Expiration:** 90 dias (anote para renovar).
3. **Repository access:** *Public repositories (read-only)* é suficiente.
4. **Account permissions → Followers:** *Read and write*.
5. Gere e copie o token. Ele só aparece uma vez.

O bot não pede nenhuma outra permissão. Issues e o branch de dados usam o `GITHUB_TOKEN` automático do Actions.

### 3. Configure o Secret

No repositório: **Settings → Secrets and variables → Actions → New repository secret**

| Nome | Valor |
|---|---|
| `GH_USER_TOKEN` | o token do passo 2 |

O token nunca aparece nos logs: o GitHub mascara secrets automaticamente, e o bot ainda filtra qualquer ocorrência dele antes de escrever no log.

### 4. Ative o Actions

Na aba **Actions**, clique em *I understand my workflows, go ahead and enable them*. Em forks, os workflows agendados vêm desligados até você fazer isso.

### 5. Primeiro teste em modo simulação

**Actions → follow-bot → Run workflow**, marque **Simulação** e rode. Na primeira execução o bot:

- registra quem você já segue (essas contas ficam protegidas: nunca entram na lista de unfollow, a menos que você mude `include_preexisting`);
- se já passou da hora configurada, abre a primeira issue de sugestões.

Quando estiver satisfeito, rode sem simulação, ou só espere: a partir daí ele roda sozinho a cada hora.

---

## Uso no dia a dia

1. **Sugestões:** todo dia (padrão 12:00 UTC, 9h em Brasília) aparece uma issue **"Sugestões para seguir"** com o rótulo `sugestoes`. Abra os perfis e siga quem fizer sentido. A issue do dia anterior é fechada sozinha.
2. **Revisão:** quando alguém que você seguiu não retribui em `wait_days` dias, aparece uma issue **"Revisar unfollow"** com o rótulo `revisao-unfollow`, todos marcados.
3. **Aprovar:** edite a issue, **desmarque** quem quer continuar seguindo e comente `/aprovar`. O comentário dispara o bot na hora.
4. **Cancelar:** comente `/cancelar`, ou só feche a issue.

Regras de segurança da aprovação:

- Só valem comentários **do dono do repositório**, feitos depois da criação da issue.
- Só saem da sua lista de seguidos perfis que **estavam na lista original** e continuam marcados. Acrescentar nomes editando a issue não tem efeito.
- Quem seguir você de volta antes da execução é **pulado**.
- No máximo `max_accounts_per_run` por execução e `max_operations_per_day` por dia. O que sobrar continua nas próximas horas.

---

## Configuração

Tudo fica em [`config.toml`](config.toml), comentado. Os principais:

| Opção | Padrão | O que faz |
|---|---|---|
| `bot.enabled` | `true` | Liga ou desliga o bot |
| `bot.dry_run` | `false` | Simulação: abre issues, mas não deixa de seguir ninguém |
| `suggestions.per_day` | `20` | Perfis sugeridos por dia (máximo 30) |
| `suggestions.hour_utc` | `12` | Hora (UTC) da lista do dia |
| `suggestions.languages` | `[]` | Ex.: `["TypeScript", "Python"]`. Vazio = qualquer |
| `unfollow.wait_days` | `7` | Espera antes de considerar que não retribuiu |
| `unfollow.keep` | `[]` | Perfis que nunca entram na revisão |
| `unfollow.include_preexisting` | `false` | Incluir quem você já seguia antes do bot |
| `limits.max_accounts_per_run` | `20` | Contas processadas por execução (máximo 20) |
| `limits.max_operations_per_day` | `20` | Unfollows por dia (máximo 50) |
| `limits.run_interval_hours` | `1` | Intervalo mínimo entre execuções agendadas |

Os máximos são travas no código: valores acima deles fazem o bot parar com erro de configuração, em vez de rodar num volume que pode violar as regras da plataforma.

**Mudar a frequência:** edite o `cron` em [`.github/workflows/bot.yml`](.github/workflows/bot.yml). Exemplo: `"17 */3 * * *"` roda a cada 3 horas. Ajuste também `run_interval_hours`.

**Sobrescrever sem commit:** em **Settings → Secrets and variables → Actions → Variables**, crie `BOT_ENABLED` (`true`/`false`) ou `DRY_RUN` (`true`/`false`). As variáveis têm prioridade sobre o `config.toml`.

---

## Executar manualmente

**Actions → follow-bot → Run workflow**. Opções:

| Campo | Valores |
|---|---|
| **O que executar** | `all` (tudo), `sync` (só sincroniza), `suggest` (força uma lista de sugestões agora), `review` (só processa revisões) |
| **Simulação** | marcado = não deixa de seguir ninguém |

Execuções manuais ignoram o intervalo mínimo.

---

## Acompanhar os logs

- **Resumo:** cada execução em **Actions → follow-bot** mostra um resumo (novos seguidores, quem deixou de seguir você, unfollows feitos, issues abertas).
- **Log completo:** clique na execução → job `run` → passo **Executar o bot**.
- **Arquivo de log:** cada execução guarda `logs/bot.log` como artifact por 14 dias (seção *Artifacts* da execução).
- **Histórico:** o branch [`bot-data`](../../tree/bot-data) tem o `state.json`, com um commit por execução que mudou algo. O campo `history` guarda os últimos 1.000 eventos.
- **Falhas:** se uma execução agendada falhar, o bot abre uma issue com o rótulo `bot-falha` e o link do log. Falhas seguidas viram comentários na mesma issue.

---

## Interromper a automação

Escolha uma das opções, da mais rápida para a mais definitiva:

1. **Variável:** crie a variável `BOT_ENABLED` = `false` (Settings → Variables). O workflow ainda roda, mas sai sem fazer nada.
2. **Desativar o workflow:** **Actions → follow-bot → ⋯ → Disable workflow**. Para voltar, *Enable workflow*.
3. **Config:** `enabled = false` no `config.toml` e commit.
4. **Revogar o token:** apague o token em Settings → Developer settings. O bot perde o acesso na hora.

Para recomeçar do zero, apague o branch `bot-data`.

---

## Rodar localmente

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt

pytest                            # testes

export GH_USER_TOKEN=...          # Windows PowerShell: $env:GH_USER_TOKEN="..."
export GITHUB_TOKEN=...           # token com acesso a Issues do repositório
export GITHUB_REPOSITORY=seu-usuario/follow-bot
export DRY_RUN=true
python src/main.py --manual
```

Localmente o estado fica em `data/state.json` (ignorado pelo git).

---

## Estrutura

```
follow-bot/
├── .github/workflows/
│   ├── bot.yml              # execução a cada hora, manual e por comentário /aprovar
│   └── tests.yml            # pytest em cada push e pull request
├── src/
│   ├── main.py              # orquestra a execução, logs e resumo
│   ├── config.py            # lê config.toml + variáveis, aplica os tetos
│   ├── follow_manager.py    # sincronização, descoberta e issue de sugestões
│   ├── unfollow_manager.py  # reciprocidade, issue de revisão e aprovação
│   ├── scheduler.py         # o que está na hora de fazer e limites diários
│   ├── storage.py           # state.json com escrita atômica
│   └── social_api.py        # cliente da API REST oficial do GitHub (sem "follow")
├── tests/                   # 50 testes, sem acesso à rede
├── config.toml
├── requirements.txt
└── .env.example
```

### Persistência entre execuções

Cada execução do Actions começa numa máquina limpa. O workflow:

1. baixa o branch `bot-data` para a pasta `.bot-data/` (usando `git worktree`); na primeira vez, cria o branch vazio;
2. roda o bot, que lê e grava `.bot-data/state.json`. O estado é salvo depois de cada etapa, então uma falha no meio não perde o que já foi feito;
3. faz commit e push do `state.json` para o `bot-data`, mesmo se o bot falhar.

Os logs vão para artifacts (temporários, 14 dias). O histórico operacional fica no git (permanente e versionado). A `concurrency` do workflow garante que duas execuções nunca escrevam ao mesmo tempo.

### Limites da API

O bot usa algumas centenas de chamadas por dia com o seu token (a sincronização de hora em hora mais a análise de até 80 perfis por dia), muito abaixo do limite de 5.000 por hora. Se o limite for atingido, a execução para e tenta de novo na hora seguinte.

---

## Avisos

- **Workflows agendados pausam em repositórios inativos.** Num repositório público sem atividade por 60 dias, o GitHub desativa os workflows agendados e avisa por e-mail. Para reativar: **Actions → follow-bot → Enable workflow**.
- **O token expira.** Quando expirar, a execução falha e o bot abre uma issue `bot-falha`. Gere outro token e atualize o secret `GH_USER_TOKEN`.
- **Use com bom senso.** Siga pessoas cujo trabalho te interessa de verdade. O objetivo é networking, não inflar números.

## Licença

[MIT](LICENSE)
