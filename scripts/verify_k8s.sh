#!/usr/bin/env bash
#
# Stand the whole system up on a local `kind` cluster and verify it there.
#
# The point is not that the YAML parses. It is that the deployed system
# actually holds its guarantees: no overdraft under concurrent load, an
# idempotency key applied exactly once, and a ledger that reconciles to zero
# afterwards, all against pods behind a Service rather than a single local
# process.
#
# Steps: create the cluster, build and side-load the image, install
# metrics-server (kind has none, and the HPA needs it), apply the manifests,
# initiate the replica set, wait for rollout, then run the same end-to-end
# assertions used for docker-compose through a port-forward.
#
# Usage:
#   ./scripts/verify_k8s.sh          # create, deploy, verify, leave running
#   ./scripts/verify_k8s.sh --clean  # ...then delete the cluster

set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-ledgerlock}"
NAMESPACE="ledgerlock"
IMAGE="ledgerlock-api:local"
MONGO_IMAGE="mongo:7.0"
LOCAL_PORT="${LOCAL_PORT:-18080}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CLEAN_UP_CLUSTER=false
[[ "${1:-}" == "--clean" ]] && CLEAN_UP_CLUSTER=true

info() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '    \033[31mFAIL\033[0m %s\n' "$1"; }

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: '$1' is required but not installed." >&2
    exit 2
  }
}
require kind
require kubectl
require docker
require curl

cd "$PROJECT_ROOT"

PORT_FORWARD_PID=""
cleanup() {
  if [[ -n "$PORT_FORWARD_PID" ]]; then
    kill "$PORT_FORWARD_PID" 2>/dev/null || true
  fi
  if [[ "$CLEAN_UP_CLUSTER" == true ]]; then
    info "Deleting the kind cluster"
    kind delete cluster --name "$CLUSTER_NAME" || true
  else
    echo
    echo "Cluster '$CLUSTER_NAME' left running. Remove it with:"
    echo "  kind delete cluster --name $CLUSTER_NAME"
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------
info "Creating the kind cluster (if it does not already exist)"
# ---------------------------------------------------------------------
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "    cluster '$CLUSTER_NAME' already exists, reusing it"
else
  kind create cluster --name "$CLUSTER_NAME" --wait 120s
fi
kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null
kubectl cluster-info | head -2

# ---------------------------------------------------------------------
info "Building the image and side-loading it into the cluster"
# ---------------------------------------------------------------------
# kind nodes have their own container runtime and cannot see the host's
# image cache, so the image has to be copied in explicitly. This is also why
# the Deployment sets imagePullPolicy: Never.
docker build -t "$IMAGE" . >/dev/null
kind load docker-image "$IMAGE" --name "$CLUSTER_NAME"
echo "    loaded $IMAGE"

# Side-load MongoDB too, if possible. Without it the kubelet inside the kind
# node pulls mongo:7.0 from Docker Hub even though the host already has it,
# which is slow enough to look like a hang.
#
# Best-effort on purpose. `kind load docker-image` cannot import a
# multi-platform manifest list from Docker Desktop's containerd image store
# (it fails with "content digest ... not found" because the other platforms'
# layers are not present locally). So: try the direct load, fall back to a
# single-platform archive, and if both fail let the kubelet pull it. The wait
# on the pod below is generous enough to cover a cold pull.
if kind load docker-image "$MONGO_IMAGE" --name "$CLUSTER_NAME" >/dev/null 2>&1; then
  echo "    loaded $MONGO_IMAGE"
elif docker save "$MONGO_IMAGE" -o /tmp/ledgerlock-mongo-image.tar >/dev/null 2>&1 \
     && kind load image-archive /tmp/ledgerlock-mongo-image.tar \
          --name "$CLUSTER_NAME" >/dev/null 2>&1; then
  rm -f /tmp/ledgerlock-mongo-image.tar
  echo "    loaded $MONGO_IMAGE (via image archive)"
else
  rm -f /tmp/ledgerlock-mongo-image.tar
  echo "    could not side-load $MONGO_IMAGE; the kubelet will pull it"
  echo "    (multi-platform manifest lists cannot be imported into kind)"
fi

# ---------------------------------------------------------------------
info "Installing metrics-server (kind ships without one)"
# ---------------------------------------------------------------------
# Without metrics-server the HPA cannot read CPU and reports <unknown>
# targets, so the autoscaling part of this phase would be unverifiable. The
# --kubelet-insecure-tls patch is needed because kind's kubelet serving
# certificates are not signed by the cluster CA.
kubectl apply -f \
  https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml \
  >/dev/null
kubectl -n kube-system patch deployment metrics-server --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}
]' >/dev/null 2>&1 || true
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s

# ---------------------------------------------------------------------
info "Applying namespace, config, and the JWT secret"
# ---------------------------------------------------------------------
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml

# Created imperatively from a freshly generated value. The secret never
# exists in a file in the repository; see k8s/secret.example.yaml for why.
GENERATED_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
kubectl -n "$NAMESPACE" create secret generic ledgerlock-secrets \
  --from-literal=JWT_SECRET_KEY="$GENERATED_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
unset GENERATED_SECRET
echo "    secret ledgerlock-secrets created from a freshly generated key"

# ---------------------------------------------------------------------
info "Deploying MongoDB and initiating the replica set"
# ---------------------------------------------------------------------
kubectl apply -f k8s/mongo-statefulset.yaml
echo "    waiting for the mongo pod to start..."
kubectl -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Running \
  pod/ledgerlock-mongo-0 --timeout=420s

# Recreated each run so a re-verify re-initiates idempotently rather than
# colliding with a completed Job.
kubectl -n "$NAMESPACE" delete job ledgerlock-mongo-init --ignore-not-found >/dev/null
kubectl apply -f k8s/mongo-init-job.yaml
echo "    waiting for the replica set init job..."
kubectl -n "$NAMESPACE" wait --for=condition=complete \
  job/ledgerlock-mongo-init --timeout=240s
kubectl -n "$NAMESPACE" logs job/ledgerlock-mongo-init | sed 's/^/    /'

echo "    waiting for the mongo pod to report Ready (writable primary)..."
kubectl -n "$NAMESPACE" wait --for=condition=ready pod/ledgerlock-mongo-0 --timeout=240s

# ---------------------------------------------------------------------
info "Deploying the API, Service, and HPA"
# ---------------------------------------------------------------------
kubectl apply -f k8s/api-deployment.yaml
kubectl apply -f k8s/api-service.yaml
kubectl apply -f k8s/api-hpa.yaml

echo "    waiting for the rollout..."
kubectl -n "$NAMESPACE" rollout status deployment/ledgerlock-api --timeout=300s

echo
kubectl -n "$NAMESPACE" get pods -o wide
echo
kubectl -n "$NAMESPACE" get svc
echo
kubectl -n "$NAMESPACE" get hpa

# ---------------------------------------------------------------------
info "Checking readiness gating"
# ---------------------------------------------------------------------
# Only pods that pass /health/ready, which requires a transaction-capable
# primary, may appear in the Service's endpoints.
ready_replicas=$(kubectl -n "$NAMESPACE" get deployment ledgerlock-api \
  -o jsonpath='{.status.readyReplicas}')
endpoint_count=$(kubectl -n "$NAMESPACE" get endpoints ledgerlock-api \
  -o jsonpath='{.subsets[*].addresses[*].ip}' | wc -w | tr -d ' ')

echo "    ready replicas: $ready_replicas"
echo "    Service endpoints: $endpoint_count"

failures=0
if [[ "$ready_replicas" == "2" ]]; then
  ok "both replicas are Ready"
else
  bad "expected 2 ready replicas, got '$ready_replicas'"
  failures=$((failures + 1))
fi
if [[ "$endpoint_count" == "2" ]]; then
  ok "both replicas are in the Service's endpoints"
else
  bad "expected 2 Service endpoints, got '$endpoint_count'"
  failures=$((failures + 1))
fi

# ---------------------------------------------------------------------
info "Port-forwarding the Service and running the end-to-end verification"
# ---------------------------------------------------------------------
# Forwarding the Service, not a pod, so requests are spread across both
# replicas. That is the interesting part: the concurrency assertions inside
# the verification script then run against two separate processes sharing one
# MongoDB, which no earlier phase has exercised.
kubectl -n "$NAMESPACE" port-forward service/ledgerlock-api \
  "${LOCAL_PORT}:80" >/tmp/ledgerlock-port-forward.log 2>&1 &
PORT_FORWARD_PID=$!

printf '    waiting for the port-forward'
for _ in $(seq 1 40); do
  if curl -fsS "http://localhost:${LOCAL_PORT}/health" >/dev/null 2>&1; then
    printf ' ok\n'
    break
  fi
  printf '.'
  sleep 1
done
printf '\n'

BASE_URL="http://localhost:${LOCAL_PORT}" ./scripts/verify_compose.sh || failures=$((failures + 1))

# ---------------------------------------------------------------------
info "HPA metrics"
# ---------------------------------------------------------------------
# An HPA that reports <unknown> targets is not an autoscaler, it is a
# manifest. So this waits for metrics-server to actually deliver CPU numbers
# and asserts the HPA computed a replica count from them.
#
# The wait is necessary rather than defensive: metrics-server scrapes on an
# interval and needs roughly 60-90 seconds after a pod starts before the HPA
# can read a utilisation figure. Measured on this cluster: <unknown>
# immediately after the rollout, `cpu: 3%/70%` about 90 seconds later.
printf '    waiting for metrics-server to report CPU for the API pods'
hpa_metrics_ready=false
for _ in $(seq 1 30); do
  scaling_active=$(kubectl -n "$NAMESPACE" get hpa ledgerlock-api \
    -o jsonpath='{.status.conditions[?(@.type=="ScalingActive")].status}' 2>/dev/null || true)
  if [[ "$scaling_active" == "True" ]]; then
    printf ' ok\n'
    hpa_metrics_ready=true
    break
  fi
  printf '.'
  sleep 10
done
[[ "$hpa_metrics_ready" == true ]] || printf ' timed out\n'

echo
kubectl -n "$NAMESPACE" top pods 2>/dev/null | sed 's/^/    /' || \
  echo "    metrics not available"
echo
kubectl -n "$NAMESPACE" get hpa ledgerlock-api | sed 's/^/    /'
echo
kubectl -n "$NAMESPACE" describe hpa ledgerlock-api \
  | sed -n '/Conditions:/,/Events:/p' | sed 's/^/    /'
echo

if [[ "$hpa_metrics_ready" == true ]]; then
  ok "the HPA computed a replica count from real CPU metrics (ScalingActive=True)"
else
  bad "the HPA never reported ScalingActive=True; it cannot autoscale"
  failures=$((failures + 1))
fi

# The HPA owns the replica count, so it must not have scaled below its floor.
hpa_replicas=$(kubectl -n "$NAMESPACE" get hpa ledgerlock-api \
  -o jsonpath='{.status.currentReplicas}')
if [[ "$hpa_replicas" -ge 2 ]]; then
  ok "HPA is holding at least minReplicas (currentReplicas=$hpa_replicas)"
else
  bad "HPA reports currentReplicas=$hpa_replicas, below minReplicas=2"
  failures=$((failures + 1))
fi

# ---------------------------------------------------------------------
info "Result"
# ---------------------------------------------------------------------
if (( failures > 0 )); then
  echo "    KUBERNETES VERIFICATION FAILED ($failures check group(s) failed)"
  exit 1
fi
echo "    KUBERNETES VERIFICATION PASSED"
