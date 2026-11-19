# AI Native 中台 · Agent Console

> 一套「CEO Agent」中台的架构参考实现：多个业务 Agent 各自调模型分析，中心 Agent 汇总裁决，处理不了的才升级给人。

**关键词**：AI Native · CEO Agent · Agent 中台 · 多 Agent 架构 · AI 工作台 · 企业 AI 落地 · 决策自动化 · LLM 编排 · 观测点 · run_id 回标 · FastAPI · 数据看板 · agent console · multi-agent · AI orchestration

---

## 先说清楚

**这是架构参考实现，不是开箱即用的产品。**

客户专属的数据采集器已经移除或桩化，跑起来只能看到框架骨架，不会有真实业务数据。你看的是**怎么组织一个多 Agent 中台**，不是一套能直接部署的系统。

---

## 它想解决什么

大多数「AI 工作台」停在摆数字：把报表搬到一个网页上，人该怎么想还得怎么想。

这套的立意相反：**判断、归并、关联、提醒、建议由模型做，规则只做兜底与安全阀。**

具体三条：

1. **每张业务卡都要调模型**：不是只展示数据，而是自己得出结论、依据、建议、置信度
2. **中心 Agent 汇总裁决**：各卡结论汇总成一个决策
3. **只有三类才升级给人**：碰钱和合同、今天必须动、需要二选一

## 架构

```
业务 Agent（每张卡）
  ├─ 取数 → 分析（调模型）→ 结论/依据/建议/置信度
  └─ 写入运行轨迹（run_id）
        ↓
中心 Agent：汇总 → 裁决 → 生成进化建议
        ↓
只有三类升级给人 ← 面板 / 推送
```

## 核心模块

| 文件 | 作用 |
|---|---|
| `ai.py` | 模型调用封装，所有调用的唯一入口 |
| `llmcfg.py` | 模型配置（端点、模型名、参数） |
| `router.py` | 请求路由与任务分发 |
| `judge.py` | 判分：给消息/结论打分，决定是否升级 |
| `context.py` | 上下文拼装与裁剪 |
| `assistant.py` | 助理 Agent |
| `evolve.py` | 进化建议生成 |
| `monitor.py` | 运行监控 |
| `oa.py` / `oa_sync.py` | 办公数据同步 |
| `store.py` / `threads.py` | 存储与线程管理 |
| `collectors.py` | 业务采集器**（已桩化）** |
| `wxreader.py` | 微信数据读取**（需配合独立解密库）** |

## 两条设计铁律

### 1. 观测点只挂在一处

所有模型调用收敛到 `ai.py` 的一个封装函数，轨迹只在这一层记录，不在各 Agent 内部散埋点。散埋点会随框架版本一起烂掉。

### 2. 每次运行有 run_id

一次运行（一张卡 / 一轮取数）= 一个 `run_id`，把它的多次调用串起来。轨迹没有归属等于没记。

回标用人的动作：采纳 / 跳过 / 改了再做，本身就是分数，事后回填。

## 快速开始

需要 Python 3.10+。

```bash
pip install fastapi uvicorn
python start_console.py
```

配置模型端点：编辑 `llmcfg.py` 或设置对应环境变量。

> 因为采集器已桩化，启动后看到的是空框架，属预期行为。

## 目录

```
ai-native-console/
├── ai.py            模型调用入口（观测点）
├── llmcfg.py        模型配置
├── router.py        路由分发
├── judge.py         判分与升级
├── context.py       上下文拼装
├── assistant.py     助理 Agent
├── evolve.py        进化建议
├── monitor.py       运行监控
├── collectors.py    采集器（桩）
├── store.py         存储
├── threads.py       线程
├── app.py           FastAPI 入口
└── start_console.py 启动脚本
```

## 适用与不适用

**适用**：想给企业搭一套多 Agent 决策中台，需要参考分层、观测点、升级机制怎么设计。

**不适用**：想要一个装完就能用的产品。这个不是。

## 授权

MIT License

## 联系

要做企业级 Agent 中台落地，加微信：**enhaopang**

添加时请备注：GitHub-ai-native-console

> 不提供免费技术支持。问题请走 Issue。
