#!/usr/bin/env bash
#
# encerrar.sh
#
# Apaga tudo o que o projeto criou, na ordem que o CloudFormation exige:
# buckets precisam estar vazios e a stack da aplicação usa a role de
# execução que mora na stack de bootstrap.
#
# Uso: ./scripts/encerrar.sh
# Compatível com o bash 3.2 do macOS.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
APP_STACK="devops-04-app"
BOOTSTRAP_STACK="devops-04-bootstrap"

stack_exists() {
  aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" >/dev/null 2>&1
}

stack_output() {
  aws cloudformation describe-stacks \
    --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
    --output text \
    --region "$REGION"
}

delete_stack() {
  echo "Apagando a stack $1..."
  aws cloudformation delete-stack --stack-name "$1" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$1" --region "$REGION"
  echo "  stack $1 apagada"
}

# Apaga todas as versões e marcadores de exclusão, 500 por vez.
empty_versioned_bucket() {
  local bucket="$1" batch
  echo "Esvaziando o bucket $bucket (todas as versões)..."
  while :; do
    # shellcheck disable=SC2016  # crases são literais do JMESPath, não do shell
    batch=$(aws s3api list-object-versions \
      --bucket "$bucket" \
      --max-keys 500 \
      --no-paginate \
      --query '{Objects: [Versions, DeleteMarkers][][].{Key: Key, VersionId: VersionId}, Quiet: `true`}' \
      --output json \
      --region "$REGION")
    if ! printf '%s' "$batch" | grep -q '"Key"'; then
      break
    fi
    aws s3api delete-objects --bucket "$bucket" --delete "$batch" --region "$REGION" >/dev/null
  done
}

if stack_exists "$APP_STACK"; then
  report_bucket=$(stack_output "$APP_STACK" ReportBucketName)
  echo "Esvaziando o bucket $report_bucket..."
  aws s3 rm "s3://$report_bucket" --recursive --region "$REGION" >/dev/null
  delete_stack "$APP_STACK"
else
  echo "Stack $APP_STACK não existe; seguindo."
fi

if stack_exists "$BOOTSTRAP_STACK"; then
  artifact_bucket=$(stack_output "$BOOTSTRAP_STACK" ArtifactBucketName)
  empty_versioned_bucket "$artifact_bucket"
  delete_stack "$BOOTSTRAP_STACK"
else
  echo "Stack $BOOTSTRAP_STACK não existe; seguindo."
fi

echo
echo "Encerramento concluído. Confira com: ./scripts/verificar-cobranca.sh $REGION"
