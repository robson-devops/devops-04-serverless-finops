# Validação na AWS: Projeto 4

Cada camada foi validada na AWS antes de construir a camada que depende dela.
Os comandos rodam da raiz do projeto, na região us-east-1. Os horários são os
medidos em 01/10/2026, em UTC.

## 1. Bootstrap

```bash
aws cloudformation describe-stacks \
  --stack-name devops-04-bootstrap \
  --query 'Stacks[0].[StackStatus,Outputs[].[OutputKey,OutputValue]]' \
  --output text --region us-east-1
```

Esperado: `CREATE_COMPLETE`, `ArtifactBucketName`, `PipelineRoleArn` e
`CfnExecutionRoleArn`.

## 2. Stack base

O primeiro deploy da `devops-04-app` foi feito já com `--role-arn` da role de
execução, para testar as permissões dela desde o início.

```bash
aws cloudformation describe-stacks \
  --stack-name devops-04-app \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
  --output text --region us-east-1
```

Esperado: `ReportBucketName`, `AlertTopicArn` e `DeadLetterQueueArn`.

## 3. waste-scanner

Conta limpa primeiro:

```bash
aws lambda invoke --function-name devops-04-waste-scanner \
  --region us-east-1 /tmp/scan.json && cat /tmp/scan.json
```

Resultado: `{"findings": 0, "monthly_usd_total": 0, ...}` e nenhum e-mail.

Desperdício criado de propósito, com a tag `Project=devops-04-teste`:

```bash
aws ec2 create-volume --availability-zone us-east-1a --size 1 --volume-type gp3 \
  --tag-specifications 'ResourceType=volume,Tags=[{Key=Project,Value=devops-04-teste}]' \
  --query VolumeId --output text --region us-east-1
aws ec2 allocate-address --domain vpc \
  --tag-specifications 'ResourceType=elastic-ip,Tags=[{Key=Project,Value=devops-04-teste}]' \
  --query AllocationId --output text --region us-east-1
aws logs create-log-group --log-group-name /devops-04/teste-sem-retencao \
  --tags Project=devops-04-teste --region us-east-1
```

O `allocate-address` rodou duas vezes por engano, e a varredura achou os dois
IPs: um teste melhor do que o planejado.

| Achado | Custo estimado/mês |
|---|---|
| `elastic_ip_unassociated` (2×) | US$ 3,65 cada |
| `ebs_volume_unattached` 1 GB gp3 | US$ 0,08 |
| `log_group_no_retention` | US$ 0,00 |
| **Total** | **US$ 7,38** |

Execução às 18:09:25, função terminou em ~2 s, e-mail `[FinOps] 4 item(ns)
sem uso, ~US$ 7.38/mês` às 18:09. Depois de apagar os recursos de teste, a
varredura voltou a 0 achados.

## 4. tag-guard

Instância sem `Environment` e `Owner`:

```bash
aws ec2 run-instances \
  --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --instance-type t4g.nano --count 1 \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Project,Value=devops-04-teste}]' \
  --query 'Instances[0].InstanceId' --output text --region us-east-1
```

| Momento | Hora |
|---|---|
| `run-instances` | 18:22:09 |
| E-mail `[FinOps] Instância sem tags obrigatórias` | 18:22 |

Tag gravada pela função, com a instância ainda rodando:

```bash
aws ec2 describe-tags --filters Name=resource-id,Values=<instância> \
  --query 'Tags[].[Key,Value]' --output text --region us-east-1
```

Esperado: `TagCompliance  faltando:Environment+Owner`.

## 5. office-hours

Instância com as três tags obrigatórias e `Schedule=office-hours`:

```bash
aws lambda invoke --function-name devops-04-office-hours \
  --payload '{"action": "stop"}' --region us-east-1 /tmp/oh.json && cat /tmp/oh.json
aws lambda invoke --function-name devops-04-office-hours \
  --payload '{"action": "start"}' --region us-east-1 /tmp/oh.json && cat /tmp/oh.json
```

| Ação | Resultado |
|---|---|
| `stop` | só a instância de teste na lista; estado `stopped` |
| `start` | a mesma instância de volta a `running` |
| `tag-guard` na volta | sem `TagCompliance` e sem e-mail: nenhum alarme falso |

## 6. Alarmes e DLQ

Erro síncrono:

```bash
aws lambda invoke --function-name devops-04-office-hours \
  --payload '{"action": "reboot"}' --region us-east-1 /tmp/oh.json
```

| Momento | Hora |
|---|---|
| Erro provocado | 18:56:23 |
| Alarme `devops-04-office-hours-errors` em ALARM e e-mail | 18:57:02 (39 s) |

Erro assíncrono, que esgota as tentativas e vai para a DLQ:

```bash
aws lambda invoke --function-name devops-04-office-hours \
  --invocation-type Event \
  --payload '{"action": "reboot"}' --region us-east-1 /tmp/oh.json
```

| Momento | Hora |
|---|---|
| Chamada aceita (`202`) | 19:00:53 |
| 1ª execução | 19:00:55 |
| 2ª execução | 19:01:51 |
| 3ª execução e envio à DLQ | ~19:03:51 |

A mensagem na DLQ guarda o evento e o motivo:

```bash
aws sqs receive-message \
  --queue-url "$(aws sqs get-queue-url --queue-name devops-04-dlq \
      --query QueueUrl --output text --region us-east-1)" \
  --message-attribute-names All --visibility-timeout 0 \
  --query 'Messages[0].[Body,MessageAttributes.ErrorMessage.StringValue]' \
  --output text --region us-east-1
```

Resultado: `{"action": "reboot"}  action deve ser stop ou start, recebido: 'reboot'`.
O alarme `devops-04-dlq-not-empty` mandou o e-mail depois que a métrica da
fila, inativa até então, voltou a ser publicada.

### Incidente durante a validação

Os primeiros e-mails de alarme não chegaram. O diagnóstico, na ordem:

1. `describe-alarm-history` mostrou `Successfully executed action` para o SNS.
2. A métrica `NumberOfMessagesPublished` do tópico mostrou as mensagens dos
   alarmes; `NumberOfNotificationsDelivered` mostrou entrega só até 18:22.
3. `list-subscriptions-by-topic` mostrou a assinatura como `Deleted`: tinha
   sido cancelada por um clique no link "unsubscribe" de um e-mail.

Correção: nova assinatura confirmada pela CLI com
`--authenticate-on-unsubscribe true`. Conferência:

```bash
aws sns get-subscription-attributes \
  --subscription-arn <arn da assinatura> \
  --query 'Attributes.ConfirmationWasAuthenticated' \
  --output text --region us-east-1
```

Esperado: `true`. Esse passo virou parte da instalação no README.

## 7. Pipeline

| Execução | Duração |
|---|---|
| Primeiro push do workflow (sem mudança de código, "no changes to deploy") | 3 min 38 s |
| Push com mudança no `waste-scanner` | 1 min 59 s |

```bash
aws lambda get-function-configuration --function-name devops-04-waste-scanner \
  --query '[LastModified,CodeSha256]' --output text --region us-east-1
```

Resultado: `LastModified` 19:32:43, com `CodeSha256` novo.

## 8. Encerramento

Antes de encerrar, a verificação encontrou todos os recursos do projeto
(funções, fila, regra, agendamentos, stacks, tópico, log groups, buckets,
provider OIDC e roles), o que prova que ela enxerga o que precisa.

```bash
./scripts/encerrar.sh
./scripts/verificar-cobranca.sh us-east-1
```

| Passo | Resultado |
|---|---|
| `encerrar.sh` | 19:39:03 a 19:42:21 (3 min 18 s), sem passo manual |
| `verificar-cobranca.sh us-east-1` | `Nada encontrado nas regiões verificadas nem nos serviços globais.` |
