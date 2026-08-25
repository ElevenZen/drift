#!/usr/bin/env bash
# =============================================================================
# Drift CLI - Artifact Build & Verification Pipeline
# =============================================================================
# Generates release artifacts:
#   1. Python Wheel (.whl) -> dist/drift-<version>-py3-none-any.whl
#   2. Standalone Zipapp   -> dist/drift (single-file executable)
#
# Optionally runs end-to-end verification tests against the built artifacts.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
DIST_DIR="${REPO_ROOT}/dist"

RUN_TESTS=true
BUILD_WHEEL=true
BUILD_ZIPAPP=true
CLEAN=false

print_usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Builds Drift release artifacts (Wheel and Standalone Zipapp) and verifies them.

Options:
  --no-test           Skip verification tests on built artifacts
  --clean             Clean previous build artifacts before building
  --wheel-only        Build and verify Python wheel only
  --zipapp-only       Build and verify standalone Zipapp executable only
  -h, --help          Display this help message and exit

EOF
}

# Parse options
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-test)
            RUN_TESTS=false
            shift
            ;;
        --clean)
            CLEAN=true
            shift
            ;;
        --wheel-only)
            BUILD_WHEEL=true
            BUILD_ZIPAPP=false
            shift
            ;;
        --zipapp-only)
            BUILD_WHEEL=false
            BUILD_ZIPAPP=true
            shift
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            echo "❌ [ERROR] Unknown option: $1" >&2
            print_usage >&2
            exit 1
            ;;
    esac
done

cd "${REPO_ROOT}"

# 1. Clean previous builds if requested
if [[ "${CLEAN}" = true ]]; then
    echo "🧹 Cleaning previous build artifacts..."
    rm -rf "${DIST_DIR}" "${REPO_ROOT}/build" "${REPO_ROOT}/drift.egg-info"
fi

mkdir -p "${DIST_DIR}"

echo "======================================================================"
echo "🌀 Building Drift Release Artifacts"
echo "======================================================================"

# 2. Build Python Wheel
WHEEL_FILE=""
if [[ "${BUILD_WHEEL}" = true ]]; then
    echo
    echo "📦 [1/2] Building Python Wheel (.whl)..."
    if command -v python3 >/dev/null 2>&1; then
        if python3 -m build --version >/dev/null 2>&1; then
            python3 -m build --wheel --outdir "${DIST_DIR}"
        else
            python3 -m pip wheel --no-deps -w "${DIST_DIR}" .
        fi
    else
        echo "❌ [ERROR] python3 is required to build wheel." >&2
        exit 1
    fi
    
    WHEEL_FILE="$(find "${DIST_DIR}" -name "drift-*.whl" | head -n 1)"
    if [[ -n "${WHEEL_FILE}" ]]; then
        echo "✅ Created Wheel: ${WHEEL_FILE}"
    else
        echo "❌ [ERROR] Wheel build failed." >&2
        exit 1
    fi
fi

# 3. Build Standalone Zipapp
ZIPAPP_FILE="${DIST_DIR}/drift"
if [[ "${BUILD_ZIPAPP}" = true ]]; then
    echo
    echo "⚡ [2/2] Building Standalone Zipapp (single-file executable)..."
    python3 -m zipapp "${REPO_ROOT}/src" -m "drift.cli:main" -o "${ZIPAPP_FILE}" -p "/usr/bin/env python3"
    chmod +x "${ZIPAPP_FILE}"
    echo "✅ Created Zipapp: ${ZIPAPP_FILE}"
fi

# 4. Artifact Verification Tests
if [[ "${RUN_TESTS}" = true ]]; then
    echo
    echo "======================================================================"
    echo "🧪 Running Verification Tests on Built Artifacts"
    echo "======================================================================"

    # --- Test Zipapp ---
    if [[ "${BUILD_ZIPAPP}" = true && -f "${ZIPAPP_FILE}" ]]; then
        echo
        echo "🔬 Testing Standalone Zipapp (${ZIPAPP_FILE})..."
        TEST_BASE="$(mktemp -d -t drift_zipapp_test_XXXXXX)"
        TEST_WORKSPACE="${TEST_BASE}/workspace"
        TEST_TARGET="${TEST_BASE}/target"
        mkdir -p "${TEST_WORKSPACE}" "${TEST_TARGET}"
        trap 'rm -rf "${TEST_BASE}"' EXIT

        # Clear PYTHONPATH to guarantee isolation and set test git author info
        (
            unset PYTHONPATH
            export GIT_AUTHOR_NAME="Drift Test"
            export GIT_AUTHOR_EMAIL="test@drift.local"
            export GIT_COMMITTER_NAME="Drift Test"
            export GIT_COMMITTER_EMAIL="test@drift.local"
            cd "${TEST_WORKSPACE}"

            # 1. Test CLI help
            "${ZIPAPP_FILE}" --help > /dev/null

            # 2. Test help docs page loading from zipapp
            "${ZIPAPP_FILE}" help drift.toml > /dev/null

            # 3. Initialize workspace
            "${ZIPAPP_FILE}" init > /dev/null

            # 4. Create a package
            "${ZIPAPP_FILE}" new test_pkg --target "${TEST_TARGET}" > /dev/null

            # 5. Add a dummy configuration file
            echo "test_key = 123" > "${TEST_WORKSPACE}/src/test_pkg/config.conf"

            # 6. Deploy package
            "${ZIPAPP_FILE}" deploy test_pkg > /dev/null

            # Verify deployed file exists
            if [[ ! -f "${TEST_TARGET}/config.conf" ]]; then
                echo "❌ [FAIL] Zipapp deployment test failed: target file missing." >&2
                exit 1
            fi
        )
        rm -rf "${TEST_BASE}"
        echo "✅ Standalone Zipapp passed verification tests!"
    fi

    # --- Test Wheel in isolated Virtual Environment ---
    if [[ "${BUILD_WHEEL}" = true && -n "${WHEEL_FILE}" ]]; then
        echo
        echo "🔬 Testing Wheel Installation in Isolated Virtualenv (${WHEEL_FILE})..."
        VENV_DIR="$(mktemp -d -t drift_wheel_venv_XXXXXX)"
        TEST_BASE="$(mktemp -d -t drift_wheel_test_XXXXXX)"
        TEST_WORKSPACE="${TEST_BASE}/workspace"
        TEST_TARGET="${TEST_BASE}/target"
        mkdir -p "${TEST_WORKSPACE}" "${TEST_TARGET}"
        trap 'rm -rf "${VENV_DIR}" "${TEST_BASE}"' EXIT

        # Create virtualenv
        python3 -m venv "${VENV_DIR}"

        # Install wheel
        "${VENV_DIR}/bin/pip" install --quiet "${WHEEL_FILE}"

        # Verify CLI execution
        (
            unset PYTHONPATH
            export GIT_AUTHOR_NAME="Drift Test"
            export GIT_AUTHOR_EMAIL="test@drift.local"
            export GIT_COMMITTER_NAME="Drift Test"
            export GIT_COMMITTER_EMAIL="test@drift.local"
            cd "${TEST_WORKSPACE}"

            "${VENV_DIR}/bin/drift" --help > /dev/null
            "${VENV_DIR}/bin/drift" help package > /dev/null
            "${VENV_DIR}/bin/drift" init > /dev/null
            "${VENV_DIR}/bin/drift" new venv_pkg --target "${TEST_TARGET}" > /dev/null
            echo "hello = 'world'" > "${TEST_WORKSPACE}/src/venv_pkg/app.toml"
            "${VENV_DIR}/bin/drift" deploy venv_pkg > /dev/null

            if [[ ! -f "${TEST_TARGET}/app.toml" ]]; then
                echo "❌ [FAIL] Wheel virtualenv deployment test failed: target file missing." >&2
                exit 1
            fi
        )
        rm -rf "${VENV_DIR}" "${TEST_BASE}"
        echo "✅ Python Wheel passed virtualenv installation and verification tests!"
    fi
fi

# 5. Summary Table
echo
echo "======================================================================"
echo "🎉 Build Summary & Artifact Manifest"
echo "======================================================================"
printf "%-35s %-12s %s\n" "Artifact File" "Size" "SHA256 Checksum"
printf "%-35s %-12s %s\n" "-----------------------------------" "------------" "----------------------------------------------------------------"

for f in "${DIST_DIR}"/*; do
    if [[ -f "$f" ]]; then
        fname="$(basename "$f")"
        fsize="$(du -h "$f" | cut -f1)"
        fsha="$(sha256sum "$f" | cut -d' ' -f1)"
        printf "%-35s %-12s %s\n" "${fname}" "${fsize}" "${fsha}"
    fi
done
echo
echo "🚀 All artifacts generated successfully in: ${DIST_DIR}"
