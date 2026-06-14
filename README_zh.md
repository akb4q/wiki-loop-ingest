# Wiki Loop Ingest

一个用于批量 LLM Wiki 消化的 skill，把原本手动的"消化 → 继续 → 继续"流程改成半自动循环。基于 Loop Engineering 思路：调度层读取队列，Maker 调用现有 ingest 流程生成 Wiki 页面，独立 Checker 做确定性校验——失败立即停止，成功再继续下一个。

## 它做什么

- 批量消化 `raw/` 目录中的多篇待处理材料
- 复用现有 Maker 流程生成 `wiki/sources`、`wiki/concepts`、`wiki/entities` 等页面
- 用本地 append-only journal 记录状态，而不依赖对话上下文维持进度
- 通过独立 Checker 把自动继续限制在机械性错误，语义问题一律停下交给人

## 它怎么工作

核心是 Maker-Checker 分离：

- **Maker**：沿用现有 ingest 流程，把单篇原始材料转成 Wiki 页面，并更新 `wiki/index.md`、`wiki/log.md`
- **Checker**：独立于 Maker 运行（单独进程 / 确定性脚本），参考 `references/checker.py`——绝不与 Maker 共用同一次 LLM 调用
- **Journal**：每次处理写入 `~/.hermes/ingestion/run_journal.jsonl`，用于跳过已完成项、识别同一路径但内容已变更的文件
- **Fail fast**：白名单中的机械错误允许一次修复；复检仍失败，或出现非白名单问题，立即停止并请求人工介入

这就是 Loop Engineering 的边界：调度、生成、校验、状态分离——模型不能既当运动员又当裁判。

## 前置条件

需要先准备好以下本地状态：

- `~/.hermes/ingestion/config.json`
  - 至少包含 `vault_root`
  - 通常也会定义待扫描的 `raw_dirs`
- `~/.hermes/ingestion/run_journal.jsonl`
  - append-only 的运行日志
  - 不存在时用 `touch` 创建空文件
- Wiki 目录结构与基础文件
  - `wiki/SCHEMA.md` — 标签分类
  - `wiki/index.md` — 导航索引
  - `wiki/log.md` — 消化日志
  - 目录：`wiki/sources`、`wiki/concepts`、`wiki/entities`

## 用法

在 Hermes / agent 对话里直接说：

```text
消化队列
```

这个触发词会让 skill：

1. 读取配置与 journal
2. 扫描未处理的原始材料
3. 对每个文件按 Maker → Checker 顺序处理
4. 成功则写入 journal 并继续，失败则立刻停下汇报

单篇材料不建议走这个 skill，直接用单文件 ingest 更轻。
