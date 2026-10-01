#!/usr/bin/env bash
#
# verificar-cobranca.sh
#
# Procura recursos que sobraram na conta AWS depois do destroy.
# Somente leitura.
#
# Uso:
#   ./scripts/verificar-cobranca.sh                 # todas as regiões habilitadas
#   ./scripts/verificar-cobranca.sh us-east-1       # apenas as regiões informadas
#   ./scripts/verificar-cobranca.sh us-east-1 sa-east-1
#
# Código de saída:
#   0 = nada encontrado
#   1 = há recursos que ficaram para trás
#   2 = alguma verificação falhou (permissão ou serviço indisponível) e o
#       resultado pode estar incompleto
#
# Compatível com o bash 3.2 do macOS.

set -uo pipefail

FOUND=0
ERRORS=0
ERROR_LIST=""

# Ignora saída vazia e o "None" do AWS CLI.
report() {
  local label="$1" out="$2"
  out=$(printf '%s\n' "$out" | sed -e '/^None$/d' -e '/^[[:space:]]*$/d')
  if [ -n "$out" ]; then
    FOUND=$((FOUND + 1))
    echo "  [!] $label"
    printf '%s\n' "$out" | sed 's/^/        /'
  fi
}

# Falha na consulta é registrada, nunca tratada como "nada encontrado".
check() {
  local region="$1" label="$2"
  shift 2
  local out
  if ! out=$(aws "$@" --region "$region" --output text 2>&1); then
    ERRORS=$((ERRORS + 1))
    ERROR_LIST="${ERROR_LIST}    ${region} · ${label}: $(printf '%s' "$out" | head -n1)
"
    return 0
  fi
  report "$label" "$out"
}

check_global() {
  local label="$1"
  shift
  local out
  if ! out=$(aws "$@" --output text 2>&1); then
    ERRORS=$((ERRORS + 1))
    ERROR_LIST="${ERROR_LIST}    global · ${label}: $(printf '%s' "$out" | head -n1)
"
    return 0
  fi
  report "$label" "$out"
}

# --- identidade ---------------------------------------------------------

if ! IDENTITY=$(aws sts get-caller-identity --query "[Account,Arn]" --output text 2>&1); then
  echo "Não foi possível identificar a conta AWS:"
  echo "  $IDENTITY"
  echo "Confira as credenciais com: aws sts get-caller-identity"
  exit 2
fi

echo "Conta: $(printf '%s' "$IDENTITY" | awk '{print $1}')"
echo "Identidade: $(printf '%s' "$IDENTITY" | awk '{print $2}')"
echo

# --- regiões ------------------------------------------------------------

if [ "$#" -gt 0 ]; then
  REGIONS="$*"
else
  if ! REGIONS=$(aws ec2 describe-regions --query "Regions[].RegionName" --output text 2>&1); then
    echo "Não foi possível listar as regiões: $REGIONS"
    exit 2
  fi
fi

echo "Regiões verificadas: $(printf '%s' "$REGIONS" | wc -w | tr -d ' ')"
echo

# --- varredura regional -------------------------------------------------

for region in $REGIONS; do
  before=$FOUND
  echo "== $region"

  check "$region" "EC2: instâncias não encerradas (paradas ainda cobram disco)" \
    ec2 describe-instances \
    --filters "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query "Reservations[].Instances[].[InstanceId,State.Name,InstanceType]"

  check "$region" "EBS: volumes" \
    ec2 describe-volumes \
    --query "Volumes[].[VolumeId,State,join('', [to_string(Size), ' GB'])]"

  check "$region" "EBS: snapshots próprios" \
    ec2 describe-snapshots --owner-ids self \
    --query "Snapshots[].[SnapshotId,join('', [to_string(VolumeSize), ' GB']),StartTime]"

  check "$region" "Elastic IPs (IPv4 público é cobrado desde 2024)" \
    ec2 describe-addresses \
    --query "Addresses[].[PublicIp,AllocationId,AssociationId]"

  check "$region" "NAT Gateways (custo fixo por hora)" \
    ec2 describe-nat-gateways \
    --filter "Name=state,Values=pending,available" \
    --query "NatGateways[].[NatGatewayId,State,VpcId]"

  check "$region" "VPC endpoints do tipo Interface (custo por hora)" \
    ec2 describe-vpc-endpoints \
    --filters "Name=vpc-endpoint-type,Values=Interface" \
    --query "VpcEndpoints[].[VpcEndpointId,ServiceName]"

  check "$region" "Load balancers (ALB/NLB)" \
    elbv2 describe-load-balancers \
    --query "LoadBalancers[].[LoadBalancerName,Type,State.Code]"

  check "$region" "Load balancers clássicos" \
    elb describe-load-balancers \
    --query "LoadBalancerDescriptions[].LoadBalancerName"

  check "$region" "RDS: instâncias" \
    rds describe-db-instances \
    --query "DBInstances[].[DBInstanceIdentifier,DBInstanceStatus,DBInstanceClass]"

  check "$region" "RDS: clusters (Aurora)" \
    rds describe-db-clusters \
    --query "DBClusters[].[DBClusterIdentifier,Status]"

  check "$region" "RDS: snapshots manuais" \
    rds describe-db-snapshots --snapshot-type manual \
    --query "DBSnapshots[].[DBSnapshotIdentifier,join('', [to_string(AllocatedStorage), ' GB'])]"

  # Cluster ECS vazio não cobra; task em execução sim.
  clusters=$(aws ecs list-clusters --region "$region" --query "clusterArns" --output text 2>/dev/null | sed '/^None$/d')
  if [ -n "$clusters" ]; then
    # shellcheck disable=SC2086,SC2016
    check "$region" "ECS: clusters com tasks em execução" \
      ecs describe-clusters --clusters $clusters \
      --query 'clusters[?runningTasksCount>`0`].[clusterName,runningTasksCount]'
  fi

  # Revisões registradas pelo pipeline ficam fora do state.
  check "$region" "ECS: task definitions ativas (sem custo, não devem sobrar)" \
    ecs list-task-definitions --status ACTIVE --query "taskDefinitionArns[]"

  check "$region" "ECS: task definitions inativas (sem custo, não devem sobrar)" \
    ecs list-task-definitions --status INACTIVE --query "taskDefinitionArns[]"

  check "$region" "EKS: clusters (cobrança fixa por hora)" \
    eks list-clusters --query "clusters"

  check "$region" "ECR: repositórios (armazenamento de imagens)" \
    ecr describe-repositories --query "repositories[].repositoryName"

  check "$region" "ElastiCache: clusters" \
    elasticache describe-cache-clusters \
    --query "CacheClusters[].[CacheClusterId,CacheNodeType]"

  check "$region" "DynamoDB: tabelas" \
    dynamodb list-tables --query "TableNames"

  check "$region" "Secrets Manager: secrets (cobrança mensal por secret)" \
    secretsmanager list-secrets --query "SecretList[].Name"

  check "$region" "KMS: chaves gerenciadas pelo cliente (cobrança mensal por chave)" \
    kms list-aliases \
    --query "Aliases[?!starts_with(AliasName, 'alias/aws/')].AliasName"

  check "$region" "Lambda: funções (sem custo parado, não devem sobrar)" \
    lambda list-functions --query "Functions[].FunctionName"

  check "$region" "SQS: filas (sem custo parado, não devem sobrar)" \
    sqs list-queues --query "QueueUrls"

  check "$region" "EventBridge: regras próprias (sem custo parado, não devem sobrar)" \
    events list-rules --query "Rules[?ManagedBy==null].Name"

  check "$region" "EventBridge Scheduler: agendamentos (sem custo parado, não devem sobrar)" \
    scheduler list-schedules --query "Schedules[].Name"

  check "$region" "CloudFormation: stacks não apagadas" \
    cloudformation list-stacks \
    --query "StackSummaries[?StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]"

  check "$region" "SNS: tópicos (sem custo parado, não devem sobrar)" \
    sns list-topics --query "Topics[].TopicArn"

  check "$region" "CloudWatch Logs: log groups (armazenamento)" \
    logs describe-log-groups \
    --query "logGroups[].[logGroupName,join('', [to_string(storedBytes), ' bytes'])]"

  if [ "$FOUND" -eq "$before" ]; then
    echo "  nada encontrado"
  fi
done

# --- serviços globais ---------------------------------------------------

echo
echo "== global"
before=$FOUND

check_global "S3: buckets (inclui o bucket de state do bootstrap)" \
  s3api list-buckets --query "Buckets[].Name"

check_global "Route 53: hosted zones (cobrança mensal por zona)" \
  route53 list-hosted-zones --query "HostedZones[].Name"

check_global "CloudFront: distribuições" \
  cloudfront list-distributions --query "DistributionList.Items[].[Id,DomainName]"

# Sem custo, mas não devem sobrar.
check_global "IAM: provider OIDC do GitHub (sem custo, não deve sobrar)" \
  iam list-open-id-connect-providers \
  --query "OpenIDConnectProviderList[?contains(Arn, 'token.actions.githubusercontent.com')].Arn"

check_global "IAM: roles que confiam no GitHub Actions (sem custo, não devem sobrar)" \
  iam list-roles \
  --query "Roles[?contains(to_string(AssumeRolePolicyDocument), 'token.actions.githubusercontent.com')].RoleName"

check_global "IAM: provider OIDC de cluster EKS (sem custo, não deve sobrar)" \
  iam list-open-id-connect-providers \
  --query "OpenIDConnectProviderList[?contains(Arn, 'oidc.eks.')].Arn"

check_global "IAM: roles IRSA que confiam em cluster EKS (sem custo, não devem sobrar)" \
  iam list-roles \
  --query "Roles[?contains(to_string(AssumeRolePolicyDocument), 'oidc.eks.')].RoleName"

check_global "IAM: roles dos projetos do portfólio (sem custo, não devem sobrar)" \
  iam list-roles \
  --query "Roles[?starts_with(RoleName, 'devops-')].RoleName"

if [ "$FOUND" -eq "$before" ]; then
  echo "  nada encontrado"
fi

# --- resumo -------------------------------------------------------------

echo
echo "------------------------------------------------------------"

if [ "$ERRORS" -gt 0 ]; then
  echo "ATENÇÃO: $ERRORS verificação(ões) falharam. Nesses pontos o resultado"
  echo "NÃO significa ausência de recurso, só que não foi possível verificar:"
  printf '%s' "$ERROR_LIST"
  echo
fi

if [ "$FOUND" -gt 0 ]; then
  echo "Encontrados $FOUND tipo(s) de recurso que ficaram para trás."
  echo "Confira se cada um é esperado antes de considerar a conta limpa."
  exit 1
fi

if [ "$ERRORS" -gt 0 ]; then
  exit 2
fi

echo "Nada encontrado nas regiões verificadas nem nos serviços globais."
exit 0
