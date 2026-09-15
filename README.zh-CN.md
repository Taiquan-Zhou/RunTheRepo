![RunTheRepo——从固定源码到可审计结果](docs/assets/readme-hero.svg)

在一次性沙箱中运行 GitHub Docker Compose 应用，验证工作流、测试加固，并生成有据可查的报告。

[快速开始](#快速开始) · [使用指南](docs/dev/usage.md) · [环境设置](docs/dev/wsl2-linux-sbx-setup.md)

[English README](README.md) · 中文

## 工作方式

![RunTheRepo 中文动态工作流程：固定版本、运行、验证、加固、报告与清理](docs/assets/workflow.zh-CN.gif)

## 功能演示

### 1.项目自动读取与环境检查

https://github.com/user-attachments/assets/4380f694-e0c7-41ef-a5fe-14feead3bedb

### 2.运行过程与结果

https://github.com/user-attachments/assets/ec45bca0-173c-476b-9086-8473a86d999c

## 你将获得什么

| 能力 | 结果 |
| --- | --- |
| 固定提交的试跑 | 明确知道测试的是哪一个源代码版本 |
| 确定性的 HTTP 检查 | 检查响应状态和内容，包括已声明的认证工作流 |
| 经过测试的加固 | 根据重放后的检查结果保留或回滚每项改动 |
| 可用的输出 | JSON/HTML 报告、证据引用，以及在来源有效时生成的累计加固 overlay |
| 本地控制台 | 提交一次试跑、跟踪状态并打开报告 |

## 快速开始

**开始前：** 使用 Ubuntu 24.04 的发行版 ext4 文件系统、WSL2、systemd、可用的嵌套 KVM，以及配置了经审核的默认拒绝策略的认证 Docker Sandboxes **v0.42.0**。不支持原生 Windows 和宿主机 Docker 执行。如果这些前置条件尚未准备好，请先阅读[一次性设置指南](docs/dev/wsl2-linux-sbx-setup.md)。

在已检出的项目目录中，并确保已安装 `uv`：

```bash
uv sync --locked --all-groups
uv run playwright install chromium
uv run repotrial doctor
```

只有当 doctor 报告 `READY` 后才能继续。设置指南包含浏览器操作系统依赖和代理配置说明。

### 本地 Web 控制台

进行模型辅助运行时，请在 Web 控制台的“高级选项”中填写兼容 OpenAI 的
Base URL 和 API Key，点击“获取模型”，然后选择返回的模型或手动填写模型并
保存。Key 只持久化在 XDG 配置目录中的 owner-only 设置文件中。默认文件路径是
`~/.config/repotrial/model-settings.json`；设置 `XDG_CONFIG_HOME` 可使用其他
XDG 配置目录。设置 API 不会回显 Key，公开 job payload 不包含它，运行任务时
只会将它传给受信任的 CLI 子进程。清除模型设置即可移除它。

```bash
uv run repotrial serve --port 8765
```

打开 **http://127.0.0.1:8765/**，输入公开仓库 URL、完整提交 SHA 和内部 Web 端口。使用模型时设置模型端点/名称。控制台仅监听回环地址，并且一次只运行一个试跑。

### CLI

示例目标：Umami。将模型端点和名称替换为你的服务商值；此命令不提供上文展示的认证工作流。
认证 provider 的 Key 通过 `REPOTRIAL_MODEL_API_KEY` 提供；完整的 CLI-only
说明请参阅[使用指南](docs/dev/usage.md#fastest-supported-setup)。

```bash
uv run repotrial inspect https://github.com/umami-software/umami \
  --provider docker-sbx \
  --commit-sha ca661c7057984aa98ed4f7083d84dae2f65bfcb0 \
  --container-port 3000 \
  --compose-path docker-compose.yml \
  --model-endpoint https://MODEL-ENDPOINT/v1 \
  --model-name MODEL_NAME
```

报告写入 `artifacts/<run_id>/report/`。
如需自定义 HTTP 检查，请使用 [`--journeys-file`](docs/dev/usage.md#operator-authored-http-journeys)。

## Preview 边界

- **隔离：** 不回退到宿主机 Docker。清理采用失败即关闭策略；CPU、内存、磁盘和宿主侧沙箱时长限制仍然有效。
- **PID 限制：** 当前支持的 SBX 运行时没有经过验证的 PID 硬上限；不宣称具备 fork bomb 防护。
- **覆盖范围：** 真实目标的浏览器工作流不受支持。尚未证明 LLM 贡献被接受；操作员编写的检查不是 LLM 证据。
- **安装：** 已在支持的现有 WSL/SBX 主机上测试，不代表全新操作系统上的安装结果。
- **产品范围：** 没有公开的恢复或持久化 Web 历史；默认 API 容器不是开箱即用的沙箱服务。
- **凭据：** 仅使用一次性的目标测试账户。Web 模型 Key 保存在上文所述的 XDG
  配置目录中的 owner-only 设置文件中；CLI 模型运行使用进程环境。两种路径都
  不会把 Key 写入 Journey 文件。

## 文档

| 需求 | 指南 |
| --- | --- |
| 安装 WSL/SBX 或排查连接问题 | [环境设置](docs/dev/wsl2-linux-sbx-setup.md) |
| 编写 HTTP 检查、配置模型或排查问题 | [使用指南](docs/dev/usage.md) |
