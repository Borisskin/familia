#!/usr/bin/env bash

set -euo pipefail
IFS=$'\n\t'
umask 077

usage() {
  cat <<'EOF'
usage: rc-test-env.sh --repo-root PATH --state-root PATH --evidence-root PATH
                      --run-id ID [--dependency-root PATH] [--python-path PATH]
                      [--allow-env NAME]
                      [--no-install | --install-locked] -- COMMAND [ARG ...]

Run COMMAND in an external Python 3.12 venv on WSL2 Ubuntu and capture an
atomic evidence bundle.  All runtime roots are required to be outside the
checkout.  --dependency-root may name an already prepared, external Python
distribution directory; its installed distributions are recorded exactly.

This runner never installs system packages or changes repository state.  The
--no-install flag is explicit documentation that the isolated venv and optional
prepared dependency roots are used as supplied.

--install-locked bootstraps pip 26.1.2 from a pinned wheel digest into external
state and installs the repository's hashed requirements without dependency
resolution or source builds.
EOF
}

reject() {
  printf '%s\n' "rc-test-env: request rejected" >&2
  exit 64
}

require_value() {
  (( $# >= 2 )) || reject
}

assert_normalized_absolute() {
  local path=$1
  [[ $path == /* ]] || reject
  [[ $(realpath -m -- "$path") == "$path" ]] || reject
  local current=/
  local remainder=${path#/}
  local -a components=()
  IFS='/' read -r -a components <<< "$remainder"
  local component
  for component in "${components[@]}"; do
    [[ -n $component && $component != . && $component != .. ]] || reject
    current=${current%/}/$component
    if [[ -L $current ]]; then
      reject
    fi
    if [[ ! -e $current ]]; then
      break
    fi
  done
}

assert_external_to_repo() {
  local repo=$1
  local candidate=$2
  if [[ $candidate == "$repo" || $candidate == "$repo"/* ]]; then
    reject
  fi
}

assert_disjoint() {
  local left=$1
  local right=$2
  if [[ $left == "$right" || $left == "$right"/* || $right == "$left"/* ]]; then
    reject
  fi
}

repo_root=
state_root=
evidence_root=
run_id=
no_install=0
install_locked=0
declare -a dependency_roots=()
declare -a python_paths=()
declare -a allowed_environment=()
declare -a command_argv=()

while (( $# )); do
  case $1 in
    --help|-h)
      usage
      exit 0
      ;;
    --repo-root)
      require_value "$@"
      repo_root=$2
      shift 2
      ;;
    --state-root)
      require_value "$@"
      state_root=$2
      shift 2
      ;;
    --evidence-root)
      require_value "$@"
      evidence_root=$2
      shift 2
      ;;
    --run-id)
      require_value "$@"
      run_id=$2
      shift 2
      ;;
    --dependency-root)
      require_value "$@"
      dependency_roots+=("$2")
      shift 2
      ;;
    --python-path)
      require_value "$@"
      python_paths+=("$2")
      shift 2
      ;;
    --allow-env)
      require_value "$@"
      allowed_environment+=("$2")
      shift 2
      ;;
    --no-install)
      no_install=1
      shift
      ;;
    --install-locked)
      install_locked=1
      shift
      ;;
    --)
      shift
      command_argv=("$@")
      break
      ;;
    *)
      reject
      ;;
  esac
done

[[ -n $repo_root && -n $state_root && -n $evidence_root && -n $run_id ]] || reject
(( ${#command_argv[@]} > 0 )) || reject
(( no_install + install_locked <= 1 )) || reject
[[ $run_id =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || reject
[[ $run_id != . && $run_id != .. ]] || reject
case ${run_id,,} in
  manifest.json|command.json|environment.json|source.json|stdout.bin|stderr.bin|hashes.json|failure.json|dependency-install.bin|incomplete)
    reject
    ;;
esac

[[ $(uname -s) == Linux ]] || reject
kernel_release=$(uname -r)
[[ ${kernel_release,,} == *microsoft* && ${kernel_release,,} == *wsl2* ]] || reject
os_id=
while IFS='=' read -r key value; do
  value=${value#\"}
  value=${value%\"}
  case $key in
    ID) os_id=$value ;;
  esac
done < /etc/os-release
[[ $os_id == ubuntu ]] || reject

python312=$(command -v python3.12) || reject
"$python312" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' || reject

assert_normalized_absolute "$repo_root"
[[ -d $repo_root && ! -L $repo_root ]] || reject
repo_root=$(realpath -e -- "$repo_root")
[[ -d $repo_root/.git || -f $repo_root/.git ]] || reject

assert_normalized_absolute "$state_root"
assert_normalized_absolute "$evidence_root"
assert_external_to_repo "$repo_root" "$state_root"
assert_external_to_repo "$repo_root" "$evidence_root"
assert_disjoint "$state_root" "$evidence_root"
[[ -d $(dirname -- "$state_root") ]] || reject
[[ -d $(dirname -- "$evidence_root") ]] || reject

declare -a validated_dependency_roots=()
for dependency_root in "${dependency_roots[@]}"; do
  assert_normalized_absolute "$dependency_root"
  [[ -d $dependency_root && ! -L $dependency_root ]] || reject
  dependency_root=$(realpath -e -- "$dependency_root")
  assert_external_to_repo "$repo_root" "$dependency_root"
  assert_disjoint "$evidence_root" "$dependency_root"
  validated_dependency_roots+=("$dependency_root")
done
dependency_roots=("${validated_dependency_roots[@]}")
declare -a validated_python_paths=()
for python_path_entry in "${python_paths[@]}"; do
  assert_normalized_absolute "$python_path_entry"
  [[ -d $python_path_entry && ! -L $python_path_entry ]] || reject
  validated_python_paths+=("$(realpath -e -- "$python_path_entry")")
done
python_paths=("${validated_python_paths[@]}")

if [[ -e $state_root || -L $state_root ]]; then
  [[ -d $state_root && ! -L $state_root ]] || reject
else
  mkdir -m 700 -- "$state_root"
fi
if [[ -e $evidence_root || -L $evidence_root ]]; then
  [[ -d $evidence_root && ! -L $evidence_root ]] || reject
else
  mkdir -m 700 -- "$evidence_root"
fi

run_state=$state_root/$run_id
[[ ! -e $run_state && ! -L $run_state ]] || reject
mkdir -m 700 -- "$run_state"
home_root=$run_state/home
tmp_root=$run_state/tmp
cache_root=$run_state/cache
pip_cache_root=$run_state/pip-cache
venv_root=$run_state/venv
mkdir -m 700 -- "$home_root" "$tmp_root" "$cache_root" "$pip_cache_root"

"$python312" -m venv --without-pip "$venv_root"
venv_python=$venv_root/bin/python
[[ -x $venv_python ]] || reject
"$venv_python" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' || reject

python_path=
for dependency_root in "${dependency_roots[@]}"; do
  if [[ -n $python_path ]]; then
    python_path=$python_path:$dependency_root
  else
    python_path=$dependency_root
  fi
done
for python_path_entry in "${python_paths[@]}"; do
  if [[ -n $python_path ]]; then
    python_path=$python_path:$python_path_entry
  else
    python_path=$python_path_entry
  fi
done

export HOME=$home_root
export TMPDIR=$tmp_root
export XDG_CACHE_HOME=$cache_root
export PIP_CACHE_DIR=$pip_cache_root
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX=$cache_root/pycache
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1
export PIP_REQUIRE_VIRTUALENV=1
export VIRTUAL_ENV=$venv_root
export PATH=$venv_root/bin:/usr/bin:/bin
if [[ -n $python_path ]]; then
  export PYTHONPATH=$python_path
else
  unset PYTHONPATH
fi
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TZ=UTC
export GIT_OPTIONAL_LOCKS=0

install_log=
pip_wheel=
declare -a installer_argv=()
if (( install_locked )); then
  lock_file=$repo_root/familia/requirements.lock
  [[ -f $lock_file && ! -L $lock_file ]] || reject
  pip_version=26.1.2
  pip_wheel_url=https://files.pythonhosted.org/packages/5d/95/6b5cb3461ea5673ba0995989746db58eb18b91b54dbf331e72f569540946/pip-26.1.2-py3-none-any.whl
  pip_wheel_sha256=382ff9f685ee3bc25864f820aa50505825f10f5458ffff07e30a6d96e5715cab
  pip_wheel=$run_state/pip-26.1.2-py3-none-any.whl
  install_log=$run_state/dependency-install.bin
  "$venv_python" - "$pip_wheel_url" "$pip_wheel_sha256" "$pip_wheel" "$install_log" <<'PY'
import hashlib
import os
import pathlib
import sys
import urllib.request

url, expected, destination_raw, log_raw = sys.argv[1:]
destination = pathlib.Path(destination_raw)
temporary = destination.with_suffix(destination.suffix + ".incomplete")
digest = hashlib.sha256()
with urllib.request.urlopen(url, timeout=60) as response, temporary.open("xb") as output:
    while chunk := response.read(1024 * 1024):
        output.write(chunk)
        digest.update(chunk)
    output.flush()
    os.fsync(output.fileno())
if digest.hexdigest() != expected:
    temporary.unlink(missing_ok=True)
    raise SystemExit("bootstrap digest mismatch")
temporary.rename(destination)
with pathlib.Path(log_raw).open("xb") as log:
    log.write(("bootstrap pip wheel sha256=" + expected + "\n").encode("ascii"))
    log.flush()
    os.fsync(log.fileno())
PY
  installer_argv=(
    "$venv_python" -m pip install
    --require-hashes
    --no-deps
    --only-binary=:all:
    --index-url https://pypi.org/simple
    --cache-dir "$pip_cache_root"
    --requirement "$lock_file"
  )
  PYTHONPATH=$pip_wheel "${installer_argv[@]}" >> "$install_log" 2>&1
fi

collector=$repo_root/scripts/capture_test_evidence.py
[[ -f $collector && ! -L $collector ]] || reject
declare -a collector_argv=(
  "$venv_python"
  "$collector"
  --repo-root "$repo_root"
  --evidence-root "$evidence_root"
  --run-id "$run_id"
  --venv-root "$venv_root"
  --home-root "$home_root"
  --tmp-root "$tmp_root"
  --cache-root "$cache_root"
  --pip-cache-root "$pip_cache_root"
  --input bin/rc-test-env.sh
  --input scripts/capture_test_evidence.py
  --input familia/tests/test_rc_test_harness.py
)

for input in familia/requirements.lock familia/pyproject.toml nanobot/pyproject.toml memx/pyproject.toml; do
  if [[ -f $repo_root/$input ]]; then
    collector_argv+=(--input "$input")
  fi
done
if [[ -f $repo_root/familia/requirements.lock ]]; then
  collector_argv+=(--dependency-input familia/requirements.lock)
fi
if (( install_locked )); then
  collector_argv+=(
    --dependency-mode hash_locked
    --external-dependency-input "$pip_wheel"
    --dependency-install-log "$install_log"
    --installer-version 26.1.2
    --package-index https://pypi.org/simple
  )
  for installer_argument in "${installer_argv[@]}"; do
    collector_argv+=("--installer-arg=$installer_argument")
  done
fi
for variable in "${allowed_environment[@]}"; do
  collector_argv+=(--allow-env "$variable")
done
collector_argv+=(-- "${command_argv[@]}")

: "$no_install"
exec "${collector_argv[@]}"
