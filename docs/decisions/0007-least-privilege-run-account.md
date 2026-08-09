# ADR-0007：控制平面最小权限运行账户设计

- 日期：2026-08-09
- 状态：已接受（设计）；云端侧已落地，Windows 侧待执行
- 相关：ADR-0002（进程树与 PID）、ADR-0003（超时分类）

## 背景

控制平面当前以 `metra`（交互登录用户）经计划任务 `ControlPlane` 运行，进程具有该用户的全部文件与网络权限。批次 1 审计确认：`D:\infrastructure\compose\dify\.env` 与 `observability\.env` 对 `CodexSandboxUsers` 与 `BUILTIN\Users` 可读——Agent 会话与任意本机用户可读运行密钥，与控制平面“阻止读取无关凭据”的边界冲突。

## 决策

### Windows 侧（设计，待执行）

1. 为控制平面创建专用运行账户 `cp-service`（无管理员组、无交互登录、无网络位置共享权限）。
2. 授予的最小权限：
   - `D:\download\agent\control-plane`（读 + data 目录写）；
   - `%LOCALAPPDATA%\dev-maintenance` 日志目录（写）；
   - 仅回环网络（默认），由控制平面自身的绑定地址 `127.0.0.1:18083` 保证；
   - 不授予对 `D:\infrastructure\compose\*\*.env`、`~\.ssh`、Credential Manager、`~\.codex` 的访问。
3. 计划任务 `ControlPlane` 以 `cp-service` 运行；启动器保持隐藏窗口。
4. 云侧对接：远端命令仍经 `ssh metratio` 以 `ratio` 执行（云端 sudoers 已收窄为白名单，见 ADR 附录），Windows 侧账户不持有 SSH 私钥副本。

### 云端侧（已落地 2026-08-09）

`/etc/sudoers.d/90-ratio` 将 `ratio` 收窄为 18 条白名单命令（firewall-cmd、dnf、yum、crontab、ausearch、tailscale、docker、systemctl、certbot、sshd、df、lastb、install、cp、visudo、reboot、gpasswd），移除 `(ALL) ALL` 与 wheel 组成员；`sudo -n cat /etc/shadow` 等未授权命令被拒。残余风险：docker/systemctl/visudo/gpasswd 属 root 等效，保留原因见变更记录。

## 验证

- Windows 账户建立后：以 `cp-service` 启动实例，验证 `/ready`、`/live`、修复流程与飞书审批全链路。
- 环境变量密钥类文件 ACL 收窄后：Codex 沙箱运行一次修复任务，确认无法读取 `.env`（路径黑名单 + ACL 双保险）。

## 回退

- Windows：将计划任务运行账户改回 `metra`。
- 云端：重传原 `ratio` sudoers 文件（install 在白名单内可自恢复）并 `gpasswd -a ratio wheel`；回滚基线见批次 1 证据。

## 附录：sudoers 命令白名单来源

| 命令 | 用途 |
|---|---|
| firewall-cmd | 每日扫描与入口检查 |
| dnf / yum | 安全更新检查 |
| crontab | root crontab 核对 |
| ausearch | SELinux AVC 采集 |
| tailscale / docker / systemctl | 服务与容器运维 |
| certbot / sshd | 证书与 SSH 配置检查 |
| df / lastb | 磁盘与登录失败记录 |
| install / cp / visudo | 配置部署与 sudoers 自维护 |
| reboot | 每周日 04:00 云服务器重启 |
| gpasswd | 用户组维护 |
