// 云服务器上的 Jenkins：检出代码、跑单测，通过后再构建并启动网关和前端。
// 单测对应 make test-backend / test-frontend / test-evals。
// 不重启 Postgres / Redis 等依赖；这些需要事先在服务器上通过 make up 准备好。
// .env 与 config.yaml 不在仓库里：服务器上的真值放在 MYFLOW_CONFIG_DIR，
// 每次 Checkout 后由 Config 阶段复制进工作区（不依赖上一次构建的残留文件）。
// Agent 需要 bash、make、Python 3.12、uv、Node 22、Corepack（pnpm 11.22.0）、Docker。

pipeline {
    agent any

    options {
        timestamps()
        timeout(time: 90, unit: 'MINUTES')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    environment {
        UV_LINK_MODE = 'copy'
        UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
        NEXT_TELEMETRY_DISABLED = '1'
        PNPM_VERSION = '11.22.0'
        MYFLOW_CONFIG_DIR = '/var/jenkins_home/myflow-config'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Config') {
            steps {
                sh '''
                    set -euo pipefail
                    for file in .env config.yaml; do
                        src="${MYFLOW_CONFIG_DIR}/${file}"
                        if [ ! -f "$src" ]; then
                            printf 'Missing %s. Put the server-side .env and config.yaml in %s before building.\n' "$src" "$MYFLOW_CONFIG_DIR" >&2
                            exit 1
                        fi
                        cp "$src" "$file"
                        chmod 600 "$file"
                    done
                '''
            }
        }

        stage('Install') {
            steps {
                sh '''
                    set -euo pipefail
                    command -v uv >/dev/null
                    command -v node >/dev/null
                    command -v pnpm >/dev/null
                    uv sync --frozen --all-packages
                    (cd frontend && pnpm install --frozen-lockfile)
                    mkdir -p reports
                '''
            }
        }

        // stage('Test') {
        //     parallel {
        //         stage('Backend') {
        //             steps {
        //                 sh '''
        //                     set -euo pipefail
        //                     cd backend
        //                     uv run pytest --junitxml=../reports/backend-junit.xml
        //                 '''
        //             }
        //         }
        //         stage('Frontend') {
        //             steps {
        //                 sh '''
        //                     set -euo pipefail
        //                     cd frontend
        //                     pnpm exec vitest run --reporter=default --reporter=junit --outputFile=../reports/frontend-junit.xml
        //                 '''
        //             }
        //         }
        //         stage('Evals') {
        //             steps {
        //                 sh '''
        //                     set -euo pipefail
        //                     uv run --project evals pytest evals/tests -v --junitxml=reports/evals-junit.xml
        //                 '''
        //             }
        //         }
        //     }
        // }

        stage('Up backend') {
            steps {
                sh 'make up-backend'
            }
        }

        stage('Up frontend') {
            steps {
                sh 'make up-frontend'
            }
        }

        stage('Health') {
            steps {
                sh '''
                    set -euo pipefail
                    for _ in 1 2 3 4 5 6; do
                        if make health; then
                            exit 0
                        fi
                        sleep 5
                    done
                    exit 1
                '''
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'reports/*-junit.xml'
        }
    }
}
