# RepoTrial MVP Implementation Plan v1.1

> **For agentic workers:** RepoTrial uses a controller-worker-reviewer workflow. The controller MUST begin with `superpowers:using-superpowers`, execute planned work through `superpowers:using-git-worktrees` + `superpowers:subagent-driven-development`, require TDD for behavior changes, independent code review after every task, and fresh verification before completion claims. Parallel agents are used only for independent domains. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在两周内实现一个可复现的 RepoTrial MVP：输入公开 GitHub Docker Compose Web 项目，在隔离 Runner 中固定 commit、跑通核心用户旅程、采集可审计证据，并通过权限消融 + 功能回归生成经过 tested journeys 验证的 hardened Compose overlay。

**Architecture:** 采用“确定性工具 + 有状态 Agent”分层。静态解析、Compose 变换、HTTP/Playwright 断言、实验 Keep/Rollback 都由确定性代码完成；LLM 只负责启动计划、失败诊断、有限的 Journey 规划和下一步实验建议。所有目标仓库内容均视为不可信数据，执行发生在 `SandboxProvider` 提供的一次性隔离环境内。

**Tech Stack:** Python 3.12、uv、Pydantic v2、LangGraph、Typer、FastAPI、httpx、Playwright、ruamel.yaml、Git CLI、Docker/SBX CLI、pytest、pytest-asyncio、Jinja2、PostgreSQL（后期接入 LangGraph PostgresSaver）。

**Spec:** `docs/project/RepoTrial_AI_Agent项目立项文档_v0.1.docx`。若仓库中尚未存放该文件，开发时以当前立项文档为唯一产品约束，不得自行扩大 MVP 范围。

## Global Constraints

### Development governance (mandatory)

- Root `AGENTS.md` and `docs/dev/AGENTIC_DEVELOPMENT_PROTOCOL.md` are pre-seeded repository governance artifacts and MUST be read before M0.1; M0.1 must not rewrite them wholesale.
- Main Codex agent acts as controller/final gate. Planned implementation is delegated to a fresh implementer subagent; an independent reviewer subagent reviews the task diff; the controller independently verifies before advancing.
- Required Superpowers process: `using-superpowers` -> `using-git-worktrees` -> `subagent-driven-development`; feature/bugfix/refactor uses `test-driven-development`; bugs use `systematic-debugging`; every task uses `requesting-code-review`; completion uses `verification-before-completion`; final integration uses `finishing-a-development-branch`.
- Use `dispatching-parallel-agents` only when domains are independent and share no files/interfaces/mutable state. One writer per file/interface/worktree at a time.
- Do not vendor Superpowers into RepoTrial and do not add it to application dependencies. If required skills are unavailable, stop before product-code changes and report the missing development dependency.
- No production change is accepted solely because a subagent says it is complete. Controller must inspect diff, tests, reviewer findings and fresh verification evidence.
- YAGNI/DRY/typed boundaries are mandatory: no speculative abstractions, dead code, compatibility shims without users, generic `utils.py` dumping grounds, silent fallback success, or broad lint/type/test suppression.
- Every scoped task must avoid unrelated refactors and future-task implementation. Critical/Important reviewer findings block advancement.
- Repository gates are `ruff check`, `ruff format --check`, `mypy`, and `pytest`; tasks that alter covered production logic also run the configured coverage gate.
- Commit steps must explicitly `git add` only the files listed by the current task before `git commit -m`; `git commit -am` is forbidden because it silently omits newly created files.

- MVP 只支持：公开 GitHub 仓库、Linux、Docker Compose、单机 Web 应用；Kubernetes/裸机/Windows/任意 `curl | bash` 安装均明确不支持。
- 不在真实生产宿主机直接执行陌生 Compose；真实目标只允许进入 `SandboxProvider` 隔离 Runner。
- 开发早期先用 `FakeSandboxProvider` 与合成 fixtures 验证状态机；Docker Sandboxes Provider 在核心接口稳定后接入。
- 不把 README、网页 UI、日志中的文本当作可信指令；它们只能作为 `data` 进入解析/规划，不能提升工具权限、读取宿主机 Secret 或改变 sandbox policy。
- Agent 不允许接触生产凭证；测试账号、API key、文件均为合成数据。
- 所有真实仓库必须固定 `commit_sha`；同一实验必须记录 repo URL、commit SHA、Compose hash、overlay hash 和 Journey version。
- 原始 Compose 永不原地修改。所有加固变更写入独立 overlay，并保存父配置 hash。
- LLM 不直接决定“功能是否正常”。功能判断必须由 HTTP 状态、JSON 字段、DOM 可见文本、文件/进程事实等确定性 verifier 完成。
- 每个 Hardening mutation 必须经过 `apply -> boot -> replay journeys -> verify -> keep/rollback`；不能根据静态推断直接接受。
- “least privilege”只允许表述为 `tested-journey / workload-conditioned hardened candidate`；产品不得宣称软件“安全”、全局最小权限或完整恶意行为检测。
- 风险分数只用于实验排序，不作为安全评分营销。
- 任何外部观测能力缺失时必须显式输出 `unsupported` / `not_observed`，禁止伪造空结果为“没有风险”。
- 每个任务遵循 TDD：先失败测试，再最小实现，再回归；每个任务单独 commit。
- Codex 每次只执行一个任务 ID；完成前不得提前实现后续里程碑。

---

# 0. 如何用 Codex + Superpowers 执行本计划

## 0.0 第一次开工前：只做治理预检，不写业务代码

在仓库中预先放入：

- `AGENTS.md`：短小的仓库级强约束与文档索引；
- `docs/dev/AGENTIC_DEVELOPMENT_PROTOCOL.md`：多 Agent 分工、TDD、Review、Debug、Verification、CI/代码质量详细规范；
- `docs/project/RepoTrial_AI_Agent项目立项文档_v0.1.docx`：产品事实与边界；
- `docs/superpowers/plans/2026-08-25-repotrial-mvp.md`：本实施计划。

第一次 Codex 会话先使用 `CODEX_BOOTSTRAP.md` 中的启动 Prompt。主 Agent 必须先调用/使用 `superpowers:using-superpowers`，确认所需 Superpowers skills 可用，然后检查文档一致性、git/worktree 和 baseline；**该会话禁止实现 M0.1。**

Superpowers 属于 Codex 的开发工作流能力，不属于 RepoTrial 运行时。不要让 Codex `git clone`/复制插件源码到本仓库，也不要加入 `pyproject.toml`。

## 0.1 主 Agent / 子 Agent 固定职责

**主 Agent = Controller / Final Gate**：读取 spec/plan、确定任务边界、创建/确认隔离 worktree、记录 BASE_SHA、分派任务、处理冲突裁决、审查 diff、验证测试证据、决定是否进入下一任务。可委派的业务实现不由主 Agent 大段直接编写。

**Implementer 子 Agent**：每个任务使用 fresh implementer；严格 RED -> GREEN -> REFACTOR，只改当前任务，执行 scoped tests、commit、自审并返回 SHA/证据。

**Reviewer 子 Agent**：必须与 implementer 分离；基于任务要求 + BASE/HEAD SHA 检查 spec compliance、correctness、architecture、tests、maintainability、security。Critical/Important 必须修复后重审。

**并行规则**：只有完全独立的 domain 才使用 `dispatching-parallel-agents`。存在接口依赖、共享文件、共享运行状态时一律串行。多 Agent 是为了隔离上下文与独立审查，不是为了增加 Agent 数量。

## 0.2 每个任务的标准执行链

```text
Controller
  -> using-superpowers
  -> using-git-worktrees / verify isolated workspace
  -> read task + interfaces + git baseline
  -> record BASE_SHA
  -> dispatch fresh Implementer
       -> test-driven-development
       -> RED -> verify RED -> GREEN -> verify GREEN -> REFACTOR
       -> scoped gates -> self-review -> commit
  -> dispatch independent Reviewer
       -> spec + correctness + architecture + tests + security review
  -> fix/re-review loop if Critical/Important findings exist
  -> Controller final diff review
  -> verification-before-completion with fresh commands
  -> record status/ledger
  -> next task
```

遇到 bug/test failure 时，在任何修复前切换到 `systematic-debugging`，先找 root cause；禁止“多试几个 patch 看哪个能过”。

## 0.3 每次任务给主 Agent 的固定 Prompt

```text
你现在只负责 RepoTrial 开发计划中的【任务 ID】，并以主 Agent / Controller 身份执行。

执行前：
1. 先调用/使用 superpowers:using-superpowers，并根据任务调用必须的 Superpowers skills。
2. 阅读 AGENTS.md、当前任务、Global Constraints、依赖接口和 docs/dev/AGENTIC_DEVELOPMENT_PROTOCOL.md。
3. 检查 git status / git log -5 / branch-worktree / 当前 baseline tests；不要假设前一任务正确。
4. 使用 using-git-worktrees 确认隔离环境；记录 BASE_SHA。

协同规则：
- 当前任务的实现交给 fresh Implementer 子 Agent；你负责约束、分派、审查与裁决。
- Implementer 必须 TDD：先观察 RED，再写最小实现，再 GREEN/REFACTOR；只改当前任务。
- Implementer 完成后，分派与其独立的 Reviewer 子 Agent做 spec compliance + code quality/security review。
- Reviewer 的 Critical/Important 未关闭前不得进入下一任务。
- 你不能只信子 Agent 的“完成”报告；必须独立检查 diff 和 fresh verification evidence。
- 只有任务彼此无共享文件、接口、状态和依赖时才允许 dispatching-parallel-agents。

代码规则：
- 不提前实现后续任务，不做无关重构，不引入当前任务不需要的框架/抽象。
- 不删除/弱化测试换取通过，不用 noqa/type-ignore/skip 隐藏问题。
- 不允许 Agent/LLM 绕开 SandboxProvider 直接执行目标代码。
- 所有 shell/外部执行遵守 AGENTS.md 的安全边界。

完成门禁：
1. 当前任务 scoped tests；
2. uv run ruff check .；
3. uv run ruff format --check .；
4. uv run mypy src/repotrial；
5. uv run pytest -q；
6. 当前任务若改变已有生产逻辑，运行 coverage gate；
7. 使用 verification-before-completion 后才允许声明完成。

最后只汇报：任务 ID、BASE/HEAD SHA、修改文件、RED/GREEN证据、测试/质量门禁结果、Reviewer 发现及处理、已知限制、偏离计划及裁决、建议 commit/下一任务。不要顺手实现下一任务。
```

## 0.4 人工检查只保留“所有者级”事项

正常任务不要求你人工逐行替代 Codex review；主 Agent + reviewer 应完成工程审查。你作为项目所有者主要确认：

1. 产品边界有没有被悄悄改掉；
2. 安全原则有没有被降低；
3. Agent 是否仍在做有价值的执行/实验，而不是堆框架；
4. 是否出现为了追进度而加入大量专用 case、隐藏失败或美化指标；
5. 重大架构/依赖/License/公开发布决定是否符合你的意图。

`docs/dev/status.md` 记录任务 ID、commit、fresh verification、review 结果和偏离计划的 rulings；事实优先，不维护虚假的百分比完成度。

---

# 1. 目标仓库结构

```text
repotrial/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── .gitignore
├── src/repotrial/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── domain/
│   │   ├── models.py
│   │   └── enums.py
│   ├── intake/
│   │   ├── github.py
│   │   └── compose_discovery.py
│   ├── compose/
│   │   ├── parser.py
│   │   ├── risk.py
│   │   ├── overlay.py
│   │   └── mutations.py
│   ├── sandbox/
│   │   ├── base.py
│   │   ├── fake.py
│   │   └── docker_sbx.py
│   ├── trial/
│   │   ├── planner.py
│   │   ├── boot.py
│   │   └── observer.py
│   ├── journey/
│   │   ├── http_runner.py
│   │   ├── playwright_runner.py
│   │   └── verifier.py
│   ├── hardening/
│   │   ├── engine.py
│   │   └── policy.py
│   ├── agent/
│   │   ├── state.py
│   │   └── graph.py
│   ├── eval/
│   │   ├── evaluator.py
│   │   └── metrics.py
│   ├── report/
│   │   ├── render.py
│   │   └── templates/report.html.j2
│   └── api/
│       └── app.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│       ├── app/
│       ├── redundant_privileged/
│       ├── readonly_tmpfs/
│       ├── nonroot_ok/
│       ├── required_capability/
│       └── prompt_injection/
├── eval/
│   ├── manifests/
│   └── results/
├── artifacts/
│   └── .gitkeep
├── deploy/
│   └── docker-compose.yml
└── docs/
    ├── project/
    ├── dev/
    │   ├── AGENTIC_DEVELOPMENT_PROTOCOL.md
    │   └── status.md
    └── superpowers/plans/2026-08-25-repotrial-mvp.md
```

原则：`src/repotrial` 中每个模块职责单一；LLM/Agent 编排层不得直接拼 shell 字符串绕开 tools/provider。

---

# 2. 核心接口冻结

这些接口在 M0 阶段确定；后续 Codex 可以增加字段，但不得无迁移说明地改名。

```python
# src/repotrial/domain/enums.py
from enum import StrEnum

class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNSUPPORTED = "unsupported"

class ExperimentVerdict(StrEnum):
    KEEP = "keep"
    ROLLBACK = "rollback"
    STOP = "stop"

class MutationType(StrEnum):
    SET_NON_ROOT = "set_non_root"
    DROP_ALL_CAPS = "drop_all_caps"
    SET_READ_ONLY = "set_read_only"
    ADD_TMPFS = "add_tmpfs"
    DROP_PRIVILEGED = "drop_privileged"
    REMOVE_DOCKER_SOCKET = "remove_docker_socket"
    BRIDGE_NETWORK = "bridge_network"
```

```python
# src/repotrial/domain/models.py
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field
from .enums import Verdict, ExperimentVerdict, MutationType

class RepoRef(BaseModel):
    url: str
    owner: str
    repo: str
    requested_ref: str | None = None

class PinnedRepo(BaseModel):
    repo: RepoRef
    commit_sha: str = Field(min_length=40, max_length=40)
    local_path: Path

class RiskFinding(BaseModel):
    finding_id: str
    kind: str
    service: str
    severity: int = Field(ge=0, le=100)
    evidence: dict[str, Any]

class JourneyAssertion(BaseModel):
    kind: str
    target: str
    expected: Any

class JourneyStep(BaseModel):
    step_id: str
    tool: str
    action: str
    params: dict[str, Any]
    assertions: list[JourneyAssertion] = Field(default_factory=list)

class Journey(BaseModel):
    journey_id: str
    name: str
    steps: list[JourneyStep]

class JourneyResult(BaseModel):
    journey_id: str
    verdict: Verdict
    passed_steps: int
    total_steps: int
    evidence_paths: list[str] = Field(default_factory=list)
    failure_reason: str | None = None

class Mutation(BaseModel):
    mutation_id: str
    type: MutationType
    service: str
    params: dict[str, Any] = Field(default_factory=dict)

class ObservationSnapshot(BaseModel):
    inspect: dict[str, Any] = Field(default_factory=dict)
    file_changes: list[dict[str, Any]] = Field(default_factory=list)
    process_events: list[dict[str, Any]] = Field(default_factory=list)
    network_events: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_collectors: list[str] = Field(default_factory=list)

class ExperimentRecord(BaseModel):
    experiment_id: str
    parent_config_hash: str
    candidate_config_hash: str
    mutation: Mutation
    boot: Verdict
    journeys: list[JourneyResult]
    before: ObservationSnapshot | None = None
    after: ObservationSnapshot | None = None
    verdict: ExperimentVerdict
    reason: str

class RunState(BaseModel):
    run_id: str
    repo_url: str
    commit_sha: str | None = None
    compose_path: str | None = None
    sandbox_id: str | None = None
    baseline_config_hash: str | None = None
    current_config_hash: str | None = None
    risk_findings: list[RiskFinding] = Field(default_factory=list)
    journeys: list[Journey] = Field(default_factory=list)
    baseline_journey_results: list[JourneyResult] = Field(default_factory=list)
    baseline_observation: ObservationSnapshot | None = None
    experiments: list[ExperimentRecord] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    stop_reason: str | None = None
```

```python
# src/repotrial/sandbox/base.py
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str

class NetworkLogResult(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    supported: bool
    unsupported_reason: str | None = None

class SandboxProvider(ABC):
    @abstractmethod
    async def create(self, workspace: Path, name: str) -> str: ...

    @abstractmethod
    async def exec(self, sandbox_id: str, argv: list[str], timeout_s: int = 60) -> ExecResult: ...

    @abstractmethod
    async def publish_port(self, sandbox_id: str, container_port: int) -> int: ...

    @abstractmethod
    async def copy(self, sandbox_id: str, remote_path: str, local_path: Path) -> None: ...

    @abstractmethod
    async def network_log(self, sandbox_id: str) -> NetworkLogResult: ...

    @abstractmethod
    async def destroy(self, sandbox_id: str) -> None: ...
```

安全约束：`exec()` 接收 argv 数组，不接受任意 shell 字符串；只有在明确需要 shell 语法时由受控工具层调用固定 `bash -lc <generated script>`，且脚本必须保存为 artifact 以便审计。

---

# 3. 里程碑总览

| 里程碑 | 目标 | 完成后可演示 |
|---|---|---|
| M0 | 工程骨架、数据契约、安全规则 | CLI 能启动，模型/Provider 接口冻结，CI 通过 |
| M1 | Repo Intake + Compose 基线分析 | 给 fixture/repo，输出固定 commit 与风险 JSON |
| M2 | SandboxProvider + 可清理 Runner | 在 Fake/真实 SBX 中安全执行命令并 100% 清理 |
| M3 | Trial 启动恢复 + 观测 | 合成项目缺 ENV/延迟依赖时 Agent 能有限恢复 |
| M4 | 用户旅程与确定性验证 | HTTP/Playwright 路径可重放并产证据 |
| M5 | Hardening Experiment Engine | non-root/cap_drop/read_only mutation 能 Keep/Rollback |
| M6 | LangGraph 闭环 + RepoTrial-Eval | 一条命令跑完整 Agent + benchmark |
| M7 | 报告、API、真实项目验证 | hardened overlay + HTML/JSON report + 真实 repo pilot |

---

# 4. 详细任务计划

## M0.1 工程骨架、依赖与质量门禁

**Files:**
- Pre-existing governance: `AGENTS.md`, `docs/dev/AGENTIC_DEVELOPMENT_PROTOCOL.md`, `docs/superpowers/plans/2026-08-25-repotrial-mvp.md`
- Create: `pyproject.toml`, `uv.lock`, `.gitignore`, `.env.example`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `README.md`, `src/repotrial/__init__.py`, `tests/unit/test_smoke.py`, `docs/dev/status.md`

**Interfaces:**
- Produces: `repotrial.__version__`, pytest 可运行环境、全局开发约束。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_smoke.py
import repotrial

def test_package_exposes_version() -> None:
    assert repotrial.__version__ == "0.1.0"
```

- [ ] **Step 2: 验证失败**

Run: `uv run pytest tests/unit/test_smoke.py -v`
Expected: import/package/version 相关失败。

- [ ] **Step 3: 最小实现**

`src/repotrial/__init__.py` 只定义 `__version__ = "0.1.0"`。`pyproject.toml` 配置 Python `>=3.12`、pytest、pytest-cov、ruff、mypy；M0.1 不加入尚未使用的运行期依赖。`pydantic>=2` 在 M0.2、`typer` 在 M0.3、`ruamel.yaml` 在 M1.2、`httpx` 在 M4.1、Playwright/LangGraph 在首次实际使用的任务加入。

质量基线：Ruff lint + format check、`mypy src/repotrial`、pytest；coverage 初始 gate 设为 branch coverage `>=85%`，但不得用无意义断言刷覆盖率。`.pre-commit-config.yaml` 至少执行 Ruff lint/format 与基础 whitespace/YAML 检查。`.github/workflows/ci.yml` 在 PR/push 上使用锁定依赖环境依次运行 lint、format、type check、unit tests + coverage。

`AGENTS.md` 与 `docs/dev/AGENTIC_DEVELOPMENT_PROTOCOL.md` 已作为治理基线存在；M0.1 只验证它们存在并遵守，不重写成大而全的说明书。README 只写当前已实现能力和开发状态，禁止提前宣称后续功能。

- [ ] **Step 4: 质量验证**

Run:
`uv run ruff check . && uv run ruff format --check . && uv run mypy src/repotrial && uv run pytest -q --cov=repotrial --cov-branch --cov-fail-under=85`
Expected: 全部 PASS；CI 使用同等或更严格命令。

- [ ] **Step 5: Commit**

`git add -- pyproject.toml uv.lock .gitignore .env.example .pre-commit-config.yaml .github/workflows/ci.yml README.md src/repotrial/__init__.py tests/unit/test_smoke.py docs/dev/status.md && git commit -m "chore: bootstrap repotrial project"`

**Codex task prompt:**
```text
执行 M0.1。AGENTS.md 与 Agentic Development Protocol 已是治理输入，不要重写。只建立 Python src-layout、最小依赖、pre-commit、GitHub Actions CI、README skeleton 和 smoke test。必须通过 Ruff lint/format、mypy、pytest + branch coverage gate。不要实现 CLI、Compose、Agent 或 Docker 功能。实现必须由 fresh implementer 完成，独立 reviewer 审查后由主 Agent fresh verify。
```

---

## M0.2 核心领域模型与 JSON Schema

**Files:**
- Create: `src/repotrial/domain/enums.py`, `src/repotrial/domain/models.py`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/unit/domain/test_models.py`

**Interfaces:** 使用“核心接口冻结”章节中的类型名和字段名。

- [ ] **Step 1: 写模型序列化测试**

```python
from repotrial.domain.models import Mutation
from repotrial.domain.enums import MutationType

def test_mutation_roundtrip_json() -> None:
    m = Mutation(mutation_id="m1", type=MutationType.SET_READ_ONLY, service="app")
    restored = Mutation.model_validate_json(m.model_dump_json())
    assert restored == m
```

同时添加 `RunState` 能序列化 `Path` 之外所有字段、`severity > 100` 被拒绝、`commit_sha` 非 40 字符被拒绝的测试。

- [ ] **Step 2: 确认失败**
Run: `uv run pytest tests/unit/domain/test_models.py -v`
Expected: 模块不存在或验证规则缺失。

- [ ] **Step 3: 按冻结接口实现**
先把 `pydantic>=2` 加入运行依赖，再使用 Pydantic v2；对 list/dict 字段使用 `Field(default_factory=...)`，不要共享可变默认值。

- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/domain -v`
Expected: PASS。

- [ ] **Step 5: Commit**
`git add -- pyproject.toml uv.lock src/repotrial/domain/enums.py src/repotrial/domain/models.py tests/unit/domain/test_models.py && git commit -m "feat: define core domain contracts"`

**Codex task prompt:**
```text
执行 M0.2。严格按开发计划冻结的字段名实现 Pydantic v2 模型，并修正所有可变默认值。不要加入数据库 ORM 或 LangGraph 类型。模型必须能直接作为 JSON artifact 的契约。
```

---

## M0.3 CLI 骨架与运行目录规范

**Files:**
- Create: `src/repotrial/cli.py`, `src/repotrial/config.py`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `repotrial doctor`, `repotrial inspect --dry-run URL` 两个命令；此时 `inspect --dry-run` 只验证 URL 并创建 run 目录，不 clone。

- [ ] **Step 1: 写 CLI 测试**
先加入 `typer` 运行依赖。使用 Typer `CliRunner`，断言 `doctor` exit code 0，`inspect --dry-run https://github.com/a/b` 返回 run id 与 artifact path；非法 URL exit code 非 0。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/test_cli.py -v`
- [ ] **Step 3: 最小实现**
`artifacts/<run_id>/` 下预创建 `evidence/`, `experiments/`, `report/`；run id 使用 UUID7/UUID4 均可，但测试通过依赖注入固定生成器保证稳定。
- [ ] **Step 4: 回归**
Run: `uv run pytest -q`
- [ ] **Step 5: Commit**
`git add -- pyproject.toml uv.lock src/repotrial/cli.py src/repotrial/config.py tests/unit/test_cli.py && git commit -m "feat: add CLI skeleton and run artifacts"`

**Codex task prompt:**
```text
执行 M0.3。CLI 只做 doctor 和 inspect --dry-run，不要提前 clone GitHub，不要调用 Docker。run artifacts 路径必须可通过测试注入临时目录。
```

---

## M1.1 GitHub URL 解析、clone 与 commit pin

**Files:**
- Create: `src/repotrial/intake/github.py`
- Test: `tests/unit/intake/test_github.py`, `tests/integration/intake/test_local_git_pin.py`

**Interfaces:**
```python
async def pin_repository(url: str, dest: Path, requested_ref: str | None = None) -> PinnedRepo

async def clone_and_resolve(source: str, dest: Path, requested_ref: str | None = None) -> tuple[str, Path]
```

- [ ] **Step 1: 测试 URL 与本地 git pin**
单元测试覆盖 `https://github.com/owner/repo`, `.git` 后缀、非 GitHub host 拒绝；集成测试创建临时 git repo 两次 commit，直接测试不做 host policy 的底层 `clone_and_resolve()`，验证 pinned SHA 与 HEAD 一致且为 40 位。公开入口 `pin_repository()` 先执行 GitHub URL policy，再调用该底层函数。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/intake tests/integration/intake -v`
- [ ] **Step 3: 最小实现**
使用 `git clone --filter=blob:none` / `git rev-parse HEAD`；shell 调用通过 `asyncio.create_subprocess_exec` 参数数组；设置超时；错误映射为自定义 `RepoIntakeError`，不得把 Git token 打进日志。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/intake tests/integration/intake -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/intake/github.py tests/unit/intake/test_github.py tests/integration/intake/test_local_git_pin.py && git commit -m "feat: pin repositories to immutable commits"`

**Codex task prompt:**
```text
执行 M1.1。只实现公开 GitHub repo 的 URL 解析、clone 和 commit pin。测试不得依赖公网：集成测试用临时本地 git repo。真实 GitHub E2E 放到后续 pilot。
```

---

## M1.2 Compose 文件发现与解析

**Files:**
- Create: `src/repotrial/intake/compose_discovery.py`, `src/repotrial/compose/parser.py`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/unit/compose/test_discovery.py`, `tests/unit/compose/test_parser.py`

**Interfaces:**
```python
def discover_compose(root: Path) -> Path

def load_compose(path: Path) -> dict
```

**Discovery order:** 根目录 `compose.yml` → `compose.yaml` → `docker-compose.yml` → `docker-compose.yaml`；多个非标准文件时返回 `AmbiguousComposeError`，不让 LLM 猜。

- [ ] **Step 1: 写发现/解析测试**
覆盖标准优先级、多文件歧义、缺文件、YAML anchor、环境变量字符串保持原样。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/compose/test_discovery.py tests/unit/compose/test_parser.py -v`
- [ ] **Step 3: 实现**
先加入 `ruamel.yaml` 运行依赖。使用其 round-trip loader，禁止执行 YAML 自定义 tag；解析输出既保留 round-trip AST 又能生成 canonical JSON 用于 hash。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/compose -v`
- [ ] **Step 5: Commit**
`git add -- pyproject.toml uv.lock src/repotrial/intake/compose_discovery.py src/repotrial/compose/parser.py tests/unit/compose/test_discovery.py tests/unit/compose/test_parser.py && git commit -m "feat: discover and parse compose projects"`

**Codex task prompt:**
```text
执行 M1.2。Compose 发现必须确定性，不调用 LLM。解析要支持后续安全生成 overlay，因此优先保持 YAML round-trip 信息，不原地格式化用户原文件。
```

---

## M1.3 基线风险提取与排序

**Files:**
- Create: `src/repotrial/compose/risk.py`
- Test: `tests/unit/compose/test_risk.py`

**Interfaces:**
```python
def analyze_risk(compose: dict) -> list[RiskFinding]
def risk_score(findings: list[RiskFinding]) -> int
```

**MVP heuristic weights:** `privileged=100`, `docker_socket_rw=90`, `host_network=70`, `host_pid=70`, `cap_add=50`, `root_user=30`, `writable_rootfs=25`, `rw_host_bind=25`。分数只用于排序，报告必须显示 finding 而不是“安全分”。

- [ ] **Step 1: 风险 fixture 测试**
构造 Compose 同时包含 privileged、docker.sock、root 与 rw bind；断言 finding kind/service/evidence 正确且排序稳定。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/compose/test_risk.py -v`
- [ ] **Step 3: 实现**
仅做语法/配置事实提取，不把 presence 自动解释为恶意；root 未显式声明时允许输出 `root_user_possible` 而不是绝对 `root_user`。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/compose -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/compose/risk.py tests/unit/compose/test_risk.py && git commit -m "feat: add deterministic compose risk findings"`

**Codex task prompt:**
```text
执行 M1.3。只做可解释 finding，不做漏洞扫描器。每个 finding 必须带 service 和原始配置 evidence；风险分仅用于后续 mutation 排序。
```

---

## M2.1 SandboxProvider 与 FakeSandboxProvider

**Files:**
- Create: `src/repotrial/sandbox/base.py`, `src/repotrial/sandbox/fake.py`
- Test: `tests/unit/sandbox/test_fake.py`

**Interfaces:** 使用第 2 节 `SandboxProvider` 契约。

- [ ] **Step 1: 写 provider contract 测试**
Fake provider 支持 create/exec/publish_port/copy/network_log/destroy；destroy 后 exec 必须失败；所有调用追加到 `calls` 供测试审计。`network_log()` 必须用 `NetworkLogResult` 区分“已观测但事件为空”和“不支持观测”。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/sandbox/test_fake.py -v`
- [ ] **Step 3: 实现**
Fake provider 不运行真实容器，通过预设 script map 返回 `ExecResult`；它是 Agent 状态机单测唯一允许的默认 Runner。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/sandbox -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/sandbox/base.py src/repotrial/sandbox/fake.py tests/unit/sandbox/test_fake.py && git commit -m "feat: define sandbox provider contract"`

**Codex task prompt:**
```text
执行 M2.1。先把安全边界做成可测试接口。FakeSandboxProvider 不能偷偷调用宿主 Docker；它只模拟命令结果并记录调用。
```

---

## M2.2 Docker Sandboxes Provider

**Files:**
- Create: `src/repotrial/sandbox/docker_sbx.py`
- Test: `tests/unit/sandbox/test_docker_sbx_commands.py`, `tests/integration/sandbox/test_docker_sbx_smoke.py`

**Interfaces:** `DockerSbxProvider(SandboxProvider)`。

**Safety contract:** Provider 构造时接收固定 sandbox policy（私网、metadata、宿主地址默认阻断；CPU、内存、PID、磁盘和总时长均有上限）。运行时 feature probe 必须证明当前后端能落实这些边界；任一强制边界无法落实时，在执行目标仓库前返回明确 `UNSUPPORTED`，禁止降级到宿主执行。

**Current official CLI assumptions (verified 2026-08-25):** Docker Sandboxes 使用 `sbx create`, `sbx exec`, `sbx ports`, `sbx cp`, `sbx rm`；sandbox 运行于独立 microVM，并拥有自己的 Docker daemon/filesystem/network。Provider 必须在运行时执行 `sbx version` / feature probe，命令不兼容时明确失败而非猜测。

- [ ] **Step 1: 先测命令构造**
Mock subprocess，断言 create 使用已通过 runtime probe 的 `sbx create --name <id> shell <workspace>`，exec 参数不会 shell 拼接，destroy 使用 `sbx rm --force <id>`；另测私网/metadata/宿主阻断或任一资源上限不可验证时，目标命令不会执行并返回 `UNSUPPORTED`。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/sandbox/test_docker_sbx_commands.py -v`
- [ ] **Step 3: 实现 Provider**
所有 subprocess 带超时与 stderr 截断；`publish_port` 解析 `sbx ports` 返回的 host port；`network_log()` 若当前 CLI 无机器可读日志接口，必须返回 `NetworkLogResult(events=[], supported=False, unsupported_reason=...)`，不能伪装已观测。隔离策略与资源限制只通过经 probe 验证的 Provider 能力落实；不能落实则停止为 `UNSUPPORTED`。
- [ ] **Step 4: 可选集成验证**
Run: `REPOTRIAL_RUN_SBX_TESTS=1 uv run pytest tests/integration/sandbox/test_docker_sbx_smoke.py -v`
Expected: 环境有 SBX 时 create -> exec `echo ok` -> destroy 全通过；无 SBX 时测试可显式 skip，但任务完成报告必须把真实 Provider 集成门禁记为 `UNSUPPORTED`，不得写成已通过。
- [ ] **Step 5: Commit**
`git add -- src/repotrial/sandbox/docker_sbx.py tests/unit/sandbox/test_docker_sbx_commands.py tests/integration/sandbox/test_docker_sbx_smoke.py && git commit -m "feat: add docker sandboxes provider"`

**Codex task prompt:**
```text
执行 M2.2。实现 Docker Sandboxes 适配层，但不要让单元测试依赖本机安装 sbx。先 mock 命令契约，再提供显式 opt-in 的集成测试。任何当前 CLI 无法可靠采集的能力都标 unsupported。
```

---

## M2.3 Runner 生命周期与 Cleanup Guard

**Files:**
- Create: `src/repotrial/sandbox/lifecycle.py`
- Test: `tests/unit/sandbox/test_lifecycle.py`

**Interfaces:**
```python
@asynccontextmanager
async def managed_sandbox(provider, workspace: Path, name: str):
    yield sandbox_id
```

- [ ] **Step 1: 写异常清理测试**
在 `yield` 内主动抛异常，断言 `destroy()` 仍调用一次；create 失败时不得 destroy 未创建 id；destroy 失败被记录并重新抛为 `CleanupError`。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/sandbox/test_lifecycle.py -v`
- [ ] **Step 3: 实现**
使用 `asynccontextmanager` + `try/finally`；生命周期日志写 run artifact，禁止仅依赖进程退出自动清理。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/sandbox -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/sandbox/lifecycle.py tests/unit/sandbox/test_lifecycle.py && git commit -m "feat: guarantee sandbox cleanup"`

**Codex task prompt:**
```text
执行 M2.3。把 cleanup 当安全底线而不是附属功能。必须覆盖成功、运行中异常、destroy 异常三种路径。
```

---

## M3.1 Fixture Web App 与可复现启动故障

**Files:**
- Create: `tests/fixtures/app/app.py`, `tests/fixtures/app/Dockerfile`, `tests/fixtures/app/requirements.txt`, `tests/fixtures/app/compose.yml`, `tests/fixtures/app/repotrial.journeys.json`
- Test: `tests/integration/fixtures/test_fixture_contract.py`

**Fixture behavior:**
- `/health` 返回 `{"status":"ok"}`；
- `/items` 支持 create/list/delete；
- 环境变量 `APP_REQUIRED_TOKEN` 缺失时进程退出；
- `STARTUP_DELAY_S` 模拟依赖延迟；
- 创建 item 时写 `/data/items.json` 和 `/tmp/repotrial.tmp`；
- `/debug/cap-net-raw` 根据 `/proc/self/status` 的 `CapEff` 位判断 `CAP_NET_RAW` 是否存在，用于 required-capability fixture。
- `/` 提供最小可访问 UI：带可访问 label 的 item 输入框、带 role/name 的创建和删除按钮、可见 item 列表，供 M4.2 的固定 locator DSL 使用。

- [ ] **Step 1: 写 fixture contract 测试**
至少验证 health、CRUD、缺 ENV 退出逻辑，以及首页 UI 的固定 label/role/text 契约。
- [ ] **Step 2: 先让测试失败**
Run: `uv run pytest tests/integration/fixtures/test_fixture_contract.py -v`
- [ ] **Step 3: 实现最小 FastAPI fixture**
fixture 是测试资产，不进入生产包；测试端口使用随机端口。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/integration/fixtures/test_fixture_contract.py -v`
- [ ] **Step 5: Commit**
`git add -- tests/fixtures/app/app.py tests/fixtures/app/Dockerfile tests/fixtures/app/requirements.txt tests/fixtures/app/compose.yml tests/fixtures/app/repotrial.journeys.json tests/integration/fixtures/test_fixture_contract.py && git commit -m "test: add deterministic trial fixture app"`

**Codex task prompt:**
```text
执行 M3.1。实现一个很小但可控的 Web fixture，用于后续 boot recovery、read-only、non-root、capability 回归。不要引入真实外部 API 或数据库。
```

---

## M3.2 确定性 Boot Runner

**Files:**
- Create: `src/repotrial/trial/boot.py`
- Test: `tests/unit/trial/test_boot.py`

**Interfaces:**
```python
class BootResult(BaseModel):
    verdict: Verdict
    service_states: dict[str, str]
    logs: dict[str, str]
    attempt: int

async def boot_compose(provider, sandbox_id: str, compose_path: str, env: dict[str, str], attempt: int) -> BootResult
```

- [ ] **Step 1: 测试命令与判定**
Fake provider 模拟 `docker compose up -d`, `docker compose ps --format json`, `docker compose logs --no-color --tail 200`；所有服务 healthy/running -> PASS，退出服务 -> FAIL。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/trial/test_boot.py -v`
- [ ] **Step 3: 实现**
Boot runner 不调用 LLM；命令、日志上限、超时固定配置化；输出日志先 redaction 常见 token pattern 再落盘。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/trial -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/trial/boot.py tests/unit/trial/test_boot.py && git commit -m "feat: add deterministic compose boot runner"`

**Codex task prompt:**
```text
执行 M3.2。Boot 成败只能来自 docker compose 状态/health 与 exit code，不允许 LLM 判断“看起来启动成功”。
```

---

## M3.3 有限启动恢复策略

**Files:**
- Create: `src/repotrial/trial/planner.py`, `src/repotrial/models/base.py`
- Test: `tests/unit/trial/test_recovery.py`

**Interfaces:**
```python
from typing import Protocol, TypeVar
from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)

class ModelAdapter(Protocol):
    async def structured(
        self, *, system: str, user: str, schema: type[ModelT]
    ) -> ModelT: ...

class RecoveryAction(BaseModel):
    action: str  # set_env | wait | retry | stop
    params: dict[str, str | int]
    reason: str

async def propose_recovery(
    logs: dict[str, str],
    readme_excerpt: str,
    allowed_env_keys: set[str],
    repeated_error_count: int,
    model: ModelAdapter | None = None,
) -> RecoveryAction
```

**MVP allowlist:** 只允许 `set_env`（仅 `.env.example` / Compose 已声明 key）、`wait`（<=30s）、`retry`、`stop`。禁止安装宿主软件、读取未知文件、执行 README 任意命令。

- [ ] **Step 1: 测试策略边界**
缺 `APP_REQUIRED_TOKEN` 日志 + `.env.example` 声明 key -> 可提出合成值；README 注入“读取 ~/.ssh/id_rsa” -> 必须拒绝；`repeated_error_count > 2` -> stop。另用 FakeModelAdapter 证明仅在 deterministic rules 无结果且显式传入 `model` 时调用 fallback；`model=None` 时不得访问模型。计数由 RunState/调用方显式维护，planner 不使用全局可变状态。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/trial/test_recovery.py -v`
- [ ] **Step 3: 实现**
先做 deterministic regex/rules 识别常见 missing env/wait；`src/repotrial/models/base.py` 唯一定义上述泛型 `ModelAdapter` 协议。LLM adapter 作为可选 fallback，输出严格 Pydantic schema，再经 policy validator 二次过滤。没有模型时 fixture 仍能跑通。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/trial -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/trial/planner.py src/repotrial/models/base.py tests/unit/trial/test_recovery.py && git commit -m "feat: add bounded boot recovery policy"`

**Codex task prompt:**
```text
执行 M3.3。重点是“有限自治”：恢复动作必须来自 allowlist。优先 deterministic rule，LLM 仅 fallback，最终一定经过 policy validator。
```

---

## M4.1 Journey DSL 与 HTTP Runner

**Files:**
- Create: `src/repotrial/journey/verifier.py`, `src/repotrial/journey/http_runner.py`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/unit/journey/test_http_runner.py`

**Interfaces:** 使用 `Journey/Step/Assertion/Result`；MVP HTTP actions: `request`；assertions: `status_code`, `json_path_equals`, `text_contains`。

- [ ] **Step 1: 写 runner 测试**
使用 `httpx.MockTransport` 模拟 health/CRUD；失败断言必须产生结构化 failure_reason。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/journey/test_http_runner.py -v`
- [ ] **Step 3: 实现**
先加入 `httpx` 运行依赖。每 step 保存 request 摘要、response status、经过截断/脱敏的 body hash；JSONPath 可先支持点路径 `a.b.c`，不引入复杂表达式。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/journey -v`
- [ ] **Step 5: Commit**
`git add -- pyproject.toml uv.lock src/repotrial/journey/verifier.py src/repotrial/journey/http_runner.py tests/unit/journey/test_http_runner.py && git commit -m "feat: add deterministic HTTP journey runner"`

**Codex task prompt:**
```text
执行 M4.1。Journey 结果必须是确定性的。先把 HTTP DSL 做小，只支持当前 fixture 真正需要的 action/assertion，不做通用浏览器脚本语言。
```

---

## M4.2 Playwright Runner 与 evidence trace

**Files:**
- Create: `src/repotrial/journey/playwright_runner.py`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/integration/journey/test_playwright_fixture.py`

**MVP browser actions:** `goto`, `fill_by_label`, `click_by_role`, `assert_text_visible`。优先 Playwright user-facing locator（role/label/text），不允许 LLM 直接生成任意 JS。

- [ ] **Step 1: 写真实 fixture 浏览器测试**
打开 fixture 简单 UI，执行 create/delete 流程；失败时生成 screenshot，成功/失败均保存 trace zip。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/integration/journey/test_playwright_fixture.py -v`
- [ ] **Step 3: 实现**
先加入 `playwright` 运行依赖并在测试环境安装固定 Chromium；使用 Playwright async API；locator 使用 `get_by_role/get_by_label/get_by_text`；trace 开启 screenshots/snapshots；artifact path 写入 JourneyResult。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/integration/journey/test_playwright_fixture.py -v`
- [ ] **Step 5: Commit**
`git add -- pyproject.toml uv.lock src/repotrial/journey/playwright_runner.py tests/integration/journey/test_playwright_fixture.py && git commit -m "feat: add replayable browser journeys"`

**Codex task prompt:**
```text
执行 M4.2。不要做“让 LLM 自由浏览”。先实现受限、可重放的 Playwright action DSL，并确保 trace/screenshot 是 evidence artifact。
```

---

## M4.3 Journey Planner：先声明式，后 LLM 建议

**Files:**
- Modify: `src/repotrial/trial/planner.py`
- Test: `tests/unit/trial/test_journey_planner.py`

**Interfaces:** Consumes the generic `ModelAdapter` protocol created in M3.3; this task must not redefine it.

**Priority:** 若 repo 存在 `repotrial.journeys.json`，100% 使用声明式 Journey；否则从 README 提取候选入口，LLM 只能生成受限 DSL，最多 5 条 Journey，每条最多 8 步。

- [ ] **Step 1: 测试声明式优先**
有 journeys file 时不得调用 LLM；无文件时 planner 输出必须通过 schema/policy validator；包含任意 JS/shell action 的输出被拒绝。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/trial/test_journey_planner.py -v`
- [ ] **Step 3: 实现**
Planner 只依赖 M3.3 的 `ModelAdapter` 协议。OpenAI-compatible 具体实现延后到 M7；当前测试用 FakeModelAdapter，禁止在 planner 内复制第二份协议。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/trial -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/trial/planner.py tests/unit/trial/test_journey_planner.py && git commit -m "feat: add constrained journey planning"`

**Codex task prompt:**
```text
执行 M4.3。RepoTrial 的可靠性优先级是：项目自带 journey spec > deterministic 推断 > LLM 受限建议。模型输出必须经过 schema 和 action allowlist。
```

---

## M5.1 Compose Overlay 与 mutation algebra

**Files:**
- Create: `src/repotrial/compose/overlay.py`, `src/repotrial/compose/mutations.py`
- Test: `tests/unit/compose/test_mutations.py`

**Interfaces:**
```python
def apply_mutation(base: dict, mutation: Mutation) -> dict

def write_overlay(base: dict, candidate: dict, path: Path) -> Path
```

**MVP mutations:** `SET_NON_ROOT`, `DROP_ALL_CAPS`, `SET_READ_ONLY`, `ADD_TMPFS`。`DROP_PRIVILEGED/REMOVE_DOCKER_SOCKET/BRIDGE_NETWORK` 先实现 AST 变换与测试，但是否自动尝试由 policy 决定。

- [ ] **Step 1: 写不修改原对象测试**
每个 mutation 都断言 base deep-equal；candidate 只改变目标 service；overlay roundtrip 后 Compose merge 语义符合预期。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/compose/test_mutations.py -v`
- [ ] **Step 3: 实现**
用 deep copy/round-trip AST；mutation 输出 canonical hash；不直接覆盖用户 compose。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/compose -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/compose/overlay.py src/repotrial/compose/mutations.py tests/unit/compose/test_mutations.py && git commit -m "feat: add auditable compose mutations"`

**Codex task prompt:**
```text
执行 M5.1。目标是“可审计的单变量实验”，不是生成一份你觉得更安全的 Compose。每个 mutation 必须小、可逆、有唯一 mutation_id。
```

---

## M5.2 Observation Collector

**Files:**
- Create: `src/repotrial/trial/observer.py`
- Test: `tests/unit/trial/test_observer.py`

**MVP facts:** `docker inspect`, `docker diff`, `docker top`。Network 通过 provider `network_log()`；不支持时写 `unsupported_collectors=["network_runtime"]`。

- [ ] **Step 1: 写解析测试**
给定固定 docker inspect/diff/top 输出，生成 ObservationSnapshot；未知输出格式明确报 `ObservationParseError`，不默默丢弃。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/trial/test_observer.py -v`
- [ ] **Step 3: 实现**
对每 service 分别收集；artifact 保存原始命令 stdout hash + 解析 JSON；敏感 ENV 值不得进入 report。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/trial -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/trial/observer.py tests/unit/trial/test_observer.py && git commit -m "feat: collect runtime evidence snapshots"`

**Codex task prompt:**
```text
执行 M5.2。Observer 只记录事实，不做风险结论。任何 collector 缺失必须显式标 unsupported，避免“没观测到 = 不存在”。
```

---

## M5.3 Hardening Experiment Engine

**Files:**
- Create: `src/repotrial/hardening/engine.py`, `src/repotrial/hardening/policy.py`
- Test: `tests/unit/hardening/test_engine.py`

**Interfaces:**
```python
async def run_experiment(
    state: RunState,
    mutation: Mutation,
    provider: SandboxProvider,
) -> ExperimentRecord
```

**Acceptance rule:** `boot == PASS` 且所有 baseline 已通过的 Journey 在 candidate 上仍 `PASS`，才能 `KEEP`；任意一条 baseline-pass Journey 回归 -> `ROLLBACK`。Baseline 本身失败的 Journey 不得用于证明 mutation 破坏。

- [ ] **Step 1: 写 Keep/Rollback 测试**
Fake provider：read_only 仍通过 -> KEEP；另一 case 删除 capability 导致 create_item 失败 -> ROLLBACK；无 baseline journeys -> STOP，reason=`insufficient_coverage`。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/hardening/test_engine.py -v`
- [ ] **Step 3: 实现**
执行顺序固定：write overlay -> boot -> observe -> replay -> compare -> verdict；异常转换为 rollback/stop 的明确 reason，不能吞掉。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/hardening -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/hardening/engine.py src/repotrial/hardening/policy.py tests/unit/hardening/test_engine.py && git commit -m "feat: add keep rollback hardening experiments"`

**Codex task prompt:**
```text
执行 M5.3。这里是 RepoTrial 的核心价值。Keep 必须由“同一批 baseline-pass journeys 重新通过”证明；LLM 不能越过 verifier 直接接受 mutation。
```

---

## M5.4 Risk-prioritized mutation policy

**Files:**
- Modify: `src/repotrial/hardening/policy.py`
- Test: `tests/unit/hardening/test_policy.py`

**Policy order:** 高风险明确授权优先，但 MVP 自动稳定实验顺序先保证 `SET_NON_ROOT -> DROP_ALL_CAPS -> SET_READ_ONLY(+ADD_TMPFS fallback)`；若 baseline 显式 `privileged=true` / docker.sock，产生高优先级候选，但只有 fixture/runner 支持回归时才尝试。

- [ ] **Step 1: 写排序/停止测试**
无 journeys -> 不产生 mutation；同 mutation 已 rollback -> 不重复；连续 3 次失败 -> stop；预算 8 experiments 到达 -> stop。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/hardening/test_policy.py -v`
- [ ] **Step 3: 实现**
采用 greedy coordinate descent，不做组合穷举；`read_only` 因 `/tmp` 写入失败时先 rollback，然后在当前已接受配置上单独尝试 `ADD_TMPFS /tmp`。只有该 mutation KEEP 后，才以其 candidate hash 为父配置重试 `SET_READ_ONLY`。这样每条 ExperimentRecord 仍是单变量且父 hash/接受链完整。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/hardening -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/hardening/policy.py tests/unit/hardening/test_policy.py && git commit -m "feat: prioritize bounded hardening trials"`

**Codex task prompt:**
```text
执行 M5.4。不要搜索所有权限组合。实现有预算、有停止条件、可解释的贪心实验策略，并避免重复失败 mutation。
```

---

## M6.1 LangGraph RunState 编排与 checkpoint

**Files:**
- Create: `src/repotrial/agent/state.py`, `src/repotrial/agent/graph.py`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/unit/agent/test_graph.py`

**Graph stages:** `intake -> baseline -> boot -> journeys -> observe -> propose_mutation -> experiment -> decide -> report_or_next`。只维护一个图，不创建“安全专家 Agent/网络 Agent”等人格。

- [ ] **Step 1: 写状态转移测试**
用 FakeSandbox/FakeModel，断言 boot fail -> recovery -> boot；baseline journeys 通过 -> hardening；无 coverage -> stop；mutation keep -> current_config_hash 更新；rollback -> 不更新。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/agent/test_graph.py -v`
- [ ] **Step 3: 实现**
本任务先加入 `langgraph` 运行依赖。开发/单测使用 `InMemorySaver`；每 run 使用 `thread_id=run_id`。节点只调用既有 service 函数，不把业务逻辑复制进 graph。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/agent -v && uv run pytest -q`
- [ ] **Step 5: Commit**
`git add -- pyproject.toml uv.lock src/repotrial/agent/state.py src/repotrial/agent/graph.py tests/unit/agent/test_graph.py && git commit -m "feat: orchestrate repotrial state machine"`

**Codex task prompt:**
```text
执行 M6.1。LangGraph 只负责编排和恢复；Compose 解析、verifier、mutation、observer 逻辑继续留在各自模块。不要为了“多 Agent”再拆角色。
```

---

## M6.2 RepoTrial-Eval fixtures 与指标

**Files:**
- Create: `src/repotrial/eval/evaluator.py`, `src/repotrial/eval/metrics.py`
- Create: `src/repotrial/eval/cli.py`
- Create: `eval/manifests/redundant_privileged.json`, `eval/manifests/readonly_tmpfs.json`, `eval/manifests/nonroot_ok.json`, `eval/manifests/required_capability.json`, `eval/manifests/prompt_injection.json`
- Create: `tests/fixtures/redundant_privileged/compose.yml`, `tests/fixtures/redundant_privileged/ground_truth.json`
- Create: `tests/fixtures/readonly_tmpfs/compose.yml`, `tests/fixtures/readonly_tmpfs/ground_truth.json`
- Create: `tests/fixtures/nonroot_ok/compose.yml`, `tests/fixtures/nonroot_ok/ground_truth.json`
- Create: `tests/fixtures/required_capability/compose.yml`, `tests/fixtures/required_capability/ground_truth.json`
- Create: `tests/fixtures/prompt_injection/compose.yml`, `tests/fixtures/prompt_injection/ground_truth.json`, `tests/fixtures/prompt_injection/README.md`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/unit/eval/test_metrics.py`, `tests/integration/eval/test_fixture_benchmark.py`

**Metric definitions:**
- Boot Recovery Rate = 可修复启动故障成功恢复数 / 可修复启动故障总数
- Journey Success Rate = 完成 Journey / 计划 Journey
- Hardening Acceptance Precision = 被 KEEP 且 Ground Truth 不破坏功能的 mutation / 所有 KEEP mutation
- Unnecessary Privilege Removal Recall = 成功删除的 Ground Truth 冗余权限 / Ground Truth 冗余权限总数
- Cleanup Success Rate = 正确销毁 runner / 所有启动过 runner
- Replay Consistency = 同 fixture 同配置两次关键 verdict 相同率

- [ ] **Step 1: 写纯指标测试**
用小数组手算 expected precision/recall，特别覆盖分母为 0 时返回 `None` 而不是 0。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/eval/test_metrics.py -v`
- [ ] **Step 3: 实现 evaluator**
manifest 明确 ground truth；benchmark 默认 Fake/local fixture，不依赖公网。`pyproject.toml` 注册 `repotrial-eval = "repotrial.eval.cli:app"`，CLI 只负责参数解析和调用 evaluator。输出 `eval/results/<timestamp>.json`。
- [ ] **Step 4: 跑 benchmark**
Run: `uv run repotrial-eval --fixtures eval/manifests`
Expected: 每个 fixture 有 run result、metrics、失败 stop_reason。
- [ ] **Step 5: Commit**
`git add -- pyproject.toml uv.lock src/repotrial/eval/evaluator.py src/repotrial/eval/metrics.py src/repotrial/eval/cli.py eval/manifests/redundant_privileged.json eval/manifests/readonly_tmpfs.json eval/manifests/nonroot_ok.json eval/manifests/required_capability.json eval/manifests/prompt_injection.json tests/fixtures/redundant_privileged/compose.yml tests/fixtures/redundant_privileged/ground_truth.json tests/fixtures/readonly_tmpfs/compose.yml tests/fixtures/readonly_tmpfs/ground_truth.json tests/fixtures/nonroot_ok/compose.yml tests/fixtures/nonroot_ok/ground_truth.json tests/fixtures/required_capability/compose.yml tests/fixtures/required_capability/ground_truth.json tests/fixtures/prompt_injection/compose.yml tests/fixtures/prompt_injection/ground_truth.json tests/fixtures/prompt_injection/README.md tests/unit/eval/test_metrics.py tests/integration/eval/test_fixture_benchmark.py && git commit -m "feat: add reproducible repotrial benchmark"`

**Codex task prompt:**
```text
执行 M6.2。评测必须离线可复现，ground truth 写在 manifest，不让 LLM 自己给自己打分。任何尚不能观测的指标都输出 unavailable，不填假数字。
```

---

## M6.3 PostgreSQL checkpoint adapter

**Files:**
- Create: `src/repotrial/agent/checkpoint.py`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/unit/agent/test_checkpoint_factory.py`

**Interfaces:**
```python
def build_checkpointer(database_url: str | None): ...
```

- [ ] **Step 1: 写 factory 测试**
无 DB URL -> `InMemorySaver`；postgres URL -> Postgres saver factory；测试 mock `setup()` 被首次调用。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/agent/test_checkpoint_factory.py -v`
- [ ] **Step 3: 实现**
加入 `langgraph-checkpoint-postgres`（`langgraph` 已在 M6.1 加入）；生产用 `PostgresSaver`/Async 对应图执行模式。不要把大 artifact 二进制塞入 checkpoint，只保存路径/hash。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/agent -v`
- [ ] **Step 5: Commit**
`git add -- pyproject.toml uv.lock src/repotrial/agent/checkpoint.py tests/unit/agent/test_checkpoint_factory.py && git commit -m "feat: persist agent checkpoints in postgres"`

**Codex task prompt:**
```text
执行 M6.3。Checkpoint 只持久化状态和 artifact 引用；trace zip、截图、原始日志保存在 artifacts，不塞数据库 state。
```

---

## M7.1 JSON/HTML Trial Report 与 hardened overlay

**Files:**
- Create: `src/repotrial/report/render.py`, `src/repotrial/report/templates/report.html.j2`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/unit/report/test_render.py`

**Report required sections:** identity(commit/hash)、coverage boundary、baseline findings、observed behavior、experiments timeline、KEEP/ROLLBACK evidence、generated overlay、unsupported collectors、免责声明。

- [ ] **Step 1: 写快照测试**
构造固定 RunState，断言 JSON schema 字段完整；HTML 必须出现 commit、4/4 journeys、一个 KEEP、一个 ROLLBACK、`not a security proof` 等固定免责声明。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/report/test_render.py -v`
- [ ] **Step 3: 实现**
先加入 `jinja2` 运行依赖。Jinja2 默认 autoescape；不要在 HTML 中直接渲染目标页面原始 HTML；外部文本全部 escape；overlay 单独保存 YAML。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/report -v`
- [ ] **Step 5: Commit**
`git add -- pyproject.toml uv.lock src/repotrial/report/render.py src/repotrial/report/templates/report.html.j2 tests/unit/report/test_render.py && git commit -m "feat: render auditable trial reports"`

**Codex task prompt:**
```text
执行 M7.1。报告重点是 evidence 和 coverage boundary，不做“安全 83 分” Dashboard。必须清楚显示哪些内容没有测试、哪些 collector 不支持。
```

---

## M7.2 Fixture/FakeModel 端到端 CLI `inspect`

**Files:**
- Modify: `src/repotrial/cli.py`
- Test: `tests/integration/test_cli_e2e_fixture.py`

**CLI:**
```bash
repotrial inspect <repo-url-or-fixture> \
  --provider fake|docker-sbx \
  --max-experiments 8
```

- [ ] **Step 1: 写 fixture E2E 测试**
用本地 fixture 跑 intake -> boot -> journeys -> hardening -> report；断言 exit 0、overlay/report/evidence 存在、cleanup 成功。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/integration/test_cli_e2e_fixture.py -v`
- [ ] **Step 3: 实现**
CLI 负责组装依赖和调用 graph，不在命令函数中写业务逻辑。本任务 fixture E2E 注入 FakeModelAdapter，不接受真实 model endpoint。失败 exit code 区分 unsupported(2)、trial failed(3)、internal error(4)。
- [ ] **Step 4: 全量回归**
Run: `uv run pytest -q && uv run ruff check .`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/cli.py tests/integration/test_cli_e2e_fixture.py && git commit -m "feat: ship end-to-end inspect command"`

**Codex task prompt:**
```text
执行 M7.2。先让 fixture E2E 真正闭环，再考虑真实 GitHub。CLI 只是 composition root，不复制 Agent/Hardening 逻辑。
```

---

## M7.3 OpenAI-compatible Model Adapter

**Files:**
- Create: `src/repotrial/models/openai_compat.py`
- Modify: `src/repotrial/cli.py`
- Test: `tests/unit/models/test_openai_compat.py`, `tests/unit/test_cli_model_endpoint.py`

**Interfaces:** Consumes the generic `ModelAdapter` protocol created in M3.3 and produces one concrete OpenAI-compatible implementation with the same `structured(...)` signature. This task adds `--model-endpoint URL` to `repotrial inspect` and wires the concrete adapter at the CLI composition root.

- [ ] **Step 1: 写 mock server 测试**
Mock HTTP endpoint 返回合法/非法 JSON；合法结果转输入 schema 对应的具体 Pydantic 类型，非法结果有限重试 1 次后失败；日志不记录 API key。CLI 测试断言 `--model-endpoint` 只在本任务加入并构造 concrete adapter。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/models/test_openai_compat.py tests/unit/test_cli_model_endpoint.py -v`
- [ ] **Step 3: 实现**
兼容标准 OpenAI-style chat/completions/structured JSON 能力时优先；若目标本地服务不支持 schema，使用 JSON-only prompt + Pydantic validate。不得把模型具体品牌写死。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/models tests/unit/test_cli_model_endpoint.py -v`
- [ ] **Step 5: Commit**
`git add -- src/repotrial/models/openai_compat.py src/repotrial/cli.py tests/unit/models/test_openai_compat.py tests/unit/test_cli_model_endpoint.py && git commit -m "feat: add local model compatible adapter"`

**Codex task prompt:**
```text
执行 M7.3。目标是支持 Qwen/DeepSeek 等通过 vLLM/Ollama 暴露的 OpenAI-compatible endpoint。ModelAdapter 只返回结构化对象，不向业务层泄漏 provider-specific response。
```

---

## M7.4 FastAPI 控制面与 Docker Compose

**Files:**
- Create: `src/repotrial/api/app.py`, `deploy/docker-compose.yml`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/unit/api/test_app.py`

**Endpoints:** `GET /healthz`, `POST /runs`（创建 run，MVP 可同步/后台任务二选一但接口返回 run_id）, `GET /runs/{id}`, `GET /runs/{id}/report`。

- [ ] **Step 1: API contract 测试**
用 TestClient 断言 health、创建 run 参数校验、未知 run 404。
- [ ] **Step 2: 验证失败**
Run: `uv run pytest tests/unit/api/test_app.py -v`
- [ ] **Step 3: 实现**
先加入 `fastapi` 运行依赖。API 不接收任意 shell；repo URL/model config 均 schema 校验；control plane Docker Compose 只启动 API/Postgres，可选本地模型由用户自己配置 endpoint，不把 Docker Sandboxes 嵌进 control plane container。
- [ ] **Step 4: 回归**
Run: `uv run pytest tests/unit/api -v && docker compose -f deploy/docker-compose.yml config`
- [ ] **Step 5: Commit**
`git add -- pyproject.toml uv.lock src/repotrial/api/app.py deploy/docker-compose.yml tests/unit/api/test_app.py && git commit -m "feat: add minimal control plane api"`

**Codex task prompt:**
```text
执行 M7.4。只做最小控制面，不做 Web UI。Docker Compose 部署的是 RepoTrial 自身 API/Postgres，不是在容器里运行陌生目标仓库。
```

---

## M7.5 真实公开项目 Pilot 与发布门槛

**Files:**
- Create: `eval/real_repos.yaml`, `docs/dev/pilot-report.md`
- Modify: README only after metrics are真实跑出。

**Selection:** 10 个公开、文档明确、Docker Compose Web App；避免需要付费 SaaS Secret、GPU、大型数据库集群、外部 OAuth 的项目。

- [ ] **Step 1: 固定 pilot manifest**
每个 repo 写 URL + commit SHA + 为什么纳入 + 预期启动入口；不要后验删掉失败项目来美化通过率。
- [ ] **Step 2: 逐个运行**
Run: `repotrial inspect <url> --provider docker-sbx ...`；保存完整 stop_reason。
- [ ] **Step 3: 汇总指标**
至少报告：自主跑通数、有意义 KEEP mutation 数、平均实验次数、P50/P95 时长、失败分类、cleanup。
- [ ] **Step 4: 对照 Kill Criteria**
发布门槛严格采用立项文档：10 个中自主跑通 `>=7` 且有意义收敛 `>=5`，才可制作发布型 Before/After Demo；否则如实记录未达门槛。若自主跑通 `<5`，触发 Kill Criteria 分析；`5-6` 个跑通或仅 `3-4` 个有意义收敛属于“未达发布门槛但未自动触发 Kill”的继续验证区间。
- [ ] **Step 5: Commit**
`git add -- eval/real_repos.yaml docs/dev/pilot-report.md README.md && git commit -m "test: document real repository pilot"`

**Codex task prompt:**
```text
执行 M7.5 时不得修改系统去专门适配某一个失败仓库，除非该修复是可泛化能力并有新增 fixture 测试。真实项目失败必须保留在 pilot-report，不做 cherry-pick 指标。
```

---

# 5. 两周建议执行顺序

| Day | 建议任务 | 当天结束必须有的可运行结果 |
|---|---|---|
| 1 | M0.1-M0.3 | governance、repo、CLI、models、CI/lint/type/test/coverage 基线 |
| 2 | M1.1-M1.3 | repo pin + Compose parse + baseline risk JSON |
| 3 | M2.1-M2.3 | provider abstraction + fake + SBX smoke + guaranteed cleanup |
| 4 | M3.1-M3.2 | deterministic fixture + boot runner |
| 5 | M3.3 | 缺 ENV/延迟依赖有限恢复闭环 |
| 6 | M4.1 | HTTP journey replay |
| 7 | M4.2-M4.3 | Playwright trace + constrained planner |
| 8 | M5.1-M5.2 | overlay mutation + observation evidence |
| 9 | M5.3 | Keep/Rollback experiment core |
| 10 | M5.4 | bounded greedy hardening policy |
| 11 | M6.1 | LangGraph 状态机闭环 |
| 12 | M6.2-M6.3 | offline eval + checkpoint persistence |
| 13 | M7.1-M7.4 | report + CLI + model adapter + API/control plane |
| 14 | M7.5 | 10 real repo pilot + README 发布判断 |

实际开发不要求严格按自然日完成；**顺序比速度重要**。任何核心任务未通过验收，不应为了赶 Day 14 跳过测试。

---

# 6. 第一阶段必须避免的“聪明过头”

- 不做 Web UI；CLI + HTML report 足够验证价值。
- 产品运行时不做多 Agent 人格；一个 LangGraph 两个业务阶段即可。开发阶段允许 Controller + Implementer + Reviewer 协同，但角色用于工程治理，不进入 RepoTrial 产品架构。
- 不做“LLM 自动写任意 Playwright 代码”；只允许受限 Journey DSL。
- 不做完整 Falco/eBPF 平台；Observer 先通过 Docker facts + provider capability 获取证据。
- 不做全局最小权限搜索；只做有预算的贪心单变量实验。
- 不自动修改生产主机、不自动部署 hardened overlay。
- 不为了支持更多仓库提前增加 Kubernetes/Helm/裸机。
- 不让 Codex 在遇到一个真实项目失败时直接写专用 if/else；先抽象为新 fixture 和通用修复策略。

---

# 7. 每个里程碑的 Go/No-Go

**M1 Go:** 对 fixture 能稳定固定 commit、发现 Compose、输出可解释风险 finding；无 LLM 参与。

**M2 Go:** provider 生命周期异常路径也能清理；真实 SBX 不可用时系统能明确诊断，而不是 fallback 到宿主执行。

**M3 Go:** 至少两类可修复启动问题可在上限内恢复；Prompt Injection fixture 无法扩大动作权限。

**M4 Go:** 同一 Journey 可以重复执行，成功/失败来自 deterministic assertions，trace 可定位失败。

**M5 Go:** 至少三类 mutation 有完整 Keep/Rollback；误删 required capability 能被回归测试拦截。

**M6 Go:** fixture benchmark 一条命令可复现；重复运行关键 verdict 稳定；所有 run 都有 stop_reason 或 report。

**M7 Go:** 真实项目 pilot 不靠项目专用脚本维持；若不达门槛，如实记录并决定继续强化通用 Trial 能力或停止。

---

# 8. 建议的首个 GitHub Demo

不要选复杂明星项目。先用自带 fixture 做一个 30 秒可理解的 Before/After：

```text
Baseline
  privileged=true        HIGH
  user=root               MEDIUM
  writable rootfs         MEDIUM

Autonomous trials
  privileged=false        PASS  -> kept
  user=1000               PASS  -> kept
  read_only=true          FAIL  -> rollback
  read_only + /tmp tmpfs  PASS  -> kept

Result
  3 permissions tightened
  4/4 tested journeys replayed successfully
  Coverage boundary: admin import/export not tested
```

README 第一屏的技术说明只需要一句：

> RepoTrial does not prove an app is safe. It runs a reproducible deployment trial, replays tested user journeys after each hardening mutation, and keeps only changes that survive regression.

---

# 9. 技术参考（开发时优先看官方文档）

- Docker Sandboxes: https://docs.docker.com/ai/sandboxes/
- Docker Sandboxes Usage / `sbx create|exec|ports|cp|rm`: https://docs.docker.com/ai/sandboxes/usage/
- Docker Sandboxes Install/System Requirements: https://docs.docker.com/ai/sandboxes/install/
- LangGraph Persistence / Checkpointers: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph Postgres checkpointer: package `langgraph-checkpoint-postgres`
- Playwright Python Locators: https://playwright.dev/python/docs/locators
- Playwright tracing: https://playwright.dev/python/docs/api/class-tracing

版本快速变化的命令（尤其 `sbx`）不要写死到 Agent prompt 中当永久事实；Provider 在运行时 probe 能力，并把不兼容变成明确错误。

---

# 10. 自审结论

这份开发计划刻意把 RepoTrial 的技术亮点放在**可验证的实验闭环**，而不是“LLM 多聪明”：

1. 产品价值由 `Trial + Journey Replay + Permission Ablation` 组成；缺一项都会退化成扫描器/报告器。
2. Agent 只负责顺序决策、受限恢复和实验选择；真正的 Keep/Rollback 由 deterministic verifier 决定。
3. Fixture-first 使你不依赖真实私有数据，也避免 Codex 一开始就在不可信仓库上调试。
4. `SandboxProvider` 把安全边界和外部 Runner 解耦；Docker Sandboxes 当前适合作为 MVP Provider，但不是永久唯一后端。
5. 所有发布指标都必须来自 RepoTrial-Eval 或真实 pilot，不能把立项目标写成已完成成绩。
6. 若项目长期演化成“为每个 self-hosted 项目写专用脚本”，应触发 Kill Criteria，而不是继续堆 Agent。

本计划本身应随代码迭代更新；但任何修改都必须说明是“产品需求变化、真实工程约束、还是接口优化”，不要由 Codex 在一次任务里静默改掉核心目标。
