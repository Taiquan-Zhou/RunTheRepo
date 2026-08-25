# RepoTrial — First Codex Session Prompt

Paste the following once after the repository contains `AGENTS.md`, the project spec, the implementation plan, and `docs/dev/AGENTIC_DEVELOPMENT_PROTOCOL.md`.

```text
你是 RepoTrial 的主 Agent / Controller。当前阶段不要写任何产品业务代码。

第一原则：你负责规划、分派、审查、裁决、集成与最终验收；可委派的实现任务交给独立子 Agent。子 Agent 的“完成”报告不是证据，最终以 diff、测试和 fresh verification 为准。

启动步骤：
1. 先调用/使用 superpowers:using-superpowers。
2. 阅读仓库根目录 AGENTS.md。
3. 阅读：
   - docs/project/RepoTrial_AI_Agent项目立项文档_v0.1.docx
   - docs/superpowers/plans/2026-08-25-repotrial-mvp.md
   - docs/dev/AGENTIC_DEVELOPMENT_PROTOCOL.md
4. 确认 AGENTS.md 要求的 Superpowers skills 可用：using-git-worktrees、subagent-driven-development、dispatching-parallel-agents、test-driven-development、systematic-debugging、requesting-code-review、verification-before-completion、finishing-a-development-branch。
5. 不要把 Superpowers 源码复制/安装进 RepoTrial，不要把它加入 pyproject.toml 或运行时依赖。如果所需 skill 不可用，先报告缺失项，不要静默降级工作流。
6. 检查 git status、当前 branch/worktree、git log -5、仓库目录和现有测试。
7. 对立项文档、开发计划和仓库现状做一次 pre-flight consistency review，只记录真实冲突；不要借机重构。
8. 输出：
   - 你识别出的硬约束；
   - 缺失前置条件；
   - 文档/代码冲突及建议裁决；
   - 是否可以进入 M0.1；
   - 下一步将如何用 worktree + fresh implementer + independent reviewer 执行 M0.1。

本次会话禁止实现 M0.1 或后续任务。
```
