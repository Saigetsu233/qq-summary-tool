# QQ 群聊 AI 总结工具

从本机 NTQQ 聊天记录的结构化导出库中选择群聊和日期，一键生成轻松、准确、可直接粘贴到 QQ 群的纯文本日报。

> 当前版本采用只读安全模式：读取 `nt_msg_export.db`，不会附加、注入或修改 QQ 进程，也不会修改原始 `nt_msg.db`。

## 已实现

- 只读打开标准 SQLite 导出库
- 自动列出群聊并按文本消息数量排序
- 按起止日期筛选群聊文本
- 根据发送者 QQ/UID 稳定区分发言人
- 支持本地群名和群昵称映射，不向 AI 暴露 QQ 号
- 长聊天自动分段总结，不截断前文
- 输出适合 QQ 的 emoji 纯文本，自动清理 Markdown
- 同时支持 DeepSeek 官方 API 和 NVIDIA API Catalog
- 服务商、API Key 与模型名可在界面中切换，两套 Key 分开保存
- 对 401、402、403、404、429、500、503 等常见 API 错误给出中文提示
- API Key、数据库、昵称映射和总结文件默认全部 Git 忽略

## 数据准备

NTQQ 的原始 `nt_msg.db` 是带自定义头的 SQLCipher 数据库，消息正文还需要解析 Protobuf。当前版本兼容 [QQBackup/nt_msg_db_util](https://github.com/QQBackup/nt_msg_db_util) 生成的结构化文件：

```text
nt_msg_export.db
```

按照该项目文档依次生成明文库和结构化导出库。处理前务必备份原始数据，并且只处理本人拥有或已获授权的数据。

`nt_msg_export.db` 至少需要包含 `group_messages` 表及以下字段：

```text
timestamp, sender_uid, sender_qq, group_id, group_qq, text
```

## 快速开始

```powershell
git clone https://github.com/Saigetsu233/qq-summary-tool.git
cd qq-summary-tool
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python qq_gui.py
```

启动后：

1. 点击“自动查找”或“选择导出数据库”。
2. 选择群聊和日期范围。
3. 选择“DeepSeek 官方”或“NVIDIA API Catalog”。
4. 填写该服务商的 API Key，按需修改模型名。
5. 点击“生成总结”，完成后直接复制到 QQ 群。

## 使用 NVIDIA 免费接口

1. 在 [NVIDIA API Catalog](https://build.nvidia.com/) 登录，打开一个带 **Free Endpoint** 的文本模型。
2. 点击模型页上的 **Generate API Key** 并复制 Key。
3. 在工具中选择“NVIDIA API Catalog”，粘贴 Key 后生成总结。

默认模型为截图中的 `deepseek-ai/deepseek-v4-pro-0813`。模型名输入框可编辑；如果 NVIDIA 返回“没有找到模型”，请在当前模型页复制最新的 `model` 值。

NVIDIA 页面将这类接口标为原型阶段免费端点，但免费模型、频率、额度和可用性可能变化，以 NVIDIA 账号和模型页当时显示为准。

## 昵称映射（可选）

标准导出库能准确区分发送者，但通常不包含群名和群昵称。工具默认使用“群友1、群友2”这类匿名稳定标签，避免把 QQ 号发送给 AI。

如需显示真实群名和昵称，将 `aliases.example.json` 复制为 `aliases.json`，放在 `nt_msg_export.db` 同一目录并填写：

```json
{
  "groups": {
    "123456789": "滑雪交流群"
  },
  "members": {
    "123456789": {
      "100001": "阿雪",
      "100002": "小明"
    }
  }
}
```

`aliases.json` 已被 Git 忽略，不会意外提交。

## 为什么没有直接自动解密正在运行的 QQ？

Windows NTQQ 的自动密钥提取通常需要以调试器启动 QQ、设置断点并短暂修改目标进程内存。参考实现明确提示这种方法可能导致聊天数据异常或账号风险，因此当前公开版本没有默认执行这类操作。

后续计划将自动数据接入做成独立、明确提示风险、可完全关闭的模块；安全模式始终保留。

## 隐私与安全

- 工具只读打开导出数据库，不修改源文件。
- QQ 号、UID、昵称、数据库、API Key 和总结结果均默认 Git 忽略。
- 没有昵称映射时，发送给 AI 的只有“群友1”等匿名标签。
- 生成总结时，所选时间范围内的文本会发送到你选择的 DeepSeek 或 NVIDIA API；使用前请取得群成员同意并遵守当地法律法规。
- API Key 仅保存在本地 `config.json`（明文），不会写入总结或提交到 Git。共用电脑上建议使用后清空 Key。
- 不要把 `nt_msg.db`、`nt_msg_export.db`、`config.json` 或 `aliases.json` 上传到公开仓库。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试使用人工构造的 SQLite 数据，不包含真实 QQ 号或聊天内容，也不会调用真实 AI API。

## 参考与致谢

- [QQBackup/nt_msg_db_util](https://github.com/QQBackup/nt_msg_db_util)：NTQQ 数据库解密、Protobuf 解析与结构化导出格式
- [QQBackup/QQDecrypt](https://github.com/QQBackup/QQDecrypt)：NTQQ 数据库格式及跨平台研究文档
- [QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key)：Windows NTQQ 密钥提取研究与风险说明

本仓库没有打包或自动执行上述项目的密钥提取脚本。

## 许可证

本项目采用 [GNU General Public License v3.0](LICENSE)。
